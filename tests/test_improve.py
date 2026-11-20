import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from foreman.cli import main
from foreman.improve import (
    WriteSurfaceError,
    check_write_surface,
    collect_improvement_bundle,
    record_improvement_run,
    run_self_improvement,
)
from foreman.telemetry import TelemetryStore


class ImproveGuardAndLoopTest(unittest.TestCase):
    def test_write_surface_allows_prompts_evals_local_skills_and_rejects_protected(self):
        allowed = [
            "src/foreman/stages/implement.md",
            "src/foreman/skills/merge-risk.md",
            "evals/promptfooconfig.yaml",
            ".agents/skills/my-skill-local/SKILL.md",
        ]
        self.assertEqual(check_write_surface(allowed), allowed)

        rejected = [
            "src/foreman/models.toml",
            "src/foreman/measure.toml",
            "src/foreman/pricing.toml",
            "src/foreman/delivery.py",
            "src/foreman/runner.py",
            "src/foreman/save.py",
            "src/foreman/config.py",
            ".github/workflows/ci.yml",
            "AGENTS.md",
            "src/foreman/../models.toml",
            "/etc/passwd",
            ".agents/skills/upstream-core/SKILL.md",
            "README.md",
        ]
        for bad in rejected:
            with self.subTest(bad=bad), self.assertRaises(WriteSurfaceError):
                check_write_surface([bad])

    def _seed_failures(self, store: TelemetryStore, count: int = 3) -> list[str]:
        rids = []
        now = datetime.now(timezone.utc)
        for i in range(count):
            ts = (now - timedelta(hours=i)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            rid = store.record_run({
                "repo": "Example/Engine",
                "issue": 100 + i,
                "stop": "verify_cap",
                "failure_phase": "verify",
                "halt_detail": f"failed check {i}",
                "models_used": {"implement": "gpt-6.2-sol-high"},
            }, finished_at=ts)
            store.upsert_score(
                run_id=rid,
                scorer="task-compliance",
                label="fail",
                score=0.0,
                passed=False,
                reason="missed constraint",
                judge_model="claude-sonnet-5-5-high",
                scored_at=ts,
            )
            rids.append(rid)
        return rids

    def test_cadence_and_threshold_gating_no_change_and_dry_run_propose(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            (repo_root / "src/foreman/stages").mkdir(parents=True)
            (repo_root / "src/foreman/stages/implement.md").write_text("Old implement instruction.\n", encoding="utf-8")

            store = TelemetryStore(root / "tel.sqlite3")
            # Below threshold when only 1 failure exists
            self._seed_failures(store, 1)
            res_low = run_self_improvement(store, repo_root, min_failing_runs=3, review_feedback=[], triage_feedback=[])
            self.assertEqual(res_low["status"], "skipped")
            self.assertEqual(res_low["reason"], "below_threshold")

            # Seed remaining failures
            self._seed_failures(store, 2)
            # Cadence window blocks within 7 days unless now=True
            now = datetime.now(timezone.utc)
            recent = (now - timedelta(days=2)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            record_improvement_run(store.path, mode="all", action="propose", title="Prev", created_at=recent)

            res_cadence = run_self_improvement(
                store, repo_root, min_failing_runs=3, now=False, review_feedback=[], triage_feedback=[], current_time=now
            )
            self.assertEqual(res_cadence["status"], "skipped")
            self.assertEqual(res_cadence["reason"], "cadence_window")

            # now=True bypasses cadence and honors no_change
            res_no_change = run_self_improvement(
                store, repo_root, min_failing_runs=3, now=True, dry_run=True,
                review_feedback=[], triage_feedback=[],
                observer=lambda bundle: {"action": "no_change", "reason": "Instructions already cover it."},
            )
            self.assertEqual(res_no_change["status"], "no_change")

            # Dry run proposal produces unified diff without mutating file
            res_dry = run_self_improvement(
                store, repo_root, min_failing_runs=3, now=True, dry_run=True,
                review_feedback=[], triage_feedback=[],
                observer=lambda bundle: {
                    "action": "propose",
                    "title": "Clarify verification loop",
                    "summary": "Clarifies error handling principle for owner@example.invalid.",
                    "regressions_addressed": ["Example/Engine#100 verify_cap"],
                    "edits": [{
                        "path": "src/foreman/stages/implement.md",
                        "content": "Old implement instruction.\nInspect captured check output before editing.\n",
                    }],
                },
            )
            self.assertEqual(res_dry["status"], "dry_run")
            self.assertIn("+Inspect captured check output", res_dry["diffs"]["src/foreman/stages/implement.md"])
            self.assertIn("## Regressions addressed", res_dry["pr_body"])
            self.assertNotIn("owner@example.invalid", res_dry["pr_body"])
            self.assertEqual(
                (repo_root / "src/foreman/stages/implement.md").read_text(encoding="utf-8"),
                "Old implement instruction.\n",
            )

            # Observer attempting to edit protected path raises WriteSurfaceError before touching disk
            with self.assertRaises(WriteSurfaceError):
                run_self_improvement(
                    store, repo_root, min_failing_runs=3, now=True, dry_run=False,
                    review_feedback=[], triage_feedback=[],
                    observer=lambda bundle: {
                        "action": "propose",
                        "title": "Bad edit",
                        "summary": "s",
                        "regressions_addressed": ["r1"],
                        "edits": [{"path": "src/foreman/models.toml", "content": "x"}],
                    },
                )

    def test_insufficient_evidence_excluded_from_bundle_and_legacy_cli_preserved(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = TelemetryStore(root / "tel.sqlite3")
            rid = store.record_run({"repo": "Example/Engine", "issue": 1, "stop": "verified"})
            store.upsert_score(
                run_id=rid, scorer="task-compliance", label="insufficient_evidence",
                score=0.5, passed=False, reason="thin", judge_model="claude-sonnet-5-5-high",
            )
            bundle = collect_improvement_bundle(store, review_feedback=[], triage_feedback=[])
            self.assertEqual(bundle["failing_count"], 0)
            self.assertEqual(bundle["failing_scores"], [])

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["improve", "redundant-tests"])
        self.assertEqual(code, 0)
        self.assertIn("Proposed change to stages/implement.md.", out.getvalue())

        # --run dry-run via CLI
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            (repo_root / "src/foreman/stages").mkdir(parents=True)
            (repo_root / "src/foreman/stages/implement.md").write_text("Base.\n", encoding="utf-8")
            db = root / "tel.sqlite3"
            store = TelemetryStore(db)
            self._seed_failures(store, 3)

            cli_out = io.StringIO()
            with contextlib.redirect_stdout(cli_out):
                code2 = main(
                    ["improve", "--run", "--db", str(db), "--workdir", str(repo_root), "--mode", "scorer", "--now", "--dry-run"],
                    observer_runner=lambda b: {
                        "action": "propose",
                        "title": "Improve implement guidance",
                        "summary": "Tightens verify guidance.",
                        "regressions_addressed": ["Example/Engine#100"],
                        "edits": [{"path": "src/foreman/stages/implement.md", "content": "Base.\nUpdated.\n"}],
                    },
                )
            self.assertEqual(code2, 0)
            self.assertIn("## Regressions addressed", cli_out.getvalue())


if __name__ == "__main__":
    unittest.main()
