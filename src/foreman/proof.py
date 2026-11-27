"""Plain code for the acceptance test proof, so implement.py stays a router (target.yaml implement invariants)."""

from collections.abc import Collection, Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from xml.etree import ElementTree

from foreman.contract import WORKFLOWS, protected
from foreman.stage_output import SliceRecord

if TYPE_CHECKING:
    from foreman.state import ChecksState

TestResult = Literal["passed", "failed", "skipped"]


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


def rejected(base: Mapping[str, TestResult], current: Mapping[str, TestResult], reports: Sequence[str]) -> str:
    """The evidence against the acceptance tests, or an empty string when the report lists new tests and each fails (IM5).

    The base passed its check without them, so a new test is an ID the base's report does not list."""
    new = {test: result for test, result in current.items() if test not in base}
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


def weakened(root: Path, checks: "ChecksState", item: SliceRecord, changed: Collection[str]) -> str:
    """The evidence that the candidate weakened the checks with its exact files, or an empty string (item 10, IM7).

    A test ID in the base's report that the candidate's report omits or newly marks skipped, an edited accepted test and a
    changed protected file each weaken them. A base test the slice lists as removed or renamed may be missing (F1). A slice
    of kind protected may change the protected files it names, never a CI workflow."""
    assert checks.base_tests is not None  # Prepare kept the base's report.
    try:
        current = results(root, checks.test_report)
    except ReportUnreadable as exc:
        return f"The candidate weakens the checks: {exc}"
    removed = {entry["test"] for entry in item["removed_tests"]}
    lines = [f"{test} is {current.get(test, 'missing')} in the candidate's report" for test, result in sorted(checks.base_tests.items())
             if test not in removed and (test not in current or current[test] == "skipped" != result)]
    if checks.accepted_digest and digest(root, checks.acceptance_tests) != checks.accepted_digest:
        lines.append(f"The accepted tests changed: {', '.join(sorted(checks.acceptance_tests))}")
    allowed = set(item["files"]) if item.get("kind") == "protected" else set[str]()
    lines += [f"{path} is a protected file" for path in sorted(changed) if path not in checks.acceptance_tests
              and protected(path, checks.verify_skill) and (path not in allowed or path.startswith(WORKFLOWS))]
    return ("The candidate weakens the checks. Restore these before the build can publish:\n"
            + "\n".join(f"- {line}" for line in lines)) if lines else ""
