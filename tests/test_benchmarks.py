import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

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

            def git_out(cwd, *args):
                return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()

            def failing_runner(task, cfg, wt):
                self.assertIsNotNone(wt)
                self.assertTrue(wt.exists())
                self.assertEqual(wt.name, f"trial-{len(seen_paths):03d}")
                self.assertTrue((wt / ".git").is_dir())
                self.assertEqual(git_out(wt, "remote"), "")
                self.assertEqual(git_out(wt, "rev-parse", "HEAD"), head)
                self.assertNotEqual(subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=wt).returncode, 0)
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
            self.assertEqual(git_out(repo, "branch", "--list"), "* main")

    def test_summarize_keeps_delivery_and_judge_quality_separate(self):
        unscored = summarize_benchmark([
            {"config": "a", "passed": True, "quality_score": None, "quality_label": "unscored", "cost_usd": 0.1, "duration_ms": 100},
            {"config": "b", "passed": False, "quality_score": None, "quality_label": "insufficient_evidence", "cost_usd": 0.2, "duration_ms": 100},
        ])
        self.assertEqual({c["config"]: c["avg_quality"] for c in unscored["configs"]}, {"a": None, "b": None})
        self.assertIsNone(unscored["recommendations"]["highest_quality"])
        self.assertIsNone(unscored["recommendations"]["most_efficient"])
        self.assertEqual(unscored["recommendations"]["lowest_cost_passing"], "a")
        self.assertIn("quality=n/a", render_benchmark_text(unscored))
        self.assertIn("<svg", render_benchmark_html(unscored))

        mixed = summarize_benchmark([
            {"config": "unjudged", "passed": True, "quality_score": None, "quality_label": "unscored", "cost_usd": 0.1, "duration_ms": 100},
            {"config": "unjudged", "passed": False, "quality_score": None, "quality_label": "unscored", "cost_usd": 0.1, "duration_ms": 100},
            {"config": "judged", "passed": True, "quality_score": 0.9, "quality_label": "pass", "cost_usd": 0.1, "duration_ms": 100},
            {"config": "judged", "passed": False, "quality_score": 0.9, "quality_label": "pass", "cost_usd": 0.1, "duration_ms": 100},
        ])
        # A missing judge score is not replaced by delivery pass rate in ranking or dominance.
        self.assertEqual(mixed["recommendations"]["highest_quality"], "judged")
        self.assertEqual(mixed["recommendations"]["most_efficient"], "judged")
        self.assertEqual(sorted(mixed["pareto_frontier"]), ["judged", "unjudged"])

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
                f"id: t1\ntitle: Live fix\nbase_sha: {head}\nrequest: Create artifact\n"
                "check: test -f artifact.txt && test -z \"$GH_TOKEN\" && test \"$GIT_TERMINAL_PROMPT\" = 0\n",
                encoding="utf-8",
            )
            suite = load_suite(suite_dir)

            seen_env = []

            def fake_worker(args, cwd):
                if "Implement" in args[-1] or ("required" in args[-1] and "review" not in args[-1].lower()):
                    # A worker that wanders onto a named branch must not leave the trial on it.
                    subprocess.run(["git", "checkout", "-q", "-b", "leak"], cwd=cwd, check=True)
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

            from foreman import benchmarks
            real_commit = benchmarks._commit_detached_trial

            def commit_and_check(worktree, title):
                sha = real_commit(worktree, title)
                seen_env.append(subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=worktree).returncode)
                return sha

            with mock.patch.dict("os.environ", {"GH_TOKEN": "gho_" + "x" * 30}), \
                    mock.patch.object(benchmarks, "_commit_detached_trial", commit_and_check):
                results = run_benchmark(
                    suite,
                    repo_path=repo,
                    dry_run=False,
                    worker_runner=fake_worker,
                    judge_runner=matched_judge,
                    score_trials=True,
                )
            self.assertEqual(len(results), 1)
            self.assertTrue(seen_env)
            self.assertTrue(all(code != 0 for code in seen_env))
            self.assertTrue(results[0]["passed"])
            self.assertTrue(results[0]["delivery_passed"])
            self.assertTrue(results[0]["judge_passed"])
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


    def test_default_worker_runner_strips_push_credentials(self):
        from foreman.benchmarks import run_isolated_worker_command

        leaked = {name: "secret-value-123" for name in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT", "GIT_ASKPASS", "SSH_AUTH_SOCK")}
        probe = 'echo "${GITHUB_TOKEN:-}${GH_TOKEN:-}${GITHUB_PAT:-}${GIT_ASKPASS:-}${SSH_AUTH_SOCK:-}|$GIT_TERMINAL_PROMPT|$GIT_CONFIG_NOSYSTEM"'
        with TemporaryDirectory() as tmpdir, mock.patch.dict("os.environ", leaked):
            out = run_isolated_worker_command(["sh", "-c", probe], Path(tmpdir)).stdout.strip()
        self.assertEqual(out, "|0|1")

    def test_isolated_env_strips_git_overrides_and_ignores_user_git_config(self):
        from foreman.benchmarks import isolated_env

        leaked = {
            "GH_ENTERPRISE_TOKEN": "t", "GH_HOST": "h", "GH_CONFIG_DIR": "d", "SSH_AGENT_PID": "1",
            "GIT_SSH": "s", "GIT_SSH_COMMAND": "s", "GIT_CREDENTIAL_HELPER": "c", "GIT_DIR": "g",
            "GIT_WORK_TREE": "w", "GIT_INDEX_FILE": "i", "GIT_COMMON_DIR": "c", "GIT_CONFIG_PARAMETERS": "p",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": "store",
        }
        with mock.patch.dict("os.environ", leaked):
            env = isolated_env()
        self.assertFalse(set(leaked) & set(env))
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_isolated_env_keeps_only_allowlisted_names(self):
        from foreman.benchmarks import isolated_env

        leaked = {"GITHUB_APP_TOKEN": "t", "GH_TEST_TOKEN": "t", "GIT_SSH_VARIANT": "ssh", "SOME_NEW_SECRET": "s"}
        with mock.patch.dict("os.environ", {**leaked, "HOME": "/home/example", "LC_ALL": "C"}):
            env = isolated_env()
        self.assertFalse(set(leaked) & set(env))
        self.assertEqual((env["HOME"], env["LC_ALL"]), ("/home/example", "C"))

    def test_trial_inspection_ignores_caller_git_env_and_status_config(self):
        from foreman.benchmarks import _inspect_trial

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            trial, other = root / "trial", root / "other"
            trial.mkdir()
            other.mkdir()
            head = git_repo(trial)
            git_repo(other)
            subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=trial, check=True)
            (trial / "hidden.txt").write_text("x\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {"GIT_DIR": str(other / ".git")}):
                self.assertEqual(_inspect_trial(trial), (head, False))

    def test_default_trial_judge_runs_isolated_in_the_trial_repo(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            head = git_repo(repo)
            suite_dir = root / "suite"
            (suite_dir / "tasks").mkdir(parents=True)
            (suite_dir / "suite.yaml").write_text("name: s\nconfigs:\n  - name: cfg\n", encoding="utf-8")
            (suite_dir / "tasks/t1.yaml").write_text(
                f"id: t1\nbase_sha: {head}\nrequest: Test change\ncheck: exit 0\n", encoding="utf-8"
            )
            suite = load_suite(suite_dir)
            judge_calls = []
            real_run = subprocess.run

            def spy_run(args, *pargs, **kwargs):
                if isinstance(args, list) and args and args[0] in ("claude", "codex", "grok"):
                    if "independent LLM judge" not in args[-1]:
                        return worker_reply(args, {"triage": "ask", "question": "Which file?"})
                    judge_calls.append((kwargs.get("cwd"), kwargs.get("env")))
                    reply = {"label": "insufficient_evidence", "reason": "n/a"}
                    if args[0] == "codex":
                        Path(args[args.index("-o") + 1]).write_text(json.dumps(reply), encoding="utf-8")
                        return subprocess.CompletedProcess(args, 0, "", "")
                    return subprocess.CompletedProcess(args, 0, json.dumps({"structured_output": reply}), "")
                return real_run(args, *pargs, **kwargs)

            with mock.patch.dict("os.environ", {"GH_TOKEN": "gho_" + "x" * 30}), \
                    mock.patch.object(subprocess, "run", spy_run):
                run_benchmark(suite, repo_path=repo, dry_run=False, score_trials=True)
            self.assertTrue(judge_calls)
            for cwd, env in judge_calls:
                self.assertTrue(Path(cwd).name.startswith("trial-"), cwd)
                self.assertIsNotNone(env)
                self.assertNotIn("GH_TOKEN", env)

    def test_cli_bench_runs_workers_with_isolated_env_by_default(self):
        from foreman import benchmarks

        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            head = git_repo(repo)
            suite_dir = root / "suite"
            (suite_dir / "tasks").mkdir(parents=True)
            (suite_dir / "suite.yaml").write_text("name: s\nconfigs:\n  - name: cfg\n", encoding="utf-8")
            (suite_dir / "tasks/t1.yaml").write_text(
                f"id: t1\nbase_sha: {head}\nrequest: Test change\ncheck: exit 0\n", encoding="utf-8"
            )
            worker_envs = []
            real_run = subprocess.run

            def spy_run(args, *pargs, **kwargs):
                if isinstance(args, list) and args and args[0] in ("claude", "codex", "grok"):
                    worker_envs.append(kwargs.get("env"))
                    return worker_reply(args, {"triage": "ask", "question": "Which file?"})
                return real_run(args, *pargs, **kwargs)

            with mock.patch.dict("os.environ", {"GH_TOKEN": "gho_" + "x" * 30}), \
                    mock.patch.object(benchmarks.subprocess, "run", spy_run), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                main(["bench", str(suite_dir), "--repo-path", str(repo), "--format", "json"])
            self.assertTrue(worker_envs)
            for env in worker_envs:
                self.assertIsNotNone(env)
                self.assertNotIn("GH_TOKEN", env)
                self.assertEqual(env["GIT_CONFIG_GLOBAL"], "/dev/null")


if __name__ == "__main__":
    unittest.main()
