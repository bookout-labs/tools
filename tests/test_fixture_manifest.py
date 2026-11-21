from pathlib import Path
import unittest
import subprocess
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from pydantic import ValidationError
from foreman.workspace import LiveWorkspace

from foreman.fixture_manifest import FIXTURE_DIR, MANIFEST_PATH, FixtureManifest


class FixtureManifestTest(unittest.TestCase):
    def test_manifest_matches_fixture_inventory(self):
        repo = Path(__file__).resolve().parents[1]
        root = repo / FIXTURE_DIR
        manifest_path = repo / MANIFEST_PATH
        manifest = FixtureManifest.from_toml(manifest_path.read_text(encoding="utf-8"))
        files = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path not in {manifest_path, root / "README.md"} and path.is_file() and not path.is_symlink()
        }
        with self.subTest("fixtures without manifest entries"):
            self.assertEqual(files - manifest.fixtures.keys(), set())
        with self.subTest("manifest entries without fixtures"):
            self.assertEqual(manifest.fixtures.keys() - files, set())


class RecordedEvidenceTest(unittest.TestCase):
    def setUp(self):

        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = LiveWorkspace(self.root)
        self.git("init", "-q")
        self.fixtures = self.root / FIXTURE_DIR
        self.fixtures.mkdir(parents=True)
        (self.fixtures / "result.txt").write_text("captured result\n")
        self.manifest = self.root / MANIFEST_PATH
        self.manifest.write_text(
            '[fixtures."result.txt"]\nkind = "captured_run"\ntool = "example"\n'
            'version = "1"\nargv = ["example", "--debug"]\n'
        )

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def commit(self):
        self.git("add", ".")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgSign=false",
                 "commit", "-qm", "Record evidence")
        return self.git("rev-parse", "HEAD")

    def check_approval(self, head):
        from foreman.delivery import LiveRun, _check_tool_facts
        from foreman.dry_run import DEFAULT_RISK
        from foreman.stage_output import ReviewOutput

        output = ReviewOutput.model_validate({
            "review": "approve", "risk": DEFAULT_RISK, "findings": [], "detail": "Reviewed.",
            "tool_facts": [{"claim": "The CLI accepts --debug.", "fixture": "result.txt"}],
        })
        return _check_tool_facts(output, LiveRun(Mock(), self.workspace), head)

    def test_unreadable_manifest_blocks_approval(self):
        cases = {"bad TOML": b"[", "schema failure": b"[fixtures.result]\nkind = 'unknown'",
                 "non-UTF-8": b"\xff", "missing manifest": None}
        for case, content in cases.items():
            with self.subTest(case=case):
                if content is None:
                    self.manifest.unlink()
                else:
                    self.manifest.write_bytes(content)
                result = self.check_approval(self.commit())
                self.assertEqual(result.review, "changes")
                self.assertIn("The CLI accepts --debug.", result.detail)
                self.assertTrue(any(f.severity == "blocking" for f in result.findings))

    def test_unavailable_head_blocks_approval(self):
        self.commit()
        for head in (None, "f" * 40):
            with self.subTest(head=head):
                result = self.check_approval(head)
                self.assertEqual(result.review, "changes")
                self.assertIn("The CLI accepts --debug.", result.detail)

    def test_custom_record_uses_its_own_directory_at_verified_head(self):
        from foreman.fixture_manifest import recorded_fixtures
        head = self.commit()
        directory = self.root / "evidence"
        directory.mkdir()
        self.manifest.rename(directory / "facts.toml")
        (self.fixtures / "result.txt").rename(directory / "result.txt")
        self.assertEqual(recorded_fixtures(self.workspace, head, "evidence/facts.toml"), set())
        self.assertEqual(recorded_fixtures(self.workspace, self.commit(), "evidence/facts.toml"), {"result.txt"})

    def test_only_regular_tracked_files_are_recorded(self):
        from foreman.fixture_manifest import recorded_fixtures

        head = self.commit()
        self.assertEqual(recorded_fixtures(self.workspace, head), {"result.txt"})
        (self.fixtures / "result.txt").unlink()
        (self.fixtures / "result.txt").symlink_to("target.txt")
        head = self.commit()
        self.assertEqual(recorded_fixtures(self.workspace, head), set())
        self.assertEqual(self.check_approval(head).review, "changes")
        self.git("update-index", "--add", "--cacheinfo", f"160000,{head},{FIXTURE_DIR}/result.txt")
        self.git("-c", "user.name=Test", "-c", "user.email=test@example.com", "-c", "commit.gpgSign=false",
                 "commit", "-qm", "Replace fixture with gitlink")
        head = self.git("rev-parse", "HEAD")
        self.assertEqual(recorded_fixtures(self.workspace, head), set())
        self.assertEqual(self.check_approval(head).review, "changes")


class GraphFactGateTest(unittest.TestCase):
    def gate(self, facts, state):
        from foreman.delivery import DryRun, _check_tool_facts
        from foreman.dry_run import DEFAULT_RISK
        from foreman.stage_output import ReviewOutput
        output = ReviewOutput.model_validate({
            "review": "approve", "risk": DEFAULT_RISK, "findings": [], "detail": "Reviewed.",
            "tool_facts": facts,
        })
        return _check_tool_facts(output, DryRun(), state.get("verified_head"), state)

    def test_only_exact_graph_check_restatement_is_exempt(self):
        from foreman.stage_output import GraphCheck
        check = GraphCheck(head="a" * 40, command="exit 0", exit_code=0)
        state = {"repo": "example/repo", "check": check.command, "path": [],
                 "verified_head": check.head, "check_exit_code": 0, "verify": "pass"}
        fact = {"claim": check.claim, "fixture": None, "graph_check": check.model_dump()}
        self.assertEqual(self.gate([fact], state).review, "approve")
        for altered in ({"claim": "CLI accepts --debug"}, {"graph_check": None},
                        {"graph_check": {**check.model_dump(), "head": "b" * 40}},
                        {"graph_check": {**check.model_dump(), "command": "other"}},
                        {"graph_check": {**check.model_dump(), "exit_code": 1}}):
            with self.subTest(altered=altered):
                self.assertEqual(self.gate([{**fact, **altered}], state).review, "changes")
        self.assertEqual(self.gate([fact], {**state, "verify": "fail"}).review, "changes")

    def test_omission_cannot_resolve_an_unrecorded_fact(self):
        fact = {"claim": "CLI accepts --debug", "fixture": None}
        state = {"repo": "example/repo", "check": "exit 0", "path": [], "tool_facts": [fact]}
        self.assertEqual(self.gate([], state).review, "changes")

    def test_record_paths_are_relative_and_normalized(self):
        from foreman.fixture_manifest import fact_record_path
        for path in ("../manifest.toml", "/manifest.toml", "a/../manifest.toml", "", ".", "a//b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                fact_record_path(path)
        self.assertEqual(fact_record_path("evidence/facts.toml"), "evidence/facts.toml")


class DerivedCaptureTest(unittest.TestCase):
    def test_derived_from_rejects_self_reference(self):
        capture = {"kind": "captured_run", "tool": "example", "version": "1", "argv": ["example"]}
        with self.assertRaisesRegex(ValidationError, "original capture"):
            FixtureManifest.model_validate({"fixtures": {"a": {**capture, "derived_from": "a"}}})

    def test_derived_from_rejects_chain(self):
        capture = {"kind": "captured_run", "tool": "example", "version": "1", "argv": ["example"]}
        with self.assertRaisesRegex(ValidationError, "original capture"):
            FixtureManifest.model_validate({"fixtures": {
                "original": capture,
                "derived": {**capture, "derived_from": "original"},
                "chain": {**capture, "derived_from": "derived"},
            }})

    def test_derived_from_requires_existing_capture(self):

        capture = {"kind": "captured_run", "tool": "example", "version": "1", "argv": ["example"]}
        with self.assertRaisesRegex(ValidationError, "existing captured run"):
            FixtureManifest.model_validate({"fixtures": {"derived.txt": {**capture, "derived_from": "absent.txt"}}})

    def test_derived_argv_must_match_original(self):

        capture = {"kind": "captured_run", "tool": "example", "version": "1", "argv": ["example"]}
        with self.assertRaisesRegex(ValidationError, "argv must match"):
            FixtureManifest.model_validate({"fixtures": {
                "original.txt": capture,
                "derived.txt": {**capture, "argv": ["other"], "derived_from": "original.txt"},
            }})

    def test_quoted_source_rejects_argv(self):

        source = {"kind": "quoted_source", "tool": "example", "version": "1",
                  "sources": ["https://example.com/source"]}
        self.assertIn("quote.txt", FixtureManifest.model_validate({"fixtures": {"quote.txt": source}}).fixtures)
        with self.assertRaises(ValidationError):
            FixtureManifest.model_validate({"fixtures": {"quote.txt": {**source, "argv": []}}})
