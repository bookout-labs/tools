import contextlib
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.cli import main
from foreman.delivery import compile_delivery, run_delivery
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import DEFAULT_RISK
from foreman.telemetry import (
    TelemetryStore,
    estimate_usage_cost,
    extract_usage_tokens,
    load_pricing,
    pr_size_bucket,
    render_stats_html,
    render_stats_text,
)
from tests.test_runner import FakeGitHub, clean_worktree, git_repo, worker_reply


class TelemetryPricingTest(unittest.TestCase):
    def test_extract_tokens_and_estimate_cost_for_claude_and_codex(self):
        pricing = load_pricing()
        codex_meta = {
            "provider": "gpt",
            "model": "gpt-6.2-sol-high",
            "slug": "gpt-6.2-sol",
            "duration_ms": 1500,
            "total_cost_usd": None,
            "model_usage": {
                "gpt-6.2-sol": {
                    "input_tokens": 5000,
                    "cached_input_tokens": 4000,
                    "output_tokens": 500,
                }
            },
        }
        cost, toks, estimated = estimate_usage_cost(codex_meta, pricing)
        self.assertTrue(estimated)
        self.assertEqual(toks["total_tokens"], 5500)
        self.assertEqual(toks["input_tokens"], 1000)
        self.assertEqual(toks["cached_input_tokens"], 4000)
        rate = pricing["models"].get("gpt-6.2-sol") or pricing["families"]["gpt"]
        expected = (1000 * rate["input_per_mtok"] + 4000 * rate["cached_input_per_mtok"] + 500 * rate["output_per_mtok"]) / 1e6
        self.assertAlmostEqual(cost, round(expected, 6))
        # A provider hint alone marks Codex usage as cache-inclusive.
        hinted = extract_usage_tokens({"input_tokens": 5000, "cached_tokens": 4000, "output_tokens": 500}, provider="gpt")
        self.assertEqual((hinted["input_tokens"], hinted["total_tokens"]), (1000, 5500))

        claude_meta = {
            "provider": "claude",
            "model": "claude-opus-5-5-high",
            "slug": "claude-opus-5-5",
            "duration_ms": 900,
            "total_cost_usd": 0.42,
            "model_usage": {
                "claude-opus-5-5": {
                    "inputTokens": 200,
                    "cacheReadInputTokens": 800,
                    "cacheCreationInputTokens": 100,
                    "outputTokens": 50,
                }
            },
        }
        c_cost, c_toks, c_est = estimate_usage_cost(claude_meta, pricing)
        self.assertFalse(c_est)
        self.assertEqual(c_cost, 0.42)
        self.assertEqual(c_toks["cached_input_tokens"], 800)
        self.assertEqual(c_toks["input_tokens"], 200)
        self.assertEqual(c_toks["total_tokens"], 1150)
        self.assertEqual(extract_usage_tokens(None)["total_tokens"], 0)

    def test_pr_size_buckets(self):
        self.assertEqual(pr_size_bucket(None, None), "unknown")
        self.assertEqual(pr_size_bucket(10, 5, 1), "S")
        self.assertEqual(pr_size_bucket(120, 40, 4), "M")
        self.assertEqual(pr_size_bucket(300, 100, 12), "L")
        self.assertEqual(pr_size_bucket(700, 200, 5), "XL")


class TelemetryStoreAndSyncTest(unittest.TestCase):
    def _sample_state(self, *, issue=10, pr_url="", request="Fix bug for owner@example.invalid"):
        return {
            "repo": "Example/Engine",
            "issue": issue,
            "request": request,
            "stop": "verified",
            "verify_rounds": 1,
            "review_rounds": 0,
            "pull_request": pr_url,
            "head_sha": "a" * 40,
            "models_used": {"triage": "claude-opus-5-5-high", "implement": "gpt-6.2-sol-high"},
            "sessions": {"implement": "thread-123"},
            "usage_records": [
                {
                    "node": "triage",
                    "model": "claude-opus-5-5-high",
                    "slug": "claude-opus-5-5",
                    "provider": "claude",
                    "status": "ran",
                    "duration_ms": 1100,
                    "total_cost_usd": 0.15,
                    "model_usage": {"claude-opus-5-5": {"inputTokens": 500, "cacheReadInputTokens": 1500, "outputTokens": 100}},
                },
                {
                    "node": "implement",
                    "model": "gpt-6.2-sol-high",
                    "slug": "gpt-6.2-sol",
                    "provider": "gpt",
                    "status": "ran",
                    "duration_ms": 2400,
                    "total_cost_usd": None,
                    "session_id": "thread-123",
                    "model_usage": {"gpt-6.2-sol": {"input_tokens": 4000, "cached_input_tokens": 3000, "output_tokens": 400}},
                },
            ],
            "attempts": [],
            "path": ["triage", "implement", "verify", "review", "merge", "done"],
        }

    def test_record_enrich_sync_and_render_stats(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            studio_db = TelemetryStore(root / "studio.sqlite3")
            laptop_db = TelemetryStore(root / "laptop.sqlite3")

            rid1 = studio_db.record_run(
                self._sample_state(issue=10, pr_url="https://github.com/Example/Engine/pull/10"),
                host="mac-studio",
                started_at="2026-11-20T10:00:00Z",
                finished_at="2026-11-20T10:05:00Z",
            )
            rid2 = laptop_db.record_run(
                self._sample_state(issue=11, pr_url="https://github.com/Example/Engine/pull/11"),
                host="macbook-pro",
                started_at="2026-11-20T11:00:00Z",
                finished_at="2026-11-20T11:06:00Z",
            )
            saved1 = studio_db.get_run(rid1)
            self.assertNotIn("owner@example.invalid", json.dumps(saved1["state"]))
            self.assertEqual(len(studio_db.list_stage_calls(rid1)), 2)

            def fake_pr_fetcher(pr_repo, pr_url):
                if pr_url.endswith("/10"):
                    return {
                        "state": "MERGED",
                        "createdAt": "2026-11-20T10:02:00Z",
                        "mergedAt": "2026-11-20T10:12:00Z",
                        "additions": 25,
                        "deletions": 10,
                        "changedFiles": 2,
                        "commits": [{"oid": "a" * 40}],
                        "comments": [{"author": {"login": "bot-user"}, "body": "Reviewed by gpt-6.2-sol-high.\nVerdict: approve."}],
                        "reviews": [],
                    }
                return {
                    "state": "MERGED",
                    "createdAt": "2026-11-20T11:02:00Z",
                    "mergedAt": "2026-11-20T11:32:00Z",
                    "additions": 140,
                    "deletions": 50,
                    "changedFiles": 4,
                    "commits": [{"oid": "a" * 40}, {"oid": "b" * 40}],
                    "comments": [{"author": {"login": "reviewer"}, "body": "Please rename this helper."}],
                    "reviews": [{"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "body": "Fix name"}],
                }

            self.assertEqual(studio_db.enrich_github_outcomes(fake_pr_fetcher), 1)
            self.assertEqual(laptop_db.enrich_github_outcomes(fake_pr_fetcher), 1)

            sync_file = root / "sync.jsonl"
            laptop_db.export_jsonl(sync_file)
            imported = studio_db.import_jsonl(sync_file)
            self.assertEqual(imported["runs"], 1)
            self.assertEqual(imported["stage_calls"], 2)
            # Re-importing the same JSONL is idempotent
            studio_db.import_jsonl(sync_file)
            self.assertEqual(len(studio_db.list_runs()), 2)
            self.assertIsNotNone(studio_db.get_run(rid2))

            stats = studio_db.compute_stats()
            self.assertEqual(stats["total_runs"], 2)
            self.assertEqual(stats["merged_runs"], 2)
            self.assertEqual(stats["zero_touch_runs"], 1)
            self.assertEqual(stats["autonomy_rate"], 0.5)
            self.assertEqual(stats["by_size_bucket"]["S"]["zero_touch"], 1)
            self.assertEqual(stats["by_size_bucket"]["M"]["zero_touch"], 0)
            self.assertIn("mac-studio", stats["by_host"])
            self.assertIn("macbook-pro", stats["by_host"])

            text = render_stats_text(stats)
            self.assertIn("Zero-touch: 1 (50.0%)", text)
            self.assertIn("gpt-6.2-sol-high", text)
            html_out = render_stats_html(stats)
            self.assertIn("<svg", html_out)
            self.assertIn("Autonomy", html_out)
            self.assertNotIn("cdn.", html_out)

    def test_nested_state_redaction_and_export_rescrub(self):
        secret = "ghp_0123456789abcdefghij0123456789"
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({
                "repo": "Example/Engine",
                "issue": 3,
                "stop": "verify_cap",
                "attempts": [{"node": "implement", "detail": f"used {secret}"}],
                "risk": {"alignment": "ask owner@example.invalid"},
                "review_comments": [{"path": "a.py", "body": "call (555) 234-5678"}],
                "sessions": {"implement": f"s-{secret}"},
            })
            saved = json.dumps(store.get_run(rid)["state"])
            for leaked in (secret, "owner@example.invalid", "(555) 234-5678"):
                self.assertNotIn(leaked, saved)

            # Rows written before a redaction fix must still leave the host scrubbed.
            raw_state = json.dumps({"halt_detail": f"halt {secret}", "check_output": "mail owner@example.invalid"})
            with store._connect() as conn:
                conn.execute(
                    "UPDATE runs SET state_json = ?, failure_reason = ? WHERE run_id = ?",
                    (raw_state, f"reason {secret}", rid),
                )
                conn.execute(
                    "INSERT INTO scores (run_id, scorer, label, score, passed, reason, judge_model, scored_at)"
                    " VALUES (?, 's', 'fail', 0.0, 0, ?, 'm', '2026-11-20T00:00:00Z')",
                    (rid, f"judge saw {secret}"),
                )
            exported = store.export_jsonl()
            self.assertNotIn(secret, exported)
            self.assertNotIn("owner@example.invalid", exported)
            self.assertIn("[REDACTED]", exported)

    def test_long_request_title_is_redacted_before_clipping(self):
        secret = "ghp_0123456789abcdefghij0123456789"
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({
                "repo": "Example/Engine", "issue": 3, "stop": "verified",
                "request": "a" * 149 + " " + secret + " tail\nsecond line",
            })
            title = store.get_run(rid)["state"]["request_title"]
        self.assertNotIn(secret[:10], title)
        self.assertLessEqual(len(title), 160)
        self.assertTrue(title.startswith("a" * 149 + " [REDACTED"))

    def test_export_rescrubs_stage_call_rows(self):
        secret = "ghp_0123456789abcdefghij0123456789"
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({"repo": "Example/Engine", "issue": 3, "stop": "verified"})
            with store._connect() as conn:
                conn.execute(
                    "INSERT INTO stage_calls (call_id, run_id, host, seq, node, model, status, session_id)"
                    " VALUES ('c1', ?, 'h', 0, 'implement', 'm', 'ok', ?)",
                    (rid, f"s-{secret}-owner@example.invalid"),
                )
            exported = store.export_jsonl()
        self.assertIn('"type": "stage_call"', exported)
        self.assertNotIn(secret, exported)
        self.assertNotIn("owner@example.invalid", exported)

    def test_import_never_lets_an_older_export_overwrite_stage_calls(self):
        def lines(finished_at, status, cost):
            # Stage call first: freshness must hold whatever order the snapshot lines arrive in.
            call = {"type": "stage_call", "call_id": "c1", "run_id": "run-9", "host": "h", "seq": 0,
                    "node": "implement", "model": "m", "provider": "gpt", "status": status,
                    "duration_ms": 1, "session_id": "", "cost_usd": cost, "input_tokens": 0,
                    "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0}
            run = {"type": "run", "run_id": "run-9", "host": "h", "repo": "Example/Engine", "issue": 9,
                   "started_at": finished_at, "finished_at": finished_at, "stop": "verified"}
            return "\n".join(json.dumps(x) for x in (call, run))

        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")

            def call_row():
                with store._connect() as conn:
                    row = conn.execute("SELECT status, cost_usd FROM stage_calls WHERE call_id = 'c1'").fetchone()
                return tuple(row)

            self.assertEqual(store.import_jsonl(lines("2026-11-20T10:00:00Z", "ok", 1.0))["stage_calls"], 1)
            self.assertEqual(call_row(), ("ok", 1.0))
            self.assertEqual(store.import_jsonl(lines("2026-11-20T09:00:00Z", "failed", 9.0))["stage_calls"], 0)
            self.assertEqual(call_row(), ("ok", 1.0))
            store.import_jsonl(lines("2026-11-20T11:00:00Z", "retried", 2.0))
            self.assertEqual(call_row(), ("retried", 2.0))

    def test_enrich_uses_newest_run_head_for_a_shared_pr(self):
        pr = "https://github.com/Example/Engine/pull/8"
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            for head, finished in (("a" * 40, "2026-11-20T09:00:00Z"), ("b" * 40, "2026-11-20T10:00:00Z")):
                store.record_run({"repo": "Example/Engine", "issue": 8, "stop": "verified",
                                  "head_sha": head, "pull_request": pr}, finished_at=finished)
            store.enrich_github_outcomes(lambda *a: {"state": "MERGED", "commits": [
                {"oid": sha * 40, "authors": [{"login": "owner"}]} for sha in "abc"
            ]})
            self.assertEqual([r["gh_human_commits"] for r in store.list_runs()], [1, 1])

    def test_same_second_runs_on_a_shared_pr_use_the_later_head(self):
        pr = "https://github.com/Example/Engine/pull/8"
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            for head in ("a" * 40, "b" * 40):
                store.record_run({"repo": "Example/Engine", "issue": 8, "stop": "verified",
                                  "head_sha": head, "pull_request": pr}, finished_at="2026-11-20T10:00:00Z")
            self.assertEqual(store.list_runs(repo="Example/Engine")[0]["head_sha"], "b" * 40)
            store.enrich_github_outcomes(lambda *a: {"state": "MERGED", "commits": [
                {"oid": sha * 40, "authors": [{"login": "owner"}]} for sha in "abc"
            ]}, repo="Example/Engine")
            self.assertEqual([r["gh_human_commits"] for r in store.list_runs()], [1, 1])

    def test_export_decodes_and_scrubs_every_json_column(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({"repo": "Example/Engine", "issue": 3, "stop": "verified"})
            # An escaped @ hides the address from a raw-text scrub.
            with store._connect() as conn:
                conn.execute(
                    "UPDATE runs SET models_json = ? WHERE run_id = ?",
                    ('{"implement": "owner\\u0040example.invalid"}', rid),
                )
            run_line = json.loads(store.export_jsonl().splitlines()[0])
        self.assertNotIn("example.invalid", json.dumps(json.loads(run_line["models_json"])))

    def test_import_drops_stage_calls_a_newer_snapshot_no_longer_lists(self):
        def snapshot(finished_at, call_ids):
            run = {"type": "run", "run_id": "run-9", "host": "h", "repo": "Example/Engine", "issue": 9,
                   "started_at": finished_at, "finished_at": finished_at, "stop": "verified"}
            calls = [{"type": "stage_call", "call_id": cid, "run_id": "run-9", "host": "h", "seq": i,
                      "node": "implement", "model": "m", "provider": "gpt", "status": "ok", "duration_ms": 1,
                      "session_id": "", "cost_usd": 1.0, "input_tokens": 0, "cached_input_tokens": 0,
                      "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0}
                     for i, cid in enumerate(call_ids)]
            return "\n".join(json.dumps(x) for x in (run, *calls))

        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            store.import_jsonl(snapshot("2026-11-20T10:00:00Z", ["c1", "c2"]))
            store.import_jsonl(snapshot("2026-11-20T11:00:00Z", ["c1"]))
            self.assertEqual([c["call_id"] for c in store.list_stage_calls("run-9")], ["c1"])
            store.import_jsonl(snapshot("2026-11-20T09:00:00Z", ["c1", "c3"]))
            self.assertEqual([c["call_id"] for c in store.list_stage_calls("run-9")], ["c1"])

    def test_insufficient_evidence_never_stored_as_passed(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({"repo": "Example/Engine", "issue": 4, "stop": "verified"})
            store.upsert_score(run_id=rid, scorer="a", label="insufficient_evidence", score=0.5, passed=True)
            store.import_jsonl(json.dumps({
                "type": "score", "run_id": rid, "scorer": "b", "label": "insufficient_evidence",
                "score": 0.5, "passed": 1, "reason": "", "judge_model": "m", "scored_at": "2026-11-20T00:00:00Z",
            }))
            self.assertEqual([row["passed"] for row in store.list_scores(run_id=rid)], [0, 0])

    def test_import_keeps_fresher_local_runs_enrichment_and_scores(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            local = TelemetryStore(root / "local.sqlite3")
            rid = local.record_run(
                {"repo": "Example/Engine", "issue": 5, "stop": "verified",
                 "pull_request": "https://github.com/Example/Engine/pull/5"},
                run_id="run-5", finished_at="2026-11-20T10:00:00Z",
            )
            local.enrich_github_outcomes(lambda *a: {"state": "MERGED", "mergedAt": "2026-11-20T11:00:00Z"})
            local.upsert_score(run_id=rid, scorer="s", label="pass", score=1.0, passed=True,
                               scored_at="2026-11-20T12:00:00Z")
            fresh = local.get_run(rid)

            stale_run = {k: v for k, v in fresh.items() if k not in {"models_used", "state"}}
            stale_run.update(type="run", stop="verify_cap", finished_at="2026-11-20T09:00:00Z",
                             gh_state="OPEN", gh_merged=0, gh_enriched_at="2026-01-01T00:00:00Z")
            stale_score = {"type": "score", "run_id": rid, "scorer": "s", "label": "fail", "score": 0.0,
                           "passed": 0, "reason": "", "judge_model": "m", "scored_at": "2026-11-20T08:00:00Z"}
            local.import_jsonl("\n".join(json.dumps(x) for x in (stale_run, stale_score)))
            kept = local.get_run(rid)
            self.assertEqual((kept["stop"], kept["gh_state"], kept["gh_merged"]), ("verified", "MERGED", 1))
            self.assertEqual(local.list_scores(run_id=rid)[0]["label"], "pass")

            newer_core = dict(stale_run, stop="needs_human", finished_at="2026-11-21T00:00:00Z")
            newer_gh = dict(newer_core, gh_state="CLOSED", gh_merged=0, gh_enriched_at="2099-01-01T00:00:00Z")
            local.import_jsonl(json.dumps(newer_core))
            self.assertEqual((local.get_run(rid)["stop"], local.get_run(rid)["gh_state"]), ("needs_human", "MERGED"))
            local.import_jsonl(json.dumps(newer_gh))
            self.assertEqual(local.get_run(rid)["gh_state"], "CLOSED")
            local.import_jsonl(json.dumps(dict(stale_score, scored_at="2026-11-21T00:00:00Z")))
            self.assertEqual(local.list_scores(run_id=rid)[0]["label"], "fail")

    def test_enrich_counts_bodyless_change_requests_and_commits_after_head(self):
        with TemporaryDirectory() as tmpdir:
            store = TelemetryStore(Path(tmpdir) / "tel.sqlite3")
            rid = store.record_run({
                "repo": "Example/Engine", "issue": 6, "stop": "verified", "head_sha": "b" * 40,
                "pull_request": "https://github.com/Example/Engine/pull/6",
            })
            store.enrich_github_outcomes(lambda *a: {
                "state": "MERGED",
                "reviews": [
                    {"author": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "body": ""},
                    {"author": {"login": "foreman-bot"}, "state": "CHANGES_REQUESTED", "body": ""},
                ],
                "comments": [],
                "commits": [
                    {"oid": "a" * 40, "authors": [{"login": "owner"}]},
                    {"oid": "b" * 40, "authors": [{"login": "owner"}]},
                    {"oid": "c" * 40, "authors": [{"login": "owner"}]},
                ],
            })
            run = store.get_run(rid)
            self.assertEqual((run["gh_review_changes"], run["gh_human_commits"]), (1, 1))


class BudgetGuardAndCliStatsTest(unittest.TestCase):
    def test_budget_soft_warning_and_hard_halt_in_delivery(self):
        calls = []

        def handler(node, model, state, instruction):
            calls.append(node)
            if node == "triage":
                return {"triage": "implement"}
            if node == "implement":
                return {"implementation": "ok", "head_sha": "a" * 40}
            if node == "verify":
                return {"verify": "pass"}
            return {"review": "approve", "risk": DEFAULT_RISK}

        dispatcher = FailoverDispatcher(handler)
        orig_run = dispatcher.run

        def tracked_run(node, models, state, instruction, **kwargs):
            res = orig_run(node, models, state, instruction, **kwargs)
            model_name = res["model"] or ""
            dispatcher.last_meta = [{
                "node": node,
                "model": model_name,
                "slug": model_name.rsplit("-", 1)[0] if model_name else "",
                "provider": "claude",
                "status": "ran",
                "duration_ms": 100,
                "total_cost_usd": 12.0 if node == "triage" else (40.0 if node == "implement" else 0.0),
                "model_usage": {},
            }]
            return res

        dispatcher.run = tracked_run  # type: ignore[method-assign]
        soft_res = run_delivery(
            dispatcher,
            repo="Example/Engine",
            request="Build it",
            budget={"soft_usd": 10.0, "hard_usd": 0.0, "hard_tokens": 0},
        )
        self.assertEqual(soft_res["stop"], "verified")
        self.assertIn("soft limit", soft_res["budget_warning"])

        calls.clear()
        hard_res = run_delivery(
            dispatcher,
            repo="Example/Engine",
            request="Build it",
            budget={"soft_usd": 5.0, "hard_usd": 10.0, "hard_tokens": 0},
        )
        self.assertEqual(hard_res["stop"], "budget_exceeded")
        self.assertEqual(hard_res["failure_phase"], "budget")
        self.assertEqual(calls, ["triage"])

    def test_cli_run_persists_telemetry_and_stats_export_import_commands(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            repo = root / "repo"
            repo.mkdir()
            git_repo(repo)
            db_path = root / "tel.sqlite3"
            checkpoint = root / "ckpt.sqlite3"

            def worker(args, cwd):
                if args[-1].find("Review") != -1 and "--model" not in args and "-m" in args:
                    pass
                if args[0] == "claude":
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        json.dumps({
                            "structured_output": {"triage": "implement"},
                            "session_id": "sess-1",
                            "duration_ms": 500,
                            "total_cost_usd": 0.25,
                            "modelUsage": {"claude-opus-5-5": {"inputTokens": 300, "outputTokens": 50}},
                        }),
                        "",
                    )
                out_path = Path(args[args.index("-o") + 1])
                if "Implement" in args[-1] or "required" in args[-1] and "review" not in args[-1].lower():
                    (repo / "feat.txt").write_text("hi\n", encoding="utf-8")
                    out_path.write_text(json.dumps({"implementation": "done", "pr_summary": "Done"}), encoding="utf-8")
                else:
                    out_path.write_text(json.dumps({"review": "approve", "risk": DEFAULT_RISK}), encoding="utf-8")
                return subprocess.CompletedProcess(args, 0, '{"type":"thread.started","thread_id":"t-1"}\n{"type":"turn.completed","usage":{"input_tokens":400,"output_tokens":60}}', "model: gpt-6.2-sol\n")

            prev_cwd = Path.cwd()
            try:
                import os
                os.chdir(repo)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(
                        ["run", "delivery", "--repo", "Example/Engine", "--issue", "8",
                         "--check", "exit 0", "--telemetry", str(db_path)],
                        worker_runner=worker,
                        checkpoint=checkpoint,
                        github=FakeGitHub(),
                        run_gh=lambda cmd: 0,
                        inspect_worktree=clean_worktree,
                        read_merge_hold=lambda repo: None,
                    )
                self.assertEqual(code, 0)
            finally:
                os.chdir(prev_cwd)

            store = TelemetryStore(db_path)
            self.assertEqual(len(store.list_runs()), 1)

            html_file = root / "stats.html"
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["stats", "--db", str(db_path), "--format", "json"]), 0)
            parsed = json.loads(out.getvalue())
            self.assertEqual(parsed["total_runs"], 1)

            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["stats", "--db", str(db_path), "--format", "html", "--out", str(html_file)]), 0)
            self.assertIn("<svg", html_file.read_text(encoding="utf-8"))

            sync_file = root / "export.jsonl"
            other_db = root / "other.sqlite3"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["telemetry", "export", "--db", str(db_path), "--out", str(sync_file)]), 0)
                self.assertEqual(main(["telemetry", "import", str(sync_file), "--db", str(other_db)]), 0)
            self.assertEqual(len(TelemetryStore(other_db).list_runs()), 1)


if __name__ == "__main__":
    unittest.main()
