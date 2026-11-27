"""Plain code for the acceptance test proof, so implement.py stays a router (target.yaml implement invariants)."""

from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree

from foreman.stage_output import SliceRecord

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
    """The content digest of the accepted tests, so a later change to one is seen."""
    hashed = sha256()
    for path in sorted(paths):
        hashed.update(path.encode() + b"\0" + (root / path).read_bytes() + b"\0")
    return hashed.hexdigest()
