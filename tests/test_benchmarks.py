import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.benchmarks import (
    BenchmarkError,
    load_suite,
    render_benchmark_html,
    render_benchmark_text,
    run_benchmark,
    summarize_benchmark,
)
from foreman.cli import main
from foreman.dry_run import DEFAULT_RISK
from tests.test_runner import git_repo, worker_reply


class BenchmarksTest(unittest.TestCase):
    def test_load_suite_validates_40_char_sha_and_runs_sample_dry_run(self):
        suite = load_suite(Path(__file__).resolve().parents[1] / "benchmarks" / "sample-triage")
        self.assertEqual(suite["name"], "sample-triage")
        self.assertEqual(len(suite["configs"]), 2)
        self.assertEqual(len(suite["tasks"]), 2)

        raw = run_benchmark(suite, dry_run=True)
        summary = summarize_benchmark(raw)
        self.assertEqual(len(summary["configs"]), 2)
        self.assertEqual(summary["recommendations"]["lowest_cost_passing"], "baseline-sol")
        self.assertIn("baseline-sol", summary["pareto_frontier"])
        self.assertIn("Config results:", render_benchmark_text(summary))
        self.assertIn("<svg", render_benchmark_html(summary))

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "tasks").mkdir()
            (root / "suite.yaml").write_text("name: bad\nconfigs:\n  - name: c1\ntasks:\n  - tasks/t.yaml\n")
            (root / "tasks/t.yaml").write_text("id: t1\nbase_sha: shortsha\nrequest: hi\n")
            with self.assertRaises(BenchmarkError):
                load_suite(root)

    def test_isolated_worktree_creation_lockout_and_cleanup_even_on_error(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            head = git_repo(repo)

            suite_dir = root / "suite"
            (suite_dir / "tasks").mkdir(parents=True)
            (suite_dir / "suite.yaml").write_text("name: s\nconfigs:\n  - name: cfg-a\n  - name: cfg-b\n")
            (suite_dir / "tasks/one.yaml").write_text(
                f"id: one\nbase_sha: {head}\nrequest: Test change\ncheck: exit 0\n"
            )
            suite = load_suite(suite_dir)

            seen_paths: list[Path] = []

            def failing_runner(task, cfg, wt):
                self.assertIsNotNone(wt)
                self.assertTrue(wt.exists())
                seen_paths.append(wt)
                if cfg["name"] == "cfg-b":
                    raise RuntimeError("boom inside trial")
                return {
                    "task_id": task["id"],
                    "config": cfg["name"],
                    "stop": "verified",
                    "passed": True,
                    "quality_score": 1.0,
                    "quality_label": "pass",
                    "cost_usd": 0.25,
                    "duration_ms": 500,
                }

            with self.assertRaises(RuntimeError):
                run_benchmark(suite, repo_path=repo, trial_runner=failing_runner)

            self.assertEqual(len(seen_paths), 2)
            for wt in seen_paths:
                self.assertFalse(wt.exists())
            wt_list = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
            ).stdout
            self.assertEqual(wt_list.count("worktree "), 1)

    def test_summarize_excludes_insufficient_evidence_and_cli_bench_renders_html(self):
        summary = summarize_benchmark([
            {"config": "a", "passed": True, "quality_score": 0.9, "quality_label": "pass", "cost_usd": 0.4, "duration_ms": 1000},
            {"config": "a", "passed": False, "quality_score": 0.5, "quality_label": "insufficient_evidence", "cost_usd": 0.4, "duration_ms": 1000},
            {"config": "b", "passed": True, "quality_score": 0.8, "quality_label": "pass", "cost_usd": 0.2, "duration_ms": 800},
        ])
        by_cfg = {c["config"]: c for c in summary["configs"]}
        self.assertEqual(by_cfg["a"]["avg_quality"], 0.9)
        self.assertEqual(by_cfg["a"]["insufficient_evidence"], 1)
        self.assertIn("b", summary["pareto_frontier"])

        with TemporaryDirectory() as tmpdir:
            html_out = Path(tmpdir) / "bench.html"
            suite_dir = Path(__file__).resolve().parents[1] / "benchmarks" / "sample-triage"
            with contextlib.redirect_stdout(io.StringIO()):
                code = main(["bench", str(suite_dir), "--dry-run", "--format", "html", "--out", str(html_out)])
            self.assertEqual(code, 0)
            self.assertIn("<svg", html_out.read_text(encoding="utf-8"))

    def test_live_benchmark_mode_runs_cli_worker_in_detached_worktree_with_merge_locked_out(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            head = git_repo(repo)
            suite_dir = root / "suite"
            (suite_dir / "tasks").mkdir(parents=True)
            (suite_dir / "suite.yaml").write_text(
                "name: live-suite\nconfigs:\n  - name: cfg-live\n    models:\n      implement: [\"gpt-6.2-sol-high\"]\n",
                encoding="utf-8",
            )
            (suite_dir / "tasks/t1.yaml").write_text(
                f"id: t1\ntitle: Live fix\nbase_sha: {head}\nrequest: Create artifact\ncheck: test -f artifact.txt\n",
                encoding="utf-8",
            )
            suite = load_suite(suite_dir)

            def fake_worker(args, cwd):
                if "Implement" in args[-1] or ("required" in args[-1] and "review" not in args[-1].lower()):
                    (cwd / "artifact.txt").write_text("ok\n", encoding="utf-8")
                    return worker_reply(args, {"implementation": "Created artifact."})
                if "Review" in args[-1] or "review" in args[-1].lower() and args[0] == "codex":
                    return worker_reply(args, {"review": "approve", "risk": DEFAULT_RISK})
                return worker_reply(args, {"triage": "implement"})

            def fake_judge(model, prompt, schema):
                return {"label": next(iter(["pass", "efficient", "compliant", "thorough"])), "reason": "good"}

            # Supply a judge returning a valid label per scorer dynamically
            from foreman.scoring import load_scorers
            scorers_by_name = load_scorers()

            def matched_judge(model, prompt, schema):
                sname = prompt.split("`")[1]
                return {"label": next(iter(scorers_by_name[sname].labels)), "reason": "verified"}

            results = run_benchmark(
                suite,
                repo_path=repo,
                dry_run=False,
                worker_runner=fake_worker,
                judge_runner=matched_judge,
                score_trials=True,
            )
            self.assertEqual(len(results), 1)
            self.assertTrue(results[0]["passed"])
            self.assertEqual(results[0]["stop"], "verified")
            self.assertFalse(results[0]["merge_armed"])
            self.assertEqual(results[0]["quality_score"], 1.0)
            # No leaked branches or worktrees in repo
            branches = subprocess.run(
                ["git", "branch", "--list"], cwd=repo, check=True, capture_output=True, text=True
            ).stdout.strip()
            self.assertEqual(branches, "* main")
            wt_list = subprocess.run(
                ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
            ).stdout
            self.assertEqual(wt_list.count("worktree "), 1)


if __name__ == "__main__":
    unittest.main()
