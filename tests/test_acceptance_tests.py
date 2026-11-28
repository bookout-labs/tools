"""implement.tests: a verifier-role worker writes acceptance tests before the build, and plain code proves them (IM5)."""

import tests  # noqa: F401

from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from foreman.adapters import Outcome
from foreman.config import load_config
from foreman.contract import CONTRACT_PATH
from foreman.delivery import LiveRun
from foreman.dry_run import SCRIPTED_USAGE, ScriptHandler
from foreman.github import LiveForge
from foreman.check_output import ReportUnreadable, check_reports
from foreman.proof import needs_tests, rejected
from foreman.runner import run_live_delivery
from foreman.state import ChecksState
from foreman.workspace import LiveWorkspace
from publication_fixture import EMPTY_SLICE, PublicationFixture, TEST_REPORT, contract_text

# The target's check: it writes a JUnit XML report with the base's own test and one test case for each accept/*.t file. A file
# that says needs-items fails until items.txt exists, and the check exits 1 when a test case fails.
CHECK_SCRIPT = """mkdir -p reports
status=0
{
  echo '<testsuites><testsuite name="check">'
  echo '<testcase classname="base" name="kept"/>'
  for f in accept/*.t; do
    [ -e "$f" ] || continue
    name=$(basename "$f" .t)
    if grep -q needs-items "$f" && ! test -f items.txt; then
      echo "<testcase classname=\\"accept\\" name=\\"$name\\"><failure message=\\"no items\\"/></testcase>"
      status=1
    else
      echo "<testcase classname=\\"accept\\" name=\\"$name\\"/>"
    fi
  done
  echo '</testsuite></testsuites>'
} > reports/junit.xml
exit $status
"""
BASE_REPORT = {'["", ["check"], "base", "kept"]': {"passed": 1, "failed": 0, "skipped": 0}}
TESTED = {**EMPTY_SLICE, "criteria": [{"text": "The view lists every item once.", "proof": {"kind": "test"}}]}
DRIVEN = {**EMPTY_SLICE, "criteria": [{"text": "The view lists every item once.", "proof": {"kind": "drive", "not_drivable": None}}]}


class ProofTest(unittest.TestCase):
    """The plain-code proof reads test IDs from the report without knowing the language."""

    def test_new_tests_must_be_listed_and_fail(self):
        base = BASE_REPORT
        test = '["", ["check"], "accept", "once"]'
        self.assertIn("lists none of the new tests", rejected(base, base, [TEST_REPORT]))
        for result in ("passed", "skipped", "failed"):
            cases = dict.fromkeys(("passed", "failed", "skipped"), 0)
            cases[result] = 1
            evidence = rejected(base, {**base, test: cases}, [TEST_REPORT])
            if result == "failed":
                self.assertEqual(evidence, "")
            else:
                self.assertIn(f"{test} ({result})", evidence)

    def test_partial_and_missing_reports_cannot_reuse_earlier_cases(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        xml = '<testsuite name="outer"><testsuite name="inner">{}</testsuite></testsuite>'
        same = '<testcase file="{}" classname="c" name="n">{}</testcase>'

        def both():
            (root / "a.xml").write_text(xml.format(same.format(root / "a.py", "") + same.format("a.py", "<skipped/>")))
            (root / "b.xml").write_text(xml.format(same.format("b.py", "<error/>")))
            return 1

        def partial():
            (root / "a.xml").write_text(xml.format(same.format("a.py", "")))
            return 0

        reports = ["a.xml", "b.xml"]
        first = check_reports(root, reports, both)
        self.assertEqual(first.exit, 1)
        self.assertEqual(first.report(), {
            '["a.py", ["outer", "inner"], "c", "n"]': {"passed": 1, "failed": 0, "skipped": 1},
            '["b.py", ["outer", "inner"], "c", "n"]': {"passed": 0, "failed": 1, "skipped": 0},
        })
        second = check_reports(root, reports, partial)
        self.assertEqual(second.missing, ["b.xml"])
        self.assertEqual(second.report(), {
            '["a.py", ["outer", "inner"], "c", "n"]': {"passed": 1, "failed": 0, "skipped": 0},
        })
        third = check_reports(root, reports, lambda: 0)
        self.assertEqual((third.exit, third.missing, third.report()), (0, reports, {}))

    def test_malformed_report_names_the_file(self):
        root = Path(self.enterContext(TemporaryDirectory()))

        def malformed():
            (root / "bad.xml").write_text("<testsuite>")
            return 0

        with self.assertRaisesRegex(ReportUnreadable, "bad.xml"):
            check_reports(root, ["bad.xml"], malformed).report()

    def test_only_a_test_or_contract_criterion_needs_tests(self):
        self.assertTrue(needs_tests(TESTED))  # type: ignore[arg-type]
        self.assertTrue(needs_tests({**TESTED, "criteria": [{"text": "x", "proof": {"kind": "contract"}}]}))  # type: ignore[arg-type]
        self.assertFalse(needs_tests(DRIVEN))  # type: ignore[arg-type]


class AcceptanceTestsTest(PublicationFixture):
    def setUp(self):
        super().setUp()
        (self.checkout / "check.sh").write_text(CHECK_SCRIPT)
        (self.checkout / ".gitignore").write_text("reports/\n")
        (self.checkout / CONTRACT_PATH).write_bytes(contract_text("sh check.sh"))
        self.git("add", "--", "check.sh", ".gitignore", CONTRACT_PATH)
        self.git("commit", "-qm", "Check")
        self.git("push", "-q", "origin", self.base)
        self.calls: list[tuple[str, ChecksState]] = []

    def deliver(self, item, tests):
        """Run the issue with the slice. Each acceptance test writer call takes the next (path, content) from tests."""
        self.slice = item
        script, workspace, written = ScriptHandler(), LiveWorkspace(self.checkout, containers=self.containers), iter(tests)

        def handler(node, model, state, instruction):
            self.calls.append((node, ChecksState.model_validate(state["checks"])))
            if node == "implement_tests":
                self.assertIn("Write the acceptance tests", instruction)
                path, content = next(written)
                (self.checkout / path).parent.mkdir(parents=True, exist_ok=True)
                (self.checkout / path).write_text(content)
                return Outcome("ran", {"tests": [{"path": path, "criterion": item["criteria"][0]["text"], "test_id": None}]},
                               usage=SCRIPTED_USAGE)
            if node == "implement":
                self.edit()
            return script(node, model, state, instruction)

        self.doctor_passed()
        return run_live_delivery(repo="Example/Engine", issue=8, checkpoint=self.checkpoint,
                                 run=LiveRun(LiveForge(), workspace), dispatcher=handler)

    def nodes(self):
        """Implement's worker calls, in order."""
        return [node for node, _ in self.calls if node in ("implement_tests", "implement")]

    def test_IM5_rejected_tests_return_to_the_writer_with_evidence_before_any_builder_call(self):
        result = self.deliver(TESTED, [("notes/once.t", "needs-items\n"), ("accept/once.t", "always\n"),
                                       ("accept/once.t", "needs-items\n")])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        self.assertEqual(self.nodes()[:4], ["implement_tests", "implement_tests", "implement_tests", "implement"])
        # Each rejected round's writer call carries the exact evidence against the last one.
        writes = [checks for node, checks in self.calls if node == "implement_tests"]
        self.assertIn("lists none of the new tests", writes[1].failure_output)
        self.assertIn('["", ["check"], "accept", "once"] (passed)', writes[2].failure_output)
        checks = result["checks"]
        self.assertEqual((checks.base_tests, checks.acceptance_tests), (BASE_REPORT, ["accept/once.t"]))
        self.assertEqual(checks.accepted_digest, sha256(b"accept/once.t\0needs-items\n\0").hexdigest())
        # Each rejected round counts toward the failed-check cap.
        self.assertEqual(checks.verify_failures, 2)
        # The accepted tests ship with the build.
        self.assertEqual(self.git("--git-dir", str(self.remote), "show", f"{result['work'].head_sha}:accept/once.t"), "needs-items")

    def test_rejections_stop_at_the_failed_check_cap_and_no_builder_starts(self):
        cap = load_config().max_review_rounds
        result = self.deliver(TESTED, [("accept/once.t", "always\n")] * cap)
        self.assertEqual(result["stop"], "failed_check_cap", result.get("halt_detail"))
        self.assertEqual(self.nodes(), ["implement_tests"] * cap)
        self.assertEqual(result["checks"].verify_failures, cap)
        self.assertEqual(self.created, [])

    def test_prepare_runs_the_base_check_for_a_slice_without_test_criteria_and_keeps_its_report(self):
        result = self.deliver(DRIVEN, [])
        self.assertEqual(result["stop"], "merged", result.get("halt_detail"))
        self.assertNotIn("implement_tests", self.nodes())
        self.assertEqual(result["checks"].base_tests, BASE_REPORT)
        # The builder's first call sees the base's report prepare kept.
        builder = next(checks for node, checks in self.calls if node == "implement")
        self.assertEqual(builder.base_tests, BASE_REPORT)

    def test_a_failing_base_check_stops_base_check_failed_before_any_worker(self):
        (self.checkout / "accept").mkdir()
        (self.checkout / "accept" / "broken.t").write_text("needs-items\n")
        self.git("add", "accept/broken.t")
        self.git("commit", "-qm", "Broken base")
        self.git("push", "-q", "origin", self.base)
        result = self.deliver(TESTED, [])
        self.assertEqual(result["stop"], "base_check_failed")
        self.assertIn("at the pinned base", result["halt_detail"])
        self.assertNotIn("implement_tests", self.nodes())
        self.assertNotIn("implement", self.nodes())


if __name__ == "__main__":
    unittest.main()
