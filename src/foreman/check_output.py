"""Verifier diagnostics and JUnit evidence from a repository check."""

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Literal
from typing_extensions import TypedDict
from xml.etree import ElementTree

from foreman.credentials import credentials
from foreman.redaction import PrivateKeyBlock, redact
from foreman.container import CheckLog, TimedOut


_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[@-~])")
_MARKER = "[... output omitted ...]\n"


def _window(text: str, size: int, lines: int, *, tail: bool = False) -> str:
    data = text.encode("utf-8")
    text = (data[-size:] if tail else data[:size]).decode("utf-8", errors="ignore") if size else ""
    parts = text.splitlines(keepends=True)
    return "".join(parts[-lines:] if tail else parts[:lines]) if lines else ""


class CheckOutput:
    def __init__(self, log: CheckLog | None = None):
        self.log = log
        self.head = self.tail = ""
        self.omitted: bool = False
        self.key_block = PrivateKeyBlock()
        self.values = tuple(sorted({*credentials.known_values(), *(os.environ.get(name, "") for name in
                                   ("GH_TOKEN", "GITHUB_TOKEN"))} - {""}, key=len, reverse=True))

    def __call__(self, output: str) -> None:
        if self.log:
            self.log(output)
        output = _ESCAPE.sub("", output)
        output = re.sub(r"\r\n|[\r\v\f\x1c-\x1e\x85\u2028\u2029]", "\n", output)
        output = output.encode("utf-8", errors="replace").decode("utf-8")
        for line in output.splitlines(keepends=True):
            line = self.key_block(line)
            for value in self.values:
                line = line.replace(value, "[redacted:Credential]")
            clean = redact(line)
            rest = clean
            if not self.tail and not self.omitted:
                combined = self.head + clean
                self.head = _window(combined, 2048, 32)
                rest = combined[len(self.head):]
            pending = self.tail + rest
            self.tail = _window(pending, 6144, 96, tail=True)
            self.omitted = self.omitted or self.tail != pending

    @property
    def text(self) -> str:
        if not self.head and not self.tail:
            return "[no check output]"
        if not self.omitted:
            return self.head + self.tail
        head = _window(self.head, 8191 - len((self.tail + _MARKER).encode("utf-8")),
                       127 - len(self.tail.splitlines()))
        return head.rstrip("\n") + "\n" + _MARKER + self.tail


TestResult = Literal["passed", "failed", "skipped"]


class ReportUnreadable(ValueError):
    """The check's JUnit XML test report is not XML that Foreman can read. The message names the file."""


class TestCases(TypedDict):
    passed: int
    failed: int
    skipped: int


TestReport = dict[str, TestCases]


@dataclass(frozen=True)
class CheckEvidence:
    exit: int | TimedOut
    tests: TestReport | ReportUnreadable
    missing: list[str]

    def report(self) -> TestReport:
        if isinstance(self.tests, ReportUnreadable):
            raise self.tests
        return self.tests


def _cases(element: ElementTree.Element, suites: tuple[str, ...] = ()) -> Iterator[tuple[ElementTree.Element, tuple[str, ...]]]:
    if element.tag == "testcase":
        yield element, suites
    else:
        if element.tag == "testsuite":
            suites = (*suites, element.get("name", ""))
        for child in element:
            yield from _cases(child, suites)


def check_reports(root: Path, reports: Sequence[str], invoke: Callable[[], int | TimedOut]) -> CheckEvidence:
    """Remove generated reports, then read only the evidence left by this invocation."""
    root = root.resolve()
    paths = dict.fromkeys(reports)
    for report in paths:
        path = root / report
        if not path.parent.resolve().is_relative_to(root):
            raise ReportUnreadable(f"The test report {report} leaves the checkout.")
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            raise ReportUnreadable(f"Cannot remove the generated test report {report}: {exc}") from exc
    exit_code = invoke()
    found: TestReport = {}
    missing: list[str] = []
    for report in paths:
        path = root / report
        if not path.resolve().is_relative_to(root):
            return CheckEvidence(exit_code, ReportUnreadable(f"The test report {report} leaves the checkout."), missing)
        if not path.is_file():
            missing.append(report)
            continue
        try:
            tree = ElementTree.parse(path)
        except (OSError, ElementTree.ParseError) as exc:
            return CheckEvidence(exit_code, ReportUnreadable(f"Cannot read {report} as JUnit XML: {exc}"), missing)
        for case, suites in _cases(tree.getroot()):
            file = case.get("file", "")
            relative = os.path.relpath(root / file, root) if file else ""
            test = json.dumps([relative, list(suites), case.get("classname", ""), case.get("name", "")], ensure_ascii=False)
            counts = found.setdefault(test, {"passed": 0, "failed": 0, "skipped": 0})
            result: TestResult = ("failed" if case.find("failure") is not None or case.find("error") is not None
                                  else "skipped" if case.find("skipped") is not None else "passed")
            counts[result] += 1
    return CheckEvidence(exit_code, found, missing)


def canonical_test_id(test: str) -> str:
    try:
        return json.dumps(json.loads(test), ensure_ascii=False)
    except json.JSONDecodeError:
        return test
