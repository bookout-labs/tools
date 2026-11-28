"""Plain code for the acceptance test proof and the mutation sample, so implement.py stays a router (target.yaml implement invariants)."""

from collections.abc import Collection, Mapping, Sequence
from hashlib import sha256
import json
from pathlib import Path
import shlex
from typing import TYPE_CHECKING, Literal, cast

from typing_extensions import TypedDict
from xml.etree import ElementTree

from foreman.check_output import CheckOutput
from foreman.container import ContainerUnavailable, TimedOut, ended
from foreman.contract import WORKFLOWS_PATH, protected
from foreman.stage_output import AcceptanceTestsOutput, FindingRecord, SliceRecord, names_mutation

if TYPE_CHECKING:
    from foreman.stage_output import PlanState
    from foreman.state import ChecksState, DeliveryState
    from foreman.worker import AttemptJournal
    from foreman.workspace import Workspace

TestResult = Literal["passed", "failed", "skipped"]


class MutationRecord(TypedDict):
    """One mutation the contract's command made in a sample: its file:line, the tool's id for it, and killed or survived."""
    line: str
    id: str
    result: str


class ReportUnreadable(ValueError):
    """The check's JUnit XML test report is not XML that Foreman can read. The message names the file."""


def needs_tests(item: SliceRecord) -> bool:
    """Whether a criterion of the slice is proven by test or contract, so acceptance tests come before the build."""
    return any(criterion["proof"]["kind"] in ("test", "contract") for criterion in item["criteria"])


def results(root: Path, reports: Sequence[str]) -> dict[str, TestResult]:
    """Each test case's ID and result in the check's JUnit XML reports, read without knowing the language.

    A test case's ID is its classname and name. A failure or error marks it failed and a skip marks it skipped. A report the
    check did not write lists no test case."""
    found: dict[str, TestResult] = {}
    for report in reports:
        path = root / report
        if not path.is_file():
            continue
        try:
            tree = ElementTree.parse(path)
        except ElementTree.ParseError as exc:
            raise ReportUnreadable(f"The test report {report} is not JUnit XML: {exc}") from exc
        for case in tree.iter("testcase"):
            result: TestResult = ("failed" if case.find("failure") is not None or case.find("error") is not None
                                  else "skipped" if case.find("skipped") is not None else "passed")
            found[f"{case.get('classname', '')}::{case.get('name', '')}"] = result
    return found


def _batch_test_failures(tests: Mapping[str, str], report: Mapping[str, TestResult], expected: TestResult) -> list[str]:
    return [f"{criterion} requires {test} ({report.get(test, 'missing')}) to be {expected}"
            for test, criterion in sorted(tests.items()) if report.get(test) != expected]


def batch_tests(output: object) -> dict[str, str]:
    return {test.test_id: test.criterion for test in AcceptanceTestsOutput.model_validate(output).tests if test.test_id is not None}


def recorded_batch_tests(state: "DeliveryState", journal: "AttemptJournal | None") -> dict[str, str]:
    """Read the declarations from the worker receipt that produced the accepted tests."""
    if not state["plan"].batch_issues or not state["checks"].accepted_digest or journal is None:
        return {}
    attempt_id = next((entry.get("attempt_id") for entry in reversed(state.get("attempts", []))
                       if entry["stage"] == "implement_tests" and entry["outcome"] == "ran"), None)
    receipt = journal.read(attempt_id) if attempt_id is not None else False
    return {} if isinstance(receipt, bool) else batch_tests(receipt.outcome.output)


def rejected(base: Mapping[str, TestResult], current: Mapping[str, TestResult], reports: Sequence[str],
             batch_tests: Mapping[str, str] | None = None) -> str:
    """The evidence against the acceptance tests, or an empty string when the report lists new tests and each fails (IM5).

    The base passed its check without them, so a new test is an ID the base's report does not list."""
    new: dict[str, TestResult] = {test: result for test, result in current.items() if test not in base}
    if batch_tests and (unproven := _batch_test_failures(batch_tests, new, "failed")):
        return "The new batch tests must fail at the base. " + "\n".join(unproven)
    if not new:
        return (f"The check's test report ({', '.join(reports)}) lists none of the new tests, so the check does not collect "
                "them. Write them where the check collects them.")
    if passing := sorted(test for test, result in new.items() if result != "failed"):
        return ("These new tests do not fail at the base, so they prove nothing the base lacks: "
                + ", ".join(f"{test} ({new[test]})" for test in passing))
    return ""


def digest(root: Path, paths: Sequence[str]) -> str:
    """The content digest of the accepted tests, so a later change to one, or its deletion, is seen."""
    hashed = sha256()
    for path in sorted(paths):
        file = root / path
        hashed.update(path.encode() + (b"\0" + file.read_bytes() + b"\0" if file.is_file() else b"\1"))
    return hashed.hexdigest()


def weakened(workspace: "Workspace", checks: "ChecksState", plan: "PlanState", changed: Collection[str],
             batch_tests: Mapping[str, str] | None = None) -> str:
    """The evidence that the candidate weakened the checks with its exact files, or an empty string (item 10, IM7).

    A test ID in the base's report that the candidate's report omits or newly marks skipped, an edited accepted test and a
    changed protected file each weaken them. A base test the slice lists as removed or renamed may be missing (F1). A slice
    of kind protected may change the protected files it names, never a CI workflow. A slice of kind revert that changes only
    files its merge commit changed may delete the tests that commit added (IM11)."""
    assert checks.base_tests is not None  # Prepare kept the base's report.
    root = workspace.root
    try:
        current = results(root, checks.test_report)
    except ReportUnreadable as exc:
        return f"The candidate weakens the checks: {exc}"
    removed = {entry["test"] for item in plan.build_slices for entry in item["removed_tests"]}
    item = plan.slice
    reverting, sha = item is not None and item["kind"] == "revert", item.get("merge_commit_sha", "") if item else ""
    outside = sorted(set(changed) - workspace.changed_files(f"{sha}^", sha)) if reverting else []
    reverted = set(checks.base_tests) if reverting and not outside else set[str]()
    lines = [f"{test} is {current.get(test, 'missing')} in the candidate's report" for test, result in sorted(checks.base_tests.items())
             if test not in removed and (test not in current and test not in reverted or current.get(test) == "skipped" != result)]
    if plan.batch_issues and checks.accepted_digest:
        declared = batch_tests or {}
        lines += [f"{label} has no declared test proof" for label, criterion in plan.build_criteria()
                  if criterion["proof"]["kind"] in ("test", "contract") and label not in declared.values()]
        lines += _batch_test_failures(declared, current, "passed")
    if lines and outside:
        lines.append(f"The revert changes files merge commit {sha} did not change, so it may not delete tests: {', '.join(outside)}")
    if checks.accepted_digest and digest(root, checks.acceptance_tests) != checks.accepted_digest:
        lines.append(f"The accepted tests changed: {', '.join(sorted(checks.acceptance_tests))}")
    allowed = set(item["files"]) if item is not None and item["kind"] == "protected" else set[str]()
    lines += [f"{path} is a protected file" for path in sorted(changed) if path not in checks.acceptance_tests
              and protected(path, checks.verify_skill) and (path not in allowed or path.startswith(WORKFLOWS_PATH))]
    return ("The candidate weakens the checks. Restore these before the build can publish:\n"
            + "\n".join(f"- {line}" for line in lines)) if lines else ""


def is_test_file(path: str) -> bool:
    """Whether a path is a test file, by the conventions test runners collect: a test or tests directory, or a test_ or
    _test name. The mutation sample leaves these out, and so does review's finding for files outside the plan's list."""
    *directories, name = path.split("/")
    stem = name.rsplit(".", 1)[0]
    return bool({"test", "tests"} & set(directories)) or stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))


def standing(claims: Mapping[str, str], findings: Sequence[FindingRecord]) -> dict[str, str]:
    """The builder's equivalent-mutation claims that no blocking review finding rejects by naming the mutation's id (IM12).
    A rejected claim's mutation is in the next sample again."""
    return {mutation: reason for mutation, reason in claims.items() if not any(names_mutation(finding, mutation) for finding in findings)}


def survived(workspace: "Workspace", state: "DeliveryState", *, output: CheckOutput) -> "ChecksState":
    """The checks after the mutation sample, with each mutation no test notices as the failure evidence (item 15, IM6).

    The sample is up to sample_cap of the lines the checked tree changes from the base outside test files, spread evenly over
    them, because the mutation command flips no line of a test (G14). The contract's
    mutation command reads it on standard input in the attempt container. A mutation the builder named equivalent, by its
    id, is left out while its claim stands. Each surviving round counts toward the failed-check cap. The checks keep the
    sample's mutations with each result."""
    assert "work" in state
    work, pinned, checks = state["work"], state["run"].base_sha, state["checks"]
    checks = checks.model_copy(update={"equivalent_mutations": standing(checks.equivalent_mutations, state["review"].findings)})
    # A revert restores code from before its merge commit, so the sample takes none of its lines.
    reverting = (state["plan"].slice or {}).get("kind") == "revert"
    changed = [] if reverting else [line for line in workspace.changed_lines(work.base_merge.head if work.base_merge else pinned, checks.checked_tree)
                                    if not is_test_file(line.rpartition(":")[0])]
    cap = checks.sample_cap
    lines = [changed[index * len(changed) // cap] for index in range(cap)] if len(changed) > cap else changed
    printed: list[str] = []

    def log(text: str) -> None:
        output(text)
        printed.append(text)
    command = f"printf '%s\\n' {shlex.join(lines)} | {{\n{checks.mutation}\n}}"
    if lines and (isinstance(result := workspace.run(command, base=pinned, log=log), TimedOut) or result in (126, 127)):
        raise ContainerUnavailable(f"Mutation command {checks.mutation!r} {ended(result)}.\n\n{output.text}")
    checks = checks.model_copy(update={"mutations": [mutation for mutation in map(_mutation, printed) if mutation]})
    found = [f"- {mutation['line']} (id {mutation['id']})" for mutation in checks.mutations
             if mutation["result"] == "survived" and mutation["id"] not in checks.equivalent_mutations]
    if not found:
        return checks
    return checks.model_copy(update={"verify": "survived", "verify_failures": checks.verify_failures + 1, "failure_output": (
        "No test notices these mutations of the changed lines. Add a test that notices each one, or name it in "
        "equivalent_mutations with the reason no test can notice it:\n" + "\n".join(found))})


def _mutation(text: str) -> MutationRecord | None:
    """One mutation the command printed, or None for any other output line."""
    try:
        mutation: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(mutation, dict):
        return None
    fields = cast(dict[object, object], mutation)
    if not all(key in fields for key in ("line", "id", "result")):
        return None
    return {"line": str(fields["line"]), "id": str(fields["id"]), "result": str(fields["result"])}
