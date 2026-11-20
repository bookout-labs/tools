import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import subprocess
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
from tests.test_runner import git_repo


def _propose(path: str, content: str):
    return lambda bundle: {
        "action": "propose",
        "title": "Tune stage",
        "summary": "Tunes a stage.",
        "regressions_addressed": ["Example/Engine#100"],
        "edits": [{"path": path, "content": content}],
    }


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


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


    def _improve(self, store, repo_root, observer, **kwargs):
        return run_self_improvement(
            store, repo_root, now=True, review_feedback=[], triage_feedback=[], observer=observer, **kwargs
        )

    def test_insufficient_evidence_stop_is_not_a_failing_run(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            store.record_run({"repo": "Example/Engine", "issue": 1, "stop": "insufficient_evidence"})
            bundle = collect_improvement_bundle(store, review_feedback=[], triage_feedback=[])
            self.assertEqual(bundle["failing_runs"], [])

    def test_symlinked_components_and_sensitive_content_are_refused_before_reading_or_writing(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            outside = root / "outside"
            outside.mkdir()
            (outside / "implement.md").write_text("outside secret prompt\n", encoding="utf-8")
            repo_root = root / "repo"
            (repo_root / "src/foreman").mkdir(parents=True)
            (repo_root / "src/foreman/stages").symlink_to(outside, target_is_directory=True)
            (repo_root / "src/foreman/skills").mkdir()
            (repo_root / "src/foreman/skills/merge-risk.md").symlink_to(outside / "implement.md")
            store = TelemetryStore(root / "tel.sqlite3")

            for path in ("src/foreman/stages/implement.md", "src/foreman/skills/merge-risk.md"):
                for dry_run in (True, False):
                    with self.subTest(path=path, dry_run=dry_run), self.assertRaises(WriteSurfaceError):
                        self._improve(store, repo_root, _propose(path, "New.\n"), dry_run=dry_run)
            self.assertEqual((outside / "implement.md").read_text(encoding="utf-8"), "outside secret prompt\n")

            (repo_root / "evals").mkdir()
            for content in ("Mail owner@example.invalid.\n", "Use ghp_0123456789abcdefghij0123456789.\n"):
                with self.subTest(content=content), self.assertRaises(WriteSurfaceError):
                    self._improve(store, repo_root, _propose("evals/case.yaml", content), dry_run=False)
            self.assertFalse((repo_root / "evals/case.yaml").exists())

    def test_open_pr_requires_clean_tree_and_commits_only_validated_redacted_paths(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo_root = root / "repo"
            repo_root.mkdir()
            git_repo(repo_root)
            stages = repo_root / "src/foreman/stages"
            stages.mkdir(parents=True)
            (stages / "implement.md").write_text("Old.\n", encoding="utf-8")
            (stages / "review.md").write_text("Ask owner@example.invalid.\n", encoding="utf-8")
            _git(repo_root, "add", "-A")
            _git(repo_root, "commit", "-m", "stages")
            subprocess.run(["git", "init", "--bare", str(root / "origin.git")], check=True, capture_output=True)
            _git(repo_root, "remote", "add", "origin", str(root / "origin.git"))
            store = TelemetryStore(root / "tel.sqlite3")
            gh_calls = []

            def gh(cmd):
                gh_calls.append(cmd)
                return subprocess.CompletedProcess(cmd, 0, "https://github.com/Example/Engine/pull/9\n", "")

            (repo_root / "scratch.txt").write_text("unrelated\n", encoding="utf-8")
            with self.assertRaises(WriteSurfaceError):
                self._improve(store, repo_root, _propose("src/foreman/stages/implement.md", "New.\n"),
                              dry_run=False, open_pr=True, gh_runner=gh)
            self.assertEqual((stages / "implement.md").read_text(encoding="utf-8"), "Old.\n")
            (repo_root / "scratch.txt").unlink()

            # Removing a line with an email keeps new content clean but leaks it in the staged diff.
            with self.assertRaises(WriteSurfaceError):
                self._improve(store, repo_root, _propose("src/foreman/stages/review.md", "Ask the owner.\n"),
                              dry_run=False, open_pr=True, gh_runner=gh)
            self.assertEqual(gh_calls, [])
            _git(repo_root, "reset", "--hard", "-q")
            _git(repo_root, "checkout", "-q", "main")

            result = self._improve(store, repo_root, _propose("src/foreman/stages/implement.md", "New.\n"),
                                   dry_run=False, open_pr=True, gh_runner=gh)
            self.assertEqual(result["pr_url"], "https://github.com/Example/Engine/pull/9")
            self.assertEqual(_git(repo_root, "show", "--name-only", "--format=", "HEAD").split(),
                             ["src/foreman/stages/implement.md"])
            self.assertEqual(_git(repo_root, "status", "--porcelain"), "")
            self.assertEqual(len(gh_calls), 1)

    def test_bundle_filters_github_feedback_to_tracked_runs(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            store.record_run({
                "repo": "Example/Engine", "issue": 7, "stop": "verified",
                "pull_request": "https://github.com/Example/Engine/pull/41", "head_sha": "h1",
            })
            prs = [
                {"number": n, "url": f"https://github.com/Example/Engine/pull/{n}", "state": "CLOSED",
                 "closedAt": "2026-11-20T10:00:00Z", "mergedAt": None, "commits": [
                     {"oid": "h1", "authors": [{"login": "owner"}]},
                     {"oid": "h2", "authors": [{"login": "owner"}]},
                 ]}
                for n in (41, 42)
            ]
            issues = [
                {"number": n, "url": f"https://github.com/Example/Engine/issues/{n}", "state": "CLOSED",
                 "stateReason": "NOT_PLANNED", "createdAt": "2026-11-20T10:00:00Z", "labels": [], "comments": []}
                for n in (7, 8)
            ]

            def gh(args):
                return subprocess.CompletedProcess(args, 0, json.dumps(prs if args[1] == "pr" else issues), "")

            bundle = collect_improvement_bundle(store, repo="Example/Engine", days=3650, gh_runner=gh)
            self.assertEqual([i["number"] for i in bundle["review_feedback"]], [41])
            self.assertIn("followup_commits:1", bundle["review_feedback"][0]["signals"])
            self.assertEqual([i["number"] for i in bundle["triage_feedback"]], [7])

    def test_preview_diff_redacts_existing_file_lines(self):
        secret = "ghp_0123456789abcdefghij0123456789"
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            stages = root / "repo/src/foreman/stages"
            stages.mkdir(parents=True)
            (stages / "implement.md").write_text(
                f"Keep {secret} away.\nMail owner@example.invalid.\nStay.\n", encoding="utf-8"
            )
            db = root / "tel.sqlite3"
            store = TelemetryStore(db)
            observer = _propose("src/foreman/stages/implement.md", "Stay.\n")
            result = self._improve(store, root / "repo", observer, dry_run=True)
            diff = result["diffs"]["src/foreman/stages/implement.md"]
            self.assertIn("-Keep [REDACTED] away.", diff)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = main(["improve", "--run", "--db", str(db), "--workdir", str(root / "repo"),
                             "--now", "--dry-run", "--mode", "scorer"], observer_runner=observer)
            self.assertEqual(code, 0)
            for text in (diff, out.getvalue()):
                self.assertNotIn(secret, text)
                self.assertNotIn("owner@example.invalid", text)

    def test_apply_rejects_dirty_git_checkout_before_writing(self):
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir) / "repo"
            repo_root.mkdir()
            git_repo(repo_root)
            stages = repo_root / "src/foreman/stages"
            stages.mkdir(parents=True)
            (stages / "implement.md").write_text("Old.\n", encoding="utf-8")
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            with self.assertRaises(WriteSurfaceError):
                self._improve(store, repo_root, _propose("src/foreman/stages/implement.md", "New.\n"),
                              dry_run=False, open_pr=False)
            self.assertEqual((stages / "implement.md").read_text(encoding="utf-8"), "Old.\n")

            _git(repo_root, "add", "-A")
            _git(repo_root, "commit", "-m", "stages")
            result = self._improve(store, repo_root, _propose("src/foreman/stages/implement.md", "New.\n"),
                                   dry_run=False, open_pr=False)
            self.assertEqual(result["status"], "applied")
            self.assertEqual((stages / "implement.md").read_text(encoding="utf-8"), "New.\n")

    def test_bundle_skips_repo_wide_github_feedback_without_tracked_runs(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            gh_calls = []

            def gh(args):
                gh_calls.append(args)
                return subprocess.CompletedProcess(args, 0, "[]", "")

            bundle = collect_improvement_bundle(store, repo="Example/Engine", days=3650, gh_runner=gh)
            self.assertEqual((bundle["review_feedback"], bundle["triage_feedback"]), ([], []))
            self.assertEqual(gh_calls, [])

            # A run with an issue but no PR allows triage feedback only.
            store.record_run({"repo": "Example/Engine", "issue": 7, "stop": "verified"})
            collect_improvement_bundle(store, repo="Example/Engine", days=3650, gh_runner=gh)
            self.assertEqual([args[1] for args in gh_calls], ["issue"])

    def test_bundle_uses_newest_run_head_for_a_shared_pr(self):
        pr = "https://github.com/Example/Engine/pull/41"
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            for head, finished in (("h1", "2026-11-20T09:00:00Z"), ("h2", "2026-11-20T10:00:00Z")):
                store.record_run({"repo": "Example/Engine", "issue": 7, "stop": "verified",
                                  "pull_request": pr, "head_sha": head}, finished_at=finished)
            prs = [{"number": 41, "url": pr, "state": "CLOSED", "closedAt": "2026-11-20T10:00:00Z",
                    "mergedAt": None, "commits": [
                        {"oid": oid, "authors": [{"login": "owner"}]} for oid in ("h1", "h2", "h3")
                    ]}]

            def gh(args):
                return subprocess.CompletedProcess(args, 0, json.dumps(prs if args[1] == "pr" else []), "")

            bundle = collect_improvement_bundle(store, repo="Example/Engine", days=3650, gh_runner=gh)
            self.assertIn("followup_commits:1", bundle["review_feedback"][0]["signals"])


if __name__ == "__main__":
    unittest.main()
