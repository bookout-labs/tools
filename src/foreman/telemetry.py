"""Local SQLite run telemetry, pricing estimation, multi-host JSONL sync, and stats dashboards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import contextlib
import html
import json
import os
from pathlib import Path
import re
import socket
import statistics
import subprocess
import tomllib
from typing import Any, Callable
import uuid

from foreman.config import ConfigError, family
from foreman.feedback import author_login, human_followup_commits, is_foreman_body, is_human_actor
from foreman.redaction import redact_text, redact_value

PACKAGE = Path(__file__).parent
PRICING_PATH = PACKAGE / "pricing.toml"
PR_URL_PATTERN = re.compile(r"^https?://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)(?:[/?#].*)?$")


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_telemetry_path() -> Path:
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return data_home / "foreman" / "telemetry.sqlite3"


def load_pricing(path: Path | None = None) -> dict[str, Any]:
    raw = tomllib.loads((path or PRICING_PATH).read_text(encoding="utf-8"))
    return {
        "budget": dict(raw.get("budget") or {}),
        "default": dict(raw.get("default") or {}),
        "families": {k: dict(v) for k, v in (raw.get("families") or {}).items()},
        "models": {k: dict(v) for k, v in (raw.get("models") or {}).items()},
    }


def _rate_for_model(slug: str, pricing: dict[str, Any]) -> dict[str, float]:
    models = pricing.get("models") or {}
    if slug in models:
        return models[slug]
    for key, rate in models.items():
        if slug.startswith(f"{key}-"):
            return rate
    try:
        fam = family(slug)
    except ConfigError:
        fam = ""
    families = pricing.get("families") or {}
    if fam in families:
        return families[fam]
    return pricing.get("default") or {
        "input_per_mtok": 3.0,
        "cached_input_per_mtok": 0.3,
        "cache_write_per_mtok": 3.75,
        "output_per_mtok": 15.0,
    }


def _int_metric(mapping: dict[str, Any], *keys: str) -> int:
    for key in keys:
        val = mapping.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and val >= 0:
            return val
    return 0


def extract_usage_tokens(usage_entry: Any, *, provider: str = "") -> dict[str, int]:
    """Normalize token counters across Claude, Codex, and Grok usage dictionaries.

    ``input_tokens`` in the result is always uncached input, so cached reads are billed once.
    """
    if not isinstance(usage_entry, dict):
        return {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
    raw_input = _int_metric(usage_entry, "input_tokens", "inputTokens", "prompt_tokens")
    cached = _int_metric(
        usage_entry,
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cacheReadInputTokens",
        "cached_tokens",
    )
    cache_write = _int_metric(
        usage_entry,
        "cache_write_input_tokens",
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    )
    out = _int_metric(usage_entry, "output_tokens", "outputTokens", "completion_tokens")
    if out == 0:
        out = _int_metric(usage_entry, "reasoning_output_tokens")
    reported_total = _int_metric(usage_entry, "total_tokens", "totalTokens")
    # Codex reports input_tokens inclusive of cached_input_tokens; Claude reports them separately.
    if "cached_input_tokens" in usage_entry or provider == "gpt":
        uncached = max(0, raw_input - cached)
        computed_total = raw_input + cache_write + out
    else:
        uncached = raw_input
        computed_total = raw_input + cached + cache_write + out
    return {
        "input_tokens": uncached,
        "cached_input_tokens": cached,
        "cache_write_tokens": cache_write,
        "output_tokens": out,
        "total_tokens": max(computed_total, reported_total),
    }


def estimate_usage_cost(
    record: dict[str, Any],
    pricing: dict[str, Any] | None = None,
) -> tuple[float, dict[str, int], bool]:
    """Compute USD cost and normalized tokens for one stage worker metadata record.

    Returns (cost_usd, tokens_dict, estimated) where estimated is True when cost
    was derived from pricing.toml rather than reported directly by the CLI.
    """
    pricing_data = pricing if pricing is not None else load_pricing()
    totals = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    estimated_cost = 0.0
    model_usage = record.get("model_usage")
    fallback_slug = str(record.get("slug") or record.get("model") or "")
    provider = str(record.get("provider") or "")

    if isinstance(model_usage, dict) and model_usage:
        for model_key, raw_usage in model_usage.items():
            toks = extract_usage_tokens(raw_usage, provider=provider)
            for k, v in toks.items():
                totals[k] += v
            rate = _rate_for_model(str(model_key or fallback_slug), pricing_data)
            estimated_cost += (
                toks["input_tokens"] * float(rate.get("input_per_mtok", 0.0))
                + toks["cached_input_tokens"] * float(rate.get("cached_input_per_mtok", 0.0))
                + toks["cache_write_tokens"] * float(rate.get("cache_write_per_mtok", 0.0))
                + toks["output_tokens"] * float(rate.get("output_per_mtok", 0.0))
            ) / 1_000_000.0
    else:
        direct_tokens = extract_usage_tokens(record, provider=provider)
        totals = direct_tokens
        if any(direct_tokens.values()):
            rate = _rate_for_model(fallback_slug, pricing_data)
            estimated_cost += (
                direct_tokens["input_tokens"] * float(rate.get("input_per_mtok", 0.0))
                + direct_tokens["cached_input_tokens"] * float(rate.get("cached_input_per_mtok", 0.0))
                + direct_tokens["cache_write_tokens"] * float(rate.get("cache_write_per_mtok", 0.0))
                + direct_tokens["output_tokens"] * float(rate.get("output_per_mtok", 0.0))
            ) / 1_000_000.0

    explicit_cost = record.get("total_cost_usd")
    if isinstance(explicit_cost, (int, float)) and not isinstance(explicit_cost, bool) and explicit_cost >= 0:
        return round(float(explicit_cost), 6), totals, False
    return round(estimated_cost, 6), totals, True


def state_usage_summary(
    usage_records: list[dict[str, Any]] | None,
    pricing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sum cost and token metrics across all recorded worker invocations in a run."""
    pricing_data = pricing if pricing is not None else load_pricing()
    totals = {
        "cost_usd": 0.0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cost_estimated": False,
    }
    for record in usage_records or []:
        if not isinstance(record, dict):
            continue
        cost, toks, estimated = estimate_usage_cost(record, pricing_data)
        totals["cost_usd"] = round(totals["cost_usd"] + cost, 6)
        totals["cost_estimated"] = totals["cost_estimated"] or (estimated and cost > 0)
        for key, value in toks.items():
            totals[key] += value
    return totals


def pr_size_bucket(
    additions: int | None,
    deletions: int | None,
    changed_files: int | None = None,
) -> str:
    """Categorize PR diff size into S, M, L, XL, or unknown."""
    if additions is None and deletions is None:
        return "unknown"
    churn = max(0, additions or 0) + max(0, deletions or 0)
    files = max(0, changed_files or 0)
    if churn < 50 and files <= 3:
        return "S"
    if churn < 250 and files <= 10:
        return "M"
    if churn < 800:
        return "L"
    return "XL"


def _sanitize_state_snapshot(state: dict[str, Any]) -> dict[str, Any]:
    """Keep public-safe telemetry metadata without leaking unredacted prompts/secrets."""
    safe_keys = (
        "repo",
        "issue",
        "stop",
        "halt",
        "halt_detail",
        "failure_phase",
        "failure_reason",
        "verify",
        "review",
        "review_note",
        "review_comments",
        "pr_summary",
        "pull_request",
        "head_sha",
        "check",
        "check_exit_code",
        "check_output",
        "verify_rounds",
        "review_rounds",
        "models_used",
        "implement_models",
        "proof_labels",
        "risk",
        "attempts",
        "path",
        "sessions",
    )
    out: dict[str, Any] = {key: redact_value(state[key], redact_pii=True) for key in safe_keys if key in state}
    if "request" in state and isinstance(state["request"], str):
        first_line = state["request"].strip().splitlines()[0] if state["request"].strip() else ""
        # Redact before clipping so a secret straddling the cut is never left half-visible.
        out["request_title"] = redact_text(first_line, redact_pii=True)[:160]
    return out


def _scrub_export_row(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-redact every field on the way out so rows stored before a redaction fix never leak."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key.endswith("_json") and isinstance(value, str):
            # Decode first: JSON escapes such as \u0040 hide PII from a raw-text scrub.
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = {}
            out[key] = json.dumps(redact_value(decoded, redact_pii=True), sort_keys=True)
        else:
            out[key] = redact_value(value, redact_pii=True)
    return out


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _default_gh_pr_fetcher(pr_repo: str, pr_url: str) -> dict[str, Any]:
    proc = subprocess.run(
        [
            "gh", "pr", "view", pr_url, "--repo", pr_repo, "--json",
            "state,mergedAt,createdAt,additions,deletions,changedFiles,commits,comments,reviews",
        ],
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"gh exited {proc.returncode}")
    data = json.loads(proc.stdout)
    if not isinstance(data, dict):
        raise RuntimeError("gh pr view did not return an object")
    return data


def _stored_passed(label: object, passed: object) -> int | None:
    # Warp invariant: insufficient_evidence never passes, whatever the writer claimed.
    if label == "insufficient_evidence":
        return 0
    return None if passed is None else (1 if passed else 0)


RUN_CORE_COLUMNS = (
    "host", "repo", "issue", "started_at", "finished_at", "duration_ms", "stop",
    "failure_phase", "failure_reason", "verify_rounds", "review_rounds", "pr_url", "head_sha",
    "cost_usd", "cost_estimated", "input_tokens", "cached_input_tokens", "cache_write_tokens",
    "output_tokens", "total_tokens", "session_id", "models_json", "state_json",
)
RUN_GH_COLUMNS = (
    "gh_merged", "gh_state", "gh_human_comments", "gh_review_changes", "gh_human_commits",
    "gh_additions", "gh_deletions", "gh_changed_files", "gh_size_bucket",
    "gh_merged_at", "gh_created_at", "gh_cycle_seconds", "gh_enriched_at",
)
RUN_DEFAULTS: dict[str, Any] = {
    "host": "unknown", "repo": "unknown/repo", "started_at": "", "finished_at": "",
    "duration_ms": 0, "stop": "unknown", "failure_phase": "", "failure_reason": "",
    "verify_rounds": 0, "review_rounds": 0, "pr_url": "", "head_sha": "",
    "cost_usd": 0.0, "cost_estimated": 0, "input_tokens": 0,
    "cached_input_tokens": 0, "cache_write_tokens": 0, "output_tokens": 0, "total_tokens": 0,
    "session_id": "", "models_json": "{}", "state_json": "{}",
    "gh_state": "", "gh_human_comments": 0, "gh_review_changes": 0, "gh_human_commits": 0,
    "gh_size_bucket": "", "gh_merged_at": "", "gh_created_at": "", "gh_enriched_at": "",
}
SCORE_COLUMNS = ("run_id", "scorer", "label", "score", "passed", "reason", "judge_model", "scored_at")
STAGE_CALL_COLUMNS = (
    "call_id", "run_id", "host", "seq", "node", "model", "provider", "status",
    "duration_ms", "num_turns", "session_id", "cost_usd",
    "input_tokens", "cached_input_tokens", "cache_write_tokens",
    "output_tokens", "total_tokens",
)


def _fresher(column: str, stamp: str) -> str:
    return f"{column}=CASE WHEN COALESCE(excluded.{stamp}, '') >= COALESCE(runs.{stamp}, '') THEN excluded.{column} ELSE runs.{column} END"


# Imports from other hosts must never roll back a newer local run or GitHub enrichment.
IMPORT_RUN_SQL = (
    f"INSERT INTO runs (run_id, {', '.join(RUN_CORE_COLUMNS + RUN_GH_COLUMNS)}) "
    f"VALUES ({', '.join(['?'] * (1 + len(RUN_CORE_COLUMNS) + len(RUN_GH_COLUMNS)))}) "
    "ON CONFLICT(run_id) DO UPDATE SET "
    + ", ".join(
        [_fresher(c, "finished_at") for c in RUN_CORE_COLUMNS]
        + [_fresher(c, "gh_enriched_at") for c in RUN_GH_COLUMNS]
    )
)
IMPORT_SCORE_SQL = (
    f"INSERT INTO scores ({', '.join(SCORE_COLUMNS)}) VALUES ({', '.join(['?'] * len(SCORE_COLUMNS))}) "
    "ON CONFLICT(run_id, scorer) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in SCORE_COLUMNS[2:])
    + " WHERE COALESCE(excluded.scored_at, '') >= COALESCE(scores.scored_at, '')"
)


class TelemetryStore:
    """SQLite-backed store for runs, per-stage calls, GitHub outcomes, and scorer results."""

    def __init__(self, path: Path | None = None):
        import sqlite3

        self.path = path or default_telemetry_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._sqlite3 = sqlite3
        self._ensure_schema()

    @contextlib.contextmanager
    def _connect(self):
        conn = self._sqlite3.connect(str(self.path), timeout=10.0)
        try:
            conn.row_factory = self._sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    issue INTEGER,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    stop TEXT NOT NULL,
                    failure_phase TEXT DEFAULT '',
                    failure_reason TEXT DEFAULT '',
                    verify_rounds INTEGER NOT NULL DEFAULT 0,
                    review_rounds INTEGER NOT NULL DEFAULT 0,
                    pr_url TEXT DEFAULT '',
                    head_sha TEXT DEFAULT '',
                    cost_usd REAL NOT NULL DEFAULT 0.0,
                    cost_estimated INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    session_id TEXT DEFAULT '',
                    models_json TEXT DEFAULT '{}',
                    state_json TEXT DEFAULT '{}',
                    gh_merged INTEGER,
                    gh_state TEXT DEFAULT '',
                    gh_human_comments INTEGER NOT NULL DEFAULT 0,
                    gh_review_changes INTEGER NOT NULL DEFAULT 0,
                    gh_human_commits INTEGER NOT NULL DEFAULT 0,
                    gh_additions INTEGER,
                    gh_deletions INTEGER,
                    gh_changed_files INTEGER,
                    gh_size_bucket TEXT DEFAULT '',
                    gh_merged_at TEXT DEFAULT '',
                    gh_created_at TEXT DEFAULT '',
                    gh_cycle_seconds REAL,
                    gh_enriched_at TEXT DEFAULT ''
                );

                CREATE INDEX IF NOT EXISTS idx_runs_finished_at ON runs(finished_at);
                CREATE INDEX IF NOT EXISTS idx_runs_repo ON runs(repo);

                CREATE TABLE IF NOT EXISTS stage_calls (
                    call_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    host TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    node TEXT NOT NULL,
                    model TEXT DEFAULT '',
                    provider TEXT DEFAULT '',
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    num_turns INTEGER,
                    session_id TEXT DEFAULT '',
                    cost_usd REAL NOT NULL DEFAULT 0.0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS idx_stage_calls_run_id ON stage_calls(run_id);

                CREATE TABLE IF NOT EXISTS scores (
                    run_id TEXT NOT NULL,
                    scorer TEXT NOT NULL,
                    label TEXT NOT NULL,
                    score REAL,
                    passed INTEGER,
                    reason TEXT DEFAULT '',
                    judge_model TEXT DEFAULT '',
                    scored_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, scorer)
                );
                """
            )

    def record_run(
        self,
        state: dict[str, Any],
        *,
        host: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        duration_ms: int | None = None,
        run_id: str | None = None,
        pricing: dict[str, Any] | None = None,
    ) -> str:
        host_name = host or socket.gethostname() or "unknown-host"
        finished = finished_at or utcnow_iso()
        started = started_at or finished
        repo = str(state.get("repo") or "unknown/repo")
        issue_raw = state.get("issue")
        issue = issue_raw if isinstance(issue_raw, int) and not isinstance(issue_raw, bool) else None
        rid = run_id or f"{host_name}:{repo.lower()}#{issue or 0}:{finished}:{uuid.uuid4().hex[:8]}"

        pricing_data = pricing if pricing is not None else load_pricing()
        usage_records = [r for r in (state.get("usage_records") or []) if isinstance(r, dict)]
        usage_summary = state_usage_summary(usage_records, pricing_data)

        dur = duration_ms
        if dur is None:
            dur = sum(
                int(r["duration_ms"])
                for r in usage_records
                if isinstance(r.get("duration_ms"), (int, float)) and r["duration_ms"] >= 0
            )
            dt_start, dt_end = _parse_iso(started), _parse_iso(finished)
            if dur == 0 and dt_start and dt_end:
                dur = max(0, int((dt_end - dt_start).total_seconds() * 1000))

        sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
        primary_session = ""
        for pref in ("implement", "review", "triage", "spec", "research"):
            if isinstance(sessions.get(pref), str) and sessions[pref]:
                primary_session = sessions[pref]
                break
        if not primary_session and usage_records:
            primary_session = str(next((r.get("session_id") for r in usage_records if r.get("session_id")), ""))

        safe_state = _sanitize_state_snapshot(state)
        row_values = (
            rid,
            host_name,
            repo,
            issue,
            started,
            finished,
            int(dur),
            str(state.get("stop") or state.get("halt") or "unknown"),
            str(state.get("failure_phase") or ""),
            str(state.get("failure_reason") or ""),
            int(state.get("verify_rounds") or 0),
            int(state.get("review_rounds") or 0),
            str(state.get("pull_request") or ""),
            str(state.get("head_sha") or ""),
            usage_summary["cost_usd"],
            1 if usage_summary["cost_estimated"] else 0,
            usage_summary["input_tokens"],
            usage_summary["cached_input_tokens"],
            usage_summary["cache_write_tokens"],
            usage_summary["output_tokens"],
            usage_summary["total_tokens"],
            primary_session,
            json.dumps(state.get("models_used") or {}, sort_keys=True),
            json.dumps(safe_state, sort_keys=True),
        )

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, host, repo, issue, started_at, finished_at, duration_ms, stop,
                    failure_phase, failure_reason, verify_rounds, review_rounds, pr_url, head_sha,
                    cost_usd, cost_estimated, input_tokens, cached_input_tokens, cache_write_tokens,
                    output_tokens, total_tokens, session_id, models_json, state_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    host=excluded.host, repo=excluded.repo, issue=excluded.issue,
                    started_at=excluded.started_at, finished_at=excluded.finished_at,
                    duration_ms=excluded.duration_ms, stop=excluded.stop,
                    failure_phase=excluded.failure_phase, failure_reason=excluded.failure_reason,
                    verify_rounds=excluded.verify_rounds, review_rounds=excluded.review_rounds,
                    pr_url=excluded.pr_url, head_sha=excluded.head_sha,
                    cost_usd=excluded.cost_usd, cost_estimated=excluded.cost_estimated,
                    input_tokens=excluded.input_tokens,
                    cached_input_tokens=excluded.cached_input_tokens,
                    cache_write_tokens=excluded.cache_write_tokens,
                    output_tokens=excluded.output_tokens,
                    total_tokens=excluded.total_tokens,
                    session_id=excluded.session_id,
                    models_json=excluded.models_json,
                    state_json=excluded.state_json
                """,
                row_values,
            )
            conn.execute("DELETE FROM stage_calls WHERE run_id = ?", (rid,))

            calls_source = usage_records or [
                {
                    "node": a.get("node") or "unknown",
                    "model": a.get("model") or "",
                    "provider": "",
                    "status": a.get("status") or "unknown",
                    "duration_ms": 0,
                }
                for a in (state.get("attempts") or [])
                if isinstance(a, dict)
            ]
            for idx, rec in enumerate(calls_source):
                cost, toks, _ = estimate_usage_cost(rec, pricing_data)
                node_name = str(rec.get("node") or "unknown")
                model_name = str(rec.get("model") or "")
                provider_name = str(rec.get("provider") or "")
                if not provider_name and model_name:
                    try:
                        provider_name = family(model_name)
                    except ConfigError:
                        provider_name = ""
                conn.execute(
                    """
                    INSERT OR REPLACE INTO stage_calls (
                        call_id, run_id, host, seq, node, model, provider, status,
                        duration_ms, num_turns, session_id, cost_usd,
                        input_tokens, cached_input_tokens, cache_write_tokens,
                        output_tokens, total_tokens
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        f"{rid}:{idx}",
                        rid,
                        host_name,
                        idx,
                        node_name,
                        model_name,
                        provider_name,
                        str(rec.get("status") or "ran"),
                        int(rec.get("duration_ms") or 0),
                        rec.get("num_turns") if isinstance(rec.get("num_turns"), int) else None,
                        str(rec.get("session_id") or ""),
                        cost,
                        toks["input_tokens"],
                        toks["cached_input_tokens"],
                        toks["cache_write_tokens"],
                        toks["output_tokens"],
                        toks["total_tokens"],
                    ),
                )
        return rid

    def _row_to_run(self, row) -> dict[str, Any]:
        item = dict(row)
        try:
            item["models_used"] = json.loads(item.get("models_json") or "{}")
        except ValueError:
            item["models_used"] = {}
        try:
            item["state"] = json.loads(item.get("state_json") or "{}")
        except ValueError:
            item["state"] = {}
        return item

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._row_to_run(row) if row is not None else None

    def list_runs(
        self,
        *,
        repo: str | None = None,
        host: str | None = None,
        since: str | None = None,
        until: str | None = None,
        stop: str | None = None,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if repo:
            clauses.append("repo = ?")
            params.append(repo)
        if host:
            clauses.append("host = ?")
            params.append(host)
        if since:
            clauses.append("finished_at >= ?")
            params.append(since)
        if until:
            clauses.append("finished_at <= ?")
            params.append(until)
        if stop:
            clauses.append("stop = ?")
            params.append(stop)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs {where} ORDER BY finished_at DESC, rowid DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    def list_stage_calls(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM stage_calls WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    def list_scores(self, *, run_id: str | None = None) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute("SELECT * FROM scores WHERE run_id = ? ORDER BY scorer ASC", (run_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM scores ORDER BY scored_at DESC, run_id, scorer").fetchall()
            return [dict(r) for r in rows]

    def upsert_score(
        self,
        *,
        run_id: str,
        scorer: str,
        label: str,
        score: float | None,
        passed: bool | None,
        reason: str = "",
        judge_model: str = "",
        scored_at: str | None = None,
    ) -> None:
        now = scored_at or utcnow_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO scores (run_id, scorer, label, score, passed, reason, judge_model, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, scorer) DO UPDATE SET
                    label=excluded.label,
                    score=excluded.score,
                    passed=excluded.passed,
                    reason=excluded.reason,
                    judge_model=excluded.judge_model,
                    scored_at=excluded.scored_at
                """,
                (
                    run_id,
                    scorer,
                    label,
                    score,
                    _stored_passed(label, passed),
                    redact_text(reason, redact_pii=True),
                    judge_model,
                    now,
                ),
            )

    def enrich_github_outcomes(
        self,
        fetcher: Callable[[str, str], dict[str, Any]] | None = None,
        *,
        repo: str | None = None,
    ) -> int:
        fetch = fetcher or _default_gh_pr_fetcher
        runs = self.list_runs(repo=repo, limit=1000)
        newest_heads: dict[str, str] = {}
        for run in runs:  # newest first, so later runs on a shared PR set the head
            pr_url, head = str(run.get("pr_url") or "").strip(), str(run.get("head_sha") or "").strip()
            if pr_url and head:
                newest_heads.setdefault(pr_url, head)
        updated = 0
        now = utcnow_iso()
        with self._connect() as conn:
            for run in runs:
                pr_url = str(run.get("pr_url") or "").strip()
                match = PR_URL_PATTERN.match(pr_url)
                if not match:
                    continue
                pr_repo = match.group(1)
                payload = fetch(pr_repo, pr_url)
                pr_state = str(payload.get("state") or "").upper()
                merged_at = str(payload.get("mergedAt") or "")
                created_at = str(payload.get("createdAt") or "")
                cycle_seconds: float | None = None
                dt_c, dt_m = _parse_iso(created_at), _parse_iso(merged_at)
                if dt_c and dt_m:
                    cycle_seconds = max(0.0, (dt_m - dt_c).total_seconds())

                human_comments = sum(
                    1
                    for c in payload.get("comments") or []
                    if isinstance(c, dict) and is_human_actor(author_login(c)) and not is_foreman_body(c.get("body"))
                )
                review_changes = sum(
                    1
                    for rv in payload.get("reviews") or []
                    if isinstance(rv, dict)
                    and str(rv.get("state") or "").upper() == "CHANGES_REQUESTED"
                    and is_human_actor(author_login(rv))
                    and not is_foreman_body(rv.get("body"))
                )
                human_commits = human_followup_commits(payload.get("commits"), newest_heads.get(pr_url))

                additions = payload.get("additions") if isinstance(payload.get("additions"), int) else None
                deletions = payload.get("deletions") if isinstance(payload.get("deletions"), int) else None
                changed_files = payload.get("changedFiles") if isinstance(payload.get("changedFiles"), int) else None
                bucket = pr_size_bucket(additions, deletions, changed_files)

                conn.execute(
                    """
                    UPDATE runs SET
                        gh_merged = ?,
                        gh_state = ?,
                        gh_human_comments = ?,
                        gh_review_changes = ?,
                        gh_human_commits = ?,
                        gh_additions = ?,
                        gh_deletions = ?,
                        gh_changed_files = ?,
                        gh_size_bucket = ?,
                        gh_merged_at = ?,
                        gh_created_at = ?,
                        gh_cycle_seconds = ?,
                        gh_enriched_at = ?
                    WHERE run_id = ?
                    """,
                    (
                        1 if pr_state == "MERGED" else 0,
                        pr_state,
                        human_comments,
                        review_changes,
                        human_commits,
                        additions,
                        deletions,
                        changed_files,
                        bucket,
                        merged_at,
                        created_at,
                        cycle_seconds,
                        now,
                        run["run_id"],
                    ),
                )
                updated += 1
        return updated

    def export_jsonl(
        self,
        output: Path | None = None,
        *,
        since: str | None = None,
        host: str | None = None,
    ) -> str:
        runs = self.list_runs(since=since, host=host, limit=10000)
        lines: list[str] = []
        run_ids: list[str] = []
        for run in reversed(runs):
            run_ids.append(run["run_id"])
            payload = _scrub_export_row({k: v for k, v in run.items() if k not in {"models_used", "state"}})
            payload["type"] = "run"
            lines.append(json.dumps(payload, sort_keys=True))
        with self._connect() as conn:
            for rid in run_ids:
                for row in conn.execute("SELECT * FROM stage_calls WHERE run_id = ? ORDER BY seq ASC", (rid,)):
                    payload = _scrub_export_row(dict(row))
                    payload["type"] = "stage_call"
                    lines.append(json.dumps(payload, sort_keys=True))
                for row in conn.execute("SELECT * FROM scores WHERE run_id = ? ORDER BY scorer ASC", (rid,)):
                    payload = _scrub_export_row(dict(row))
                    payload["type"] = "score"
                    lines.append(json.dumps(payload, sort_keys=True))
        text = ("\n".join(lines) + "\n") if lines else ""
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        return text

    def import_jsonl(self, source: Path | str) -> dict[str, int]:
        raw = source.read_text(encoding="utf-8") if isinstance(source, Path) else str(source)
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
        rows = {kind: [i for i in items if isinstance(i, dict) and i.get("type") == kind]
                for kind in ("run", "stage_call", "score")}
        counts = {"runs": 0, "stage_calls": 0, "scores": 0}
        with self._connect() as conn:
            # Runs go first, whatever the line order, so stage calls can follow their run's freshness.
            fresh_runs: set[str] = set()
            for item in rows["run"]:
                existing = conn.execute(
                    "SELECT finished_at FROM runs WHERE run_id = ?", (item.get("run_id"),)
                ).fetchone()
                if existing is None or str(item.get("finished_at") or "") >= str(existing[0] or ""):
                    fresh_runs.add(str(item.get("run_id")))
                    # A fresher snapshot owns the run's calls; calls it no longer lists are stale.
                    conn.execute("DELETE FROM stage_calls WHERE run_id = ?", (item.get("run_id"),))
                conn.execute(
                    IMPORT_RUN_SQL,
                    [item.get("run_id")]
                    + [item.get(c, RUN_DEFAULTS.get(c)) for c in RUN_CORE_COLUMNS + RUN_GH_COLUMNS],
                )
                counts["runs"] += 1
            for item in rows["stage_call"]:
                if str(item.get("run_id")) not in fresh_runs:
                    continue
                conn.execute(
                    f"INSERT OR REPLACE INTO stage_calls ({', '.join(STAGE_CALL_COLUMNS)}) "
                    f"VALUES ({', '.join(['?'] * len(STAGE_CALL_COLUMNS))})",
                    [item.get(c) for c in STAGE_CALL_COLUMNS],
                )
                counts["stage_calls"] += 1
            for item in rows["score"]:
                vals = {c: item.get(c) for c in SCORE_COLUMNS}
                vals["passed"] = _stored_passed(vals["label"], vals["passed"])
                conn.execute(IMPORT_SCORE_SQL, [vals[c] for c in SCORE_COLUMNS])
                counts["scores"] += 1
        return counts

    def compute_stats(
        self,
        *,
        repo: str | None = None,
        host: str | None = None,
        since: str | None = None,
        days: int | None = None,
    ) -> dict[str, Any]:
        effective_since = since
        if days is not None and not effective_since:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            effective_since = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        runs = self.list_runs(repo=repo, host=host, since=effective_since, limit=10000)
        run_ids = [r["run_id"] for r in runs]

        calls: list[dict[str, Any]] = []
        scores: list[dict[str, Any]] = []
        if run_ids:
            with self._connect() as conn:
                placeholders = ", ".join(["?"] * len(run_ids))
                calls = [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT * FROM stage_calls WHERE run_id IN ({placeholders})",
                        run_ids,
                    ).fetchall()
                ]
                scores = [
                    dict(r)
                    for r in conn.execute(
                        f"SELECT * FROM scores WHERE run_id IN ({placeholders})",
                        run_ids,
                    ).fetchall()
                ]

        total_runs = len(runs)
        verified_runs = sum(1 for r in runs if r["stop"] == "verified")
        pr_runs = [r for r in runs if str(r.get("pr_url") or "").strip()]
        merged_runs = [r for r in pr_runs if r.get("gh_merged") == 1]
        zero_touch_runs = [
            r
            for r in merged_runs
            if (r.get("gh_human_comments") or 0) == 0
            and (r.get("gh_review_changes") or 0) == 0
            and (r.get("gh_human_commits") or 0) == 0
        ]

        costs = [float(r.get("cost_usd") or 0.0) for r in runs]
        durations = [int(r.get("duration_ms") or 0) for r in runs if (r.get("duration_ms") or 0) > 0]
        cycle_secs = [
            float(r["gh_cycle_seconds"])
            for r in runs
            if isinstance(r.get("gh_cycle_seconds"), (int, float))
        ]
        input_tokens = sum(int(r.get("input_tokens") or 0) for r in runs)
        cached_tokens = sum(int(r.get("cached_input_tokens") or 0) for r in runs)
        cache_write_tokens = sum(int(r.get("cache_write_tokens") or 0) for r in runs)
        output_tokens = sum(int(r.get("output_tokens") or 0) for r in runs)
        total_tokens = sum(int(r.get("total_tokens") or 0) for r in runs)

        cache_denom = input_tokens + cached_tokens
        cache_hit_rate = (cached_tokens / cache_denom) if cache_denom > 0 else None

        by_stop: dict[str, int] = {}
        by_host: dict[str, dict[str, Any]] = {}
        by_repo: dict[str, dict[str, Any]] = {}
        by_failure_phase: dict[str, int] = {}
        by_size: dict[str, dict[str, Any]] = {}

        for r in runs:
            stop = r["stop"]
            by_stop[stop] = by_stop.get(stop, 0) + 1

            h = r["host"]
            h_entry = by_host.setdefault(h, {"runs": 0, "cost_usd": 0.0, "tokens": 0})
            h_entry["runs"] += 1
            h_entry["cost_usd"] = round(h_entry["cost_usd"] + float(r.get("cost_usd") or 0.0), 6)
            h_entry["tokens"] += int(r.get("total_tokens") or 0)

            rp = r["repo"]
            rp_entry = by_repo.setdefault(rp, {"runs": 0, "verified": 0, "cost_usd": 0.0, "tokens": 0})
            rp_entry["runs"] += 1
            if stop == "verified":
                rp_entry["verified"] += 1
            rp_entry["cost_usd"] = round(rp_entry["cost_usd"] + float(r.get("cost_usd") or 0.0), 6)
            rp_entry["tokens"] += int(r.get("total_tokens") or 0)

            if r.get("failure_phase"):
                fp = str(r["failure_phase"])
                by_failure_phase[fp] = by_failure_phase.get(fp, 0) + 1

            bucket = str(r.get("gh_size_bucket") or "").strip() or "unknown"
            b_entry = by_size.setdefault(bucket, {"runs": 0, "merged": 0, "zero_touch": 0, "_cycles": []})
            b_entry["runs"] += 1
            if r.get("gh_merged") == 1:
                b_entry["merged"] += 1
                if (
                    (r.get("gh_human_comments") or 0) == 0
                    and (r.get("gh_review_changes") or 0) == 0
                    and (r.get("gh_human_commits") or 0) == 0
                ):
                    b_entry["zero_touch"] += 1
            if isinstance(r.get("gh_cycle_seconds"), (int, float)):
                b_entry["_cycles"].append(float(r["gh_cycle_seconds"]))

        for bucket_stats in by_size.values():
            cycles = bucket_stats.pop("_cycles")
            bucket_stats["median_cycle_seconds"] = round(statistics.median(cycles), 1) if cycles else None

        by_model: dict[str, dict[str, Any]] = {}
        for c in calls:
            model = str(c.get("model") or c.get("provider") or "unknown")
            m_entry = by_model.setdefault(
                model,
                {"calls": 0, "ran": 0, "quotas": 0, "stops": 0, "cost_usd": 0.0, "tokens": 0, "duration_ms": 0},
            )
            m_entry["calls"] += 1
            status = c.get("status")
            if status == "ran":
                m_entry["ran"] += 1
            elif status == "quota":
                m_entry["quotas"] += 1
            else:
                m_entry["stops"] += 1
            m_entry["cost_usd"] = round(m_entry["cost_usd"] + float(c.get("cost_usd") or 0.0), 6)
            m_entry["tokens"] += int(c.get("total_tokens") or 0)
            m_entry["duration_ms"] += int(c.get("duration_ms") or 0)

        scorer_cards: dict[str, dict[str, Any]] = {}
        for sc in scores:
            name = str(sc["scorer"])
            card = scorer_cards.setdefault(
                name,
                {
                    "total": 0,
                    "evaluable": 0,
                    "passed": 0,
                    "failed": 0,
                    "insufficient_evidence": 0,
                    "pass_rate": None,
                    "avg_score": None,
                    "_scores": [],
                },
            )
            card["total"] += 1
            label = str(sc.get("label") or "")
            if label == "insufficient_evidence":
                card["insufficient_evidence"] += 1
            else:
                card["evaluable"] += 1
                if sc.get("passed"):
                    card["passed"] += 1
                else:
                    card["failed"] += 1
                if isinstance(sc.get("score"), (int, float)):
                    card["_scores"].append(float(sc["score"]))
        for card in scorer_cards.values():
            vals = card.pop("_scores")
            evaluable = card["evaluable"]
            card["pass_rate"] = round(card["passed"] / evaluable, 4) if evaluable > 0 else None
            card["avg_score"] = round(sum(vals) / len(vals), 4) if vals else None

        total_cost = round(sum(costs), 6)
        return {
            "total_runs": total_runs,
            "verified_runs": verified_runs,
            "verified_rate": round(verified_runs / total_runs, 4) if total_runs else 0.0,
            "pr_runs": len(pr_runs),
            "merged_runs": len(merged_runs),
            "zero_touch_runs": len(zero_touch_runs),
            "autonomy_rate": round(len(zero_touch_runs) / len(merged_runs), 4) if merged_runs else None,
            "total_cost_usd": total_cost,
            "avg_cost_usd": round(total_cost / total_runs, 6) if total_runs else 0.0,
            "median_cost_usd": round(statistics.median(costs), 6) if costs else 0.0,
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "output_tokens": output_tokens,
            "cache_hit_rate": round(cache_hit_rate, 4) if cache_hit_rate is not None else None,
            "median_duration_ms": int(statistics.median(durations)) if durations else 0,
            "median_gh_cycle_seconds": round(statistics.median(cycle_secs), 1) if cycle_secs else None,
            "by_stop": by_stop,
            "by_host": by_host,
            "by_repo": by_repo,
            "by_model": by_model,
            "by_size_bucket": by_size,
            "by_failure_phase": by_failure_phase,
            "scorers": scorer_cards,
        }


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "n/a"


def render_stats_text(stats: dict[str, Any]) -> str:
    """Format telemetry statistics into a compact human-readable report."""
    lines = [
        f"Runs: {stats['total_runs']}  Verified: {stats['verified_runs']} ({_fmt_pct(stats['verified_rate'])})",
        (
            f"PRs: {stats['pr_runs']}  Merged: {stats['merged_runs']}  "
            f"Zero-touch: {stats['zero_touch_runs']} ({_fmt_pct(stats['autonomy_rate'])})"
        ),
        (
            f"Reference spend: ${stats['total_cost_usd']:.2f}  "
            f"Avg: ${stats['avg_cost_usd']:.2f}  Median: ${stats['median_cost_usd']:.2f}"
        ),
        (
            f"Tokens: {stats['total_tokens']:,} "
            f"(in={stats['input_tokens']:,}, cached={stats['cached_input_tokens']:,}, "
            f"out={stats['output_tokens']:,}, cache_hit={_fmt_pct(stats['cache_hit_rate'])})"
        ),
        (
            f"Median duration: {stats['median_duration_ms'] / 1000.0:.1f}s  "
            f"Median PR cycle: "
            + (f"{stats['median_gh_cycle_seconds'] / 60.0:.1f}m" if stats["median_gh_cycle_seconds"] is not None else "n/a")
        ),
    ]
    if stats["by_model"]:
        lines.append("")
        lines.append("Models:")
        for model, entry in sorted(stats["by_model"].items(), key=lambda kv: (-kv[1]["cost_usd"], kv[0])):
            lines.append(
                f"  {model:<22} calls={entry['calls']:<4} quotas={entry['quotas']:<3} "
                f"stops={entry['stops']:<3} tokens={entry['tokens']:>9,} ref=${entry['cost_usd']:.2f}"
            )
    if stats["by_host"]:
        lines.append("")
        lines.append("Hosts:")
        for host, entry in sorted(stats["by_host"].items()):
            lines.append(
                f"  {host:<22} runs={entry['runs']:<4} tokens={entry['tokens']:>9,} ref=${entry['cost_usd']:.2f}"
            )
    if stats["by_stop"]:
        lines.append("")
        lines.append("Stops: " + ", ".join(f"{k}={v}" for k, v in sorted(stats["by_stop"].items())))
    if stats["scorers"]:
        lines.append("")
        lines.append("Scorers:")
        for scorer, card in sorted(stats["scorers"].items()):
            lines.append(
                f"  {scorer:<22} evaluable={card['evaluable']:<4} "
                f"pass={_fmt_pct(card['pass_rate'])} avg={card['avg_score']} "
                f"insufficient={card['insufficient_evidence']}"
            )
    return "\n".join(lines)


def render_stats_html(stats: dict[str, Any], *, title: str = "Foreman Telemetry & Autonomy") -> str:
    """Render a self-contained static HTML dashboard with inline SVG charts."""
    model_rows = sorted(stats.get("by_model", {}).items(), key=lambda kv: (-kv[1]["cost_usd"], kv[0]))
    max_cost = max([entry["cost_usd"] for _, entry in model_rows] + [0.01])

    bar_items: list[str] = []
    for idx, (model, entry) in enumerate(model_rows[:12]):
        y = 18 + idx * 28
        bar_w = max(2, int((entry["cost_usd"] / max_cost) * 320))
        bar_items.append(
            f'<text x="8" y="{y + 14}" fill="#e2e8f0" font-size="12" font-family="monospace">{html.escape(model)}</text>'
            f'<rect x="195" y="{y}" width="{bar_w}" height="18" rx="4" fill="#38bdf8" />'
            f'<text x="{202 + bar_w}" y="{y + 14}" fill="#94a3b8" font-size="11" font-family="monospace">'
            f'${entry["cost_usd"]:.2f} ({entry["tokens"]:,} tok)</text>'
        )
    svg_height = max(60, 28 + len(bar_items) * 28)

    scorer_cards_html = ""
    if stats.get("scorers"):
        cards = []
        for name, card in sorted(stats["scorers"].items()):
            cards.append(
                f'<div class="card"><div class="label">{html.escape(name)}</div>'
                f'<div class="value">{_fmt_pct(card["pass_rate"])}</div>'
                f'<div class="sub">avg={card["avg_score"] if card["avg_score"] is not None else "n/a"} '
                f'n={card["evaluable"]} insuf={card["insufficient_evidence"]}</div></div>'
            )
        scorer_cards_html = "<h2>LLM Scorers</h2><div class=\"grid\">" + "".join(cards) + "</div>"

    size_rows = "".join(
        f"<tr><td>{html.escape(bucket)}</td><td>{data['runs']}</td><td>{data['merged']}</td>"
        f"<td>{data['zero_touch']}</td><td>"
        + (f"{data['median_cycle_seconds'] / 60.0:.1f}m" if data.get("median_cycle_seconds") is not None else "n/a")
        + "</td></tr>"
        for bucket, data in sorted(stats.get("by_size_bucket", {}).items())
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #f8fafc; margin: 24px; }}
  h1, h2 {{ margin: 16px 0 8px; font-weight: 600; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .card {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; padding: 14px; }}
  .label {{ color: #94a3b8; font-size: 12px; text-transform: uppercase; letter-spacing: 0.04em; }}
  .value {{ font-size: 24px; font-weight: 700; margin-top: 6px; }}
  .sub {{ color: #94a3b8; font-size: 12px; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 8px; background: #1e293b; border-radius: 8px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #334155; font-size: 13px; }}
  th {{ color: #94a3b8; font-weight: 600; }}
  svg {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; width: 100%; max-width: 720px; }}
</style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <div class="grid">
    <div class="card"><div class="label">Runs</div><div class="value">{stats['total_runs']}</div><div class="sub">Verified: {_fmt_pct(stats['verified_rate'])}</div></div>
    <div class="card"><div class="label">Autonomy (Zero-Touch)</div><div class="value">{_fmt_pct(stats['autonomy_rate'])}</div><div class="sub">{stats['zero_touch_runs']} / {stats['merged_runs']} merged</div></div>
    <div class="card"><div class="label">Reference Spend</div><div class="value">${stats['total_cost_usd']:.2f}</div><div class="sub">Median: ${stats['median_cost_usd']:.2f}</div></div>
    <div class="card"><div class="label">Cache Hit Rate</div><div class="value">{_fmt_pct(stats['cache_hit_rate'])}</div><div class="sub">{stats['total_tokens']:,} total tokens</div></div>
  </div>
  <h2>Reference Spend by Model</h2>
  <svg viewBox="0 0 700 {svg_height}" xmlns="http://www.w3.org/2000/svg">{"".join(bar_items)}</svg>
  {scorer_cards_html}
  <h2>PR Size Buckets</h2>
  <table><thead><tr><th>Size</th><th>Runs</th><th>Merged</th><th>Zero-Touch</th><th>Median Cycle</th></tr></thead><tbody>{size_rows}</tbody></table>
</body>
</html>
"""
