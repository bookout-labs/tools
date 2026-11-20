import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest import mock

from foreman.cli import main
from foreman.scoring import (
    ScorerError,
    ScorerSpec,
    calibration_report,
    load_scorers,
    parse_scorer_file,
    score_run,
    select_judge_model,
    should_sample,
)
from foreman.telemetry import TelemetryStore, render_stats_text
from tests.test_runner import FakeGitHub, clean_worktree, git_repo, worker_reply


class ScorersTest(unittest.TestCase):
    def test_builtin_five_scorers_parse_and_validate_strictly(self):
        scorers = load_scorers()
        self.assertEqual(
            sorted(scorers),
            ["code-quality", "efficiency", "procedure-compliance", "review-quality", "task-compliance"],
        )
        for spec in scorers.values():
            self.assertIn("insufficient_evidence", spec.labels)
            self.assertEqual(spec.labels["insufficient_evidence"]["score"], 0.5)
            self.assertGreater(spec.sampling_rate, 0.0)
            self.assertLessEqual(spec.sampling_rate, 1.0)

        with TemporaryDirectory() as tmpdir:
            bad = Path(tmpdir) / "scorer.md"
            bad.write_text("---\nname: broken\ndescription: missing labels\nstages: [implement]\n---\nHi\n", encoding="utf-8")
            with self.assertRaises(ScorerError):
                parse_scorer_file(bad)

    def test_cross_family_judge_selection_and_deterministic_sampling(self):
        scorers = load_scorers()
        tc = scorers["task-compliance"]  # default model claude-sonnet-5-5-high
        self.assertEqual(select_judge_model(tc, {"implement": "gpt-6.2-sol-high"}), "claude-sonnet-5-5-high")
        self.assertEqual(select_judge_model(tc, {"implement": "claude-opus-5-5-high"}), "grok-4.7-high")

        rq = scorers["review-quality"]  # default model grok-4.7-high, stages=["review"]
        self.assertEqual(
            select_judge_model(rq, {"implement": "gpt-6.2-sol-high", "review": "grok-4.7-high"}),
            "claude-sonnet-5-5-high",
        )

        # Deterministic sampling reproducibility
        half_sampler = ScorerSpec(
            name="half", description="d", stages=("implement",),
            labels=tc.labels, passing_score=0.8, sampling_rate=0.5,
            model="claude-sonnet-5-5-high", self_improvement=True, prompt="p", path=tc.path,
        )
        decisions = {rid: should_sample(rid, half_sampler) for rid in [f"run-{i}" for i in range(40)]}
        self.assertEqual(decisions, {rid: should_sample(rid, half_sampler) for rid in [f"run-{i}" for i in range(40)]})
        self.assertTrue(any(decisions.values()))
        self.assertFalse(all(decisions.values()))

    def test_insufficient_evidence_never_passes_and_excludes_from_averages_and_rescore_upserts(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = TelemetryStore(root / "tel.sqlite3")
            r1 = store.record_run({
                "repo": "Example/Engine", "issue": 1, "stop": "verified",
                "pr_url": "https://github.com/Example/Engine/pull/1",
                "pull_request": "https://github.com/Example/Engine/pull/1",
                "models_used": {"implement": "gpt-6.2-sol-high"},
                "path": ["triage", "implement", "verify", "review", "done"],
            }, host="studio")
            r2 = store.record_run({
                "repo": "Example/Engine", "issue": 2, "stop": "verified",
                "models_used": {"implement": "gpt-6.2-sol-high"},
                "path": ["triage", "implement", "verify", "review", "done"],
            }, host="studio")
            store.enrich_github_outcomes(lambda *a: {
                "state": "MERGED", "createdAt": "2026-11-20T10:00:00Z", "mergedAt": "2026-11-20T10:05:00Z",
                "additions": 10, "deletions": 2, "changedFiles": 1, "commits": [{"oid": "a" * 40}], "comments": [], "reviews": [],
            })

            # Score using a rubric with passingScore=0.4 to prove insufficient_evidence STILL fails even when 0.5 >= 0.4!
            low_bar = ScorerSpec(
                name="lenient", description="d", stages=("implement",),
                labels={
                    "pass": {"value": "pass", "description": "ok", "score": 1.0},
                    "fail": {"value": "fail", "description": "bad", "score": 0.0},
                    "insufficient_evidence": {"value": "insufficient_evidence", "description": "thin", "score": 0.5},
                },
                passing_score=0.4, sampling_rate=1.0, model="claude-sonnet-5-5-high",
                self_improvement=True, prompt="Judge", path=Path("dummy.md"),
            )

            mode = {"r2": "insufficient_evidence"}

            def fake_judge(model, prompt, schema):
                if r1 in prompt:
                    return {"label": "pass", "reason": "Clean change."}
                return {"label": mode["r2"], "reason": "Missing logs."}

            score_run(store, r1, [low_bar], judge=fake_judge)
            res2 = score_run(store, r2, [low_bar], judge=fake_judge)
            self.assertFalse(res2[0]["passed"])
            self.assertEqual(res2[0]["label"], "insufficient_evidence")

            stats = store.compute_stats()
            card = stats["scorers"]["lenient"]
            self.assertEqual(card["total"], 2)
            self.assertEqual(card["evaluable"], 1)
            self.assertEqual(card["insufficient_evidence"], 1)
            self.assertEqual(card["pass_rate"], 1.0)
            self.assertEqual(card["avg_score"], 1.0)
            self.assertIn("Scorers:", render_stats_text(stats))

            cal = calibration_report(store, human_labels={r1: {"lenient": "pass"}, r2: {"lenient": "fail"}})
            self.assertEqual(cal["scorers"]["lenient"]["human_compared"], 1)
            self.assertEqual(cal["scorers"]["lenient"]["human_agreed"], 1)
            self.assertEqual(cal["scorers"]["lenient"]["github_agreement"], 1.0)

            # Re-score with force=True upserts instead of duplicating
            mode["r2"] = "fail"
            score_run(store, r2, [low_bar], judge=fake_judge, force=True)
            self.assertEqual(len(store.list_scores(run_id=r2)), 1)
            self.assertEqual(store.list_scores(run_id=r2)[0]["label"], "fail")

    def test_judge_avoids_every_builder_family_and_skips_unobserved_stages(self):
        tc = load_scorers()["task-compliance"]
        builders = ["claude-opus-5-5-high", "gpt-6.2-sol-high"]
        self.assertEqual(select_judge_model(tc, {"implement": "gpt-6.2-sol-high"}, implement_models=builders), "grok-4.7-high")

        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({
                "repo": "Example/Engine", "issue": 3, "stop": "verified",
                "models_used": {"implement": "gpt-6.2-sol-high"},
                "implement_models": builders,
                "path": ["triage", "implement", "verify", "done"],
            })
            calls = []

            def judge(model, prompt, schema):
                name = prompt.split("`")[1]
                calls.append((name, model))
                return {"label": next(iter(load_scorers()[name].labels)), "reason": "ok"}

            scored = score_run(store, rid, judge=judge, respect_sampling=False)
            names = {row["scorer"] for row in scored}
            self.assertNotIn("review-quality", names)
            self.assertIn("task-compliance", names)
            self.assertEqual(dict(calls)["task-compliance"], "grok-4.7-high")

    def test_judge_refuses_when_every_family_built_the_change(self):
        tc = load_scorers()["task-compliance"]
        builders = ["claude-opus-5-5-high", "gpt-6.2-sol-high", "grok-4.7-high"]
        with self.assertRaisesRegex(ScorerError, "no independent judge family"):
            select_judge_model(tc, {"implement": "gpt-6.2-sol-high"}, implement_models=builders)

    def test_review_judge_never_reuses_the_reviewer_family(self):
        rq = load_scorers()["review-quality"]
        builders = ["claude-opus-5-5-high", "gpt-6.2-sol-high"]
        with self.assertRaisesRegex(ScorerError, "no independent judge family"):
            select_judge_model(rq, {"implement": "gpt-6.2-sol-high", "review": "grok-4.7-high"},
                               implement_models=builders)

    def test_run_without_observed_stages_skips_stage_scoped_scorers(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({"repo": "Example/Engine", "issue": 4, "stop": "verified"})
            calls = []

            def judge(model, prompt, schema):
                calls.append(prompt)
                return {"label": "pass", "reason": "ok"}

            self.assertEqual(score_run(store, rid, judge=judge, respect_sampling=False), [])
            self.assertEqual(calls, [])

    def test_calibration_only_compares_terminal_pull_requests(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            outcomes = {"1": "OPEN", "2": "CLOSED", "3": "MERGED"}
            for n in outcomes:
                rid = store.record_run({
                    "repo": "Example/Engine", "issue": int(n), "stop": "verified",
                    "pull_request": f"https://github.com/Example/Engine/pull/{n}",
                }, run_id=f"run-{n}")
                store.upsert_score(run_id=rid, scorer="s", label="pass", score=1.0, passed=True)
            store.enrich_github_outcomes(lambda repo, url: {"state": outcomes[url.rsplit("/", 1)[1]]})
            bucket = calibration_report(store)["scorers"]["s"]
            self.assertEqual(bucket["github_compared"], 2)
            self.assertEqual(bucket["github_agreed"], 1)

    def test_score_command_reads_the_runs_pinned_head_not_the_checkout(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            git_repo(repo)

            def commit(name, text):
                (repo / name).write_text(text, encoding="utf-8")
                subprocess.run(["git", "add", name], cwd=repo, check=True)
                subprocess.run(["git", "commit", "-q", "-m", name], cwd=repo, check=True)
                return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                      capture_output=True, text=True).stdout.strip()

            subprocess.run(["git", "checkout", "-q", "-b", "8-feature"], cwd=repo, check=True)
            pinned = commit("delivered.txt", "delivered-change\n")
            commit("later.txt", "later-change\n")
            db = root / "tel.sqlite3"
            rid = TelemetryStore(db).record_run(
                {"repo": "Example/Engine", "issue": 8, "stop": "verified", "head_sha": pinned,
                 "path": ["triage", "implement", "verify", "review", "done"],}
            )
            prompts: list[str] = []

            def judge(model, prompt, schema):
                prompts.append(prompt)
                return {"label": next(iter(load_scorers()[prompt.split("`")[1]].labels)), "reason": "ok"}

            prev = Path.cwd()
            try:
                os.chdir(repo)
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["score", rid, "--db", str(db), "--all-samples"], judge_runner=judge), 0)
            finally:
                os.chdir(prev)
            self.assertTrue(prompts)
            self.assertTrue(all("delivered-change" in p for p in prompts))
            self.assertFalse(any("later-change" in p for p in prompts))

    def test_cli_scorers_and_score_and_run_with_score_flag(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            git_repo(repo)
            db = root / "tel.sqlite3"
            ckpt = root / "ckpt.sqlite3"

            def worker(args, cwd):
                if args[0] == "claude":
                    return subprocess.CompletedProcess(
                        args, 0,
                        json.dumps({
                            "structured_output": {"triage": "implement"},
                            "session_id": "s-1",
                            "modelUsage": {"claude-opus-5-5": {"inputTokens": 100, "outputTokens": 20}},
                        }),
                        "",
                    )
                out_path = Path(args[args.index("-o") + 1])
                if "Implement" in args[-1] or ("required" in args[-1] and "review" not in args[-1].lower()):
                    (repo / "f.txt").write_text("ok\n", encoding="utf-8")
                    out_path.write_text(json.dumps({"implementation": "done"}), encoding="utf-8")
                else:
                    out_path.write_text(json.dumps({"review": "approve", "risk": {"alignment": "ok", "impact": "low", "verification": "ok", "rollback": "easy", "error_detection": "ok", "level": "low"}}), encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, "", "model: gpt-6.2-sol\n")

            prompts: list[str] = []

            def judge(model, prompt, schema):
                prompts.append(prompt)
                return {"label": next(iter(load_scorers()[prompt.split("`")[1]].labels)), "reason": "ok"}

            out_list = io.StringIO()
            with contextlib.redirect_stdout(out_list):
                self.assertEqual(main(["scorers", "--json"]), 0)
            self.assertEqual(len(json.loads(out_list.getvalue())), 5)

            prev = Path.cwd()
            try:
                os.chdir(repo)
                pinned = lambda cwd, sha: f"diff --git a/x b/x\n+PINNED-DIFF-{sha}\n"
                with contextlib.redirect_stdout(io.StringIO()), mock.patch("foreman.cli._pinned_diff", pinned):
                    code = main(
                        ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                         "--check", "exit 0", "--telemetry", str(db), "--score"],
                        worker_runner=worker,
                        judge_runner=judge,
                        checkpoint=ckpt,
                        github=FakeGitHub(),
                        run_gh=lambda cmd: 0,
                        inspect_worktree=clean_worktree,
                        read_merge_hold=lambda r: None,
                    )
                self.assertEqual(code, 0)
            finally:
                os.chdir(prev)

            store = TelemetryStore(db)
            runs = store.list_runs()
            self.assertEqual(len(runs), 1)
            scores = store.list_scores(run_id=runs[0]["run_id"])
            self.assertGreaterEqual(len(scores), 4)
            self.assertTrue(any("PINNED-DIFF-" + "a" * 40 in p for p in prompts))

            out_cal = io.StringIO()
            with contextlib.redirect_stdout(out_cal):
                self.assertEqual(main(["score", "--db", str(db), "--calibration"]), 0)
            self.assertIn("Calibration", out_cal.getvalue())


if __name__ == "__main__":
    unittest.main()
