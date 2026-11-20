"""Closed-loop self-improvement observer with hard write-surface and cadence guards."""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import difflib
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable, Mapping
import uuid

from foreman.config import ConfigError, family
from foreman.feedback import GhRunner, collect_review_feedback, collect_triage_feedback
from foreman.redaction import redact_text
from foreman.telemetry import TelemetryStore, utcnow_iso
from foreman.worker import CommandRunner, json_object, model_and_effort, run_worker_command

PACKAGE = Path(__file__).parent
IMPROVE_STAGE = PACKAGE / "stages" / "improve.md"

ALLOWED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^src/foreman/stages/[A-Za-z0-9._-]+\.md$"),
    re.compile(r"^src/foreman/skills/[A-Za-z0-9._-]+\.md$"),
    re.compile(r"^evals/[A-Za-z0-9._/-]+$"),
    re.compile(r"^\.agents/skills/[A-Za-z0-9._-]+-local/[A-Za-z0-9._/-]+$"),
)

BLOCKED_BASENAMES = {
    "models.toml",
    "measure.toml",
    "pricing.toml",
    "agents.md",
    "delivery.py",
    "save.py",
    "config.py",
    "runner.py",
    "worker.py",
    "cli.py",
    "telemetry.py",
    "scoring.py",
    "improve.py",
    "watcher.py",
}

_IMPROVE_EDIT_ITEM = {
    "type": "object",
    "required": ["path", "content"],
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "additionalProperties": False,
}

IMPROVE_SCHEMA = {
    "type": "object",
    "required": ["action", "reason", "title", "summary", "regressions_addressed", "edits"],
    "properties": {
        "action": {"type": "string", "enum": ["no_change", "propose"]},
        "reason": {"type": ["string", "null"]},
        "title": {"type": ["string", "null"]},
        "summary": {"type": ["string", "null"]},
        "regressions_addressed": {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]},
        "edits": {"anyOf": [{"type": "array", "items": _IMPROVE_EDIT_ITEM}, {"type": "null"}]},
    },
    "additionalProperties": False,
}

NON_INFRA_FAIL_STOPS = {
    "verify_cap",
    "review_cap",
    "invalid_output",
    "no_reviewer",
    "missing_slice",
    "dirty_worktree",
}


class WriteSurfaceError(ValueError):
    pass


ObserverRunner = Callable[[dict[str, Any]], dict[str, Any]]


def check_write_surface(paths: Iterable[str]) -> list[str]:
    """Validate every target path is strictly within the allowed self-improvement surface."""
    normalized: list[str] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.strip():
            raise WriteSurfaceError("empty path is not allowed")
        path = raw.strip()
        if "\\" in path or path.startswith("/") or ".." in Path(path).parts:
            raise WriteSurfaceError(f"path escapes write surface: {path}")
        if path.startswith(".github/") or path == ".github":
            raise WriteSurfaceError(f"blocked workflow path: {path}")
        if Path(path).name.lower() in BLOCKED_BASENAMES:
            raise WriteSurfaceError(f"protected config or engine file cannot be modified: {path}")
        if not any(pattern.fullmatch(path) for pattern in ALLOWED_PATTERNS):
            raise WriteSurfaceError(
                f"path outside allowed self-improvement surface ({path}): "
                "allowed prefixes are src/foreman/stages/, src/foreman/skills/, evals/, "
                "or .agents/skills/*-local/"
            )
        normalized.append(path)
    return normalized


def _assert_contained_path(repo_root: Path, rel_path: str) -> Path:
    """Resolve rel_path under repo_root, refusing symlinked components or escapes."""
    root = repo_root.resolve(strict=True)
    current = root
    for part in Path(rel_path).parts:
        current = current / part
        if current.is_symlink():
            raise WriteSurfaceError(f"path traverses a symlink: {rel_path}")
    if not current.resolve().is_relative_to(root):
        raise WriteSurfaceError(f"path escapes repository root: {rel_path}")
    return current


def _assert_public_safe(text: str, what: str) -> None:
    if redact_text(text, redact_pii=True) != text:
        raise WriteSurfaceError(f"{what} contains sensitive secret or PII pattern")


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_root,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _inside_git_worktree(repo_root: Path) -> bool:
    return _git(repo_root, "rev-parse", "--is-inside-work-tree").returncode == 0


def _assert_clean_worktree(repo_root: Path) -> None:
    status = _git(repo_root, "status", "--porcelain")
    if status.returncode != 0:
        raise WriteSurfaceError("could not read git status before improvement edits")
    if status.stdout.strip():
        raise WriteSurfaceError("worktree has uncommitted changes; improvement PR needs a clean checkout")


@contextlib.contextmanager
def _connect_db(db_path: Path):
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS improvement_runs (
                proposal_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                mode TEXT NOT NULL,
                action TEXT NOT NULL,
                title TEXT DEFAULT '',
                files_json TEXT DEFAULT '[]',
                pr_url TEXT DEFAULT ''
            )
            """
        )
        with conn:
            yield conn
    finally:
        conn.close()


def last_proposal_timestamp(db_path: Path) -> datetime | None:
    with _connect_db(db_path) as conn:
        row = conn.execute(
            "SELECT created_at FROM improvement_runs WHERE action = 'propose' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None or not row["created_at"]:
        return None
    try:
        return datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None


def record_improvement_run(
    db_path: Path,
    *,
    mode: str,
    action: str,
    title: str = "",
    files: Iterable[str] = (),
    pr_url: str = "",
    created_at: str | None = None,
) -> str:
    pid = f"imp-{uuid.uuid4().hex[:10]}"
    ts = created_at or utcnow_iso()
    with _connect_db(db_path) as conn:
        conn.execute(
            "INSERT INTO improvement_runs (proposal_id, created_at, mode, action, title, files_json, pr_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, ts, mode, action, title, json.dumps(list(files)), pr_url),
        )
    return pid


def collect_improvement_bundle(
    store: TelemetryStore,
    *,
    repo: str | None = None,
    days: int = 7,
    min_failing_runs: int = 3,
    mode: str = "all",
    review_feedback: list[dict[str, Any]] | None = None,
    triage_feedback: list[dict[str, Any]] | None = None,
    gh_runner: GhRunner | None = None,
) -> dict[str, Any]:
    """Gather failing runs, failing LLM scorer grades, and optional GitHub human feedback."""
    since_dt = datetime.now(timezone.utc) - timedelta(days=days)
    since_iso = since_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    runs = store.list_runs(repo=repo, since=since_iso, limit=1000)
    run_by_id = {r["run_id"]: r for r in runs}

    failing_runs: list[dict[str, Any]] = []
    failing_ids: set[str] = set()
    for r in runs:
        if r.get("stop") in NON_INFRA_FAIL_STOPS:
            failing_ids.add(r["run_id"])
            failing_runs.append({
                "run_id": r["run_id"],
                "repo": r["repo"],
                "issue": r["issue"],
                "stop": r["stop"],
                "failure_phase": r.get("failure_phase", ""),
                "halt_detail": redact_text((r.get("state") or {}).get("halt_detail") or "", redact_pii=True),
                "models_used": r.get("models_used") or {},
            })

    failing_scores: list[dict[str, Any]] = []
    if mode in {"all", "scorer"}:
        for sc in store.list_scores():
            rid = sc["run_id"]
            if rid not in run_by_id:
                continue
            if sc.get("label") == "insufficient_evidence" or sc.get("passed"):
                continue
            failing_ids.add(rid)
            failing_scores.append({
                "run_id": rid,
                "scorer": sc["scorer"],
                "label": sc["label"],
                "score": sc["score"],
                "reason": redact_text(sc.get("reason") or "", redact_pii=True),
            })

    # Feedback from PRs and issues foreman never touched says nothing about its prompts.
    tracked = store.list_runs(repo=repo, limit=10000) if repo else []
    known_pr_urls = {str(r["pr_url"]) for r in tracked if r.get("pr_url")}
    head_shas: dict[str, str] = {}
    for r in tracked:  # newest first, so the newest run's head wins for a shared PR
        if r.get("pr_url") and r.get("head_sha"):
            head_shas.setdefault(str(r["pr_url"]), str(r["head_sha"]))
    known_issues = {r["issue"] for r in tracked if isinstance(r.get("issue"), int)}

    rev_items: list[dict[str, Any]] = []
    if mode in {"all", "review"}:
        if review_feedback is not None:
            rev_items = list(review_feedback)
        elif repo and known_pr_urls:
            rev_items = collect_review_feedback(
                repo,
                since_days=max(days, 14),
                runner=gh_runner,
                known_pr_urls=known_pr_urls,
                head_shas=head_shas,
            )

    tri_items: list[dict[str, Any]] = []
    if mode in {"all", "triage"}:
        if triage_feedback is not None:
            tri_items = list(triage_feedback)
        elif repo and known_issues:
            tri_items = collect_triage_feedback(
                repo, since_days=max(days, 14), runner=gh_runner, known_issues=known_issues
            )

    return {
        "mode": mode,
        "repo": repo or "",
        "window_days": days,
        "min_failing_runs": min_failing_runs,
        "failing_runs": failing_runs,
        "failing_scores": failing_scores,
        "review_feedback": rev_items,
        "triage_feedback": tri_items,
        "failing_count": len(failing_ids) + len(rev_items) + len(tri_items),
    }


def _read_editable_prompts(repo_root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for rel_dir in ("src/foreman/stages", "src/foreman/skills"):
        folder = repo_root / rel_dir
        if folder.is_dir():
            for path in sorted(folder.glob("*.md")):
                rel = path.relative_to(repo_root).as_posix()
                snapshot[rel] = path.read_text(encoding="utf-8")
    return snapshot


def default_observer_runner(
    *,
    repo_root: Path,
    model: str = "claude-sonnet-5-5-high",
    run: CommandRunner = run_worker_command,
) -> ObserverRunner:
    """Create a read-only CLI observer calling IMPROVE_STAGE against redacted failure evidence."""
    instruction = IMPROVE_STAGE.read_text(encoding="utf-8").strip()

    def _observe(bundle: dict[str, Any]) -> dict[str, Any]:
        prompts = _read_editable_prompts(repo_root)
        prompt = (
            f"{instruction}\n\n"
            f"Current editable files:\n{json.dumps(prompts)}\n\n"
            f"Failure & feedback bundle (already redacted):\n{json.dumps(bundle)}"
        )
        slug, effort = model_and_effort(model)
        try:
            provider = family(slug)
        except ConfigError:
            provider = "claude"
        with TemporaryDirectory(prefix="foreman_improve_") as tmpdir:
            if provider == "gpt":
                tmp = Path(tmpdir)
                schema_file = tmp / "schema.json"
                out_file = tmp / "proposal.json"
                schema_file.write_text(json.dumps(IMPROVE_SCHEMA), encoding="utf-8")
                effort_args = ["-c", f"model_reasoning_effort={effort}"] if effort else []
                args = [
                    "codex", "exec", "-m", slug, *effort_args,
                    "--color", "never", "-s", "read-only",
                    "--json", "--output-schema", str(schema_file), "-o", str(out_file),
                    prompt,
                ]
                proc = run(args, repo_root)
                if proc.returncode != 0:
                    raise RuntimeError(f"observer codex failed: {proc.stderr or proc.stdout}")
                file_raw = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
                if file_raw.strip():
                    return json_object(file_raw)
                from foreman.worker import _parse_codex_events
                ev_body, _ = _parse_codex_events(proc.stdout, slug)
                return json_object(ev_body if ev_body is not None else proc.stdout)

            options = [
                "--output-format", "json",
                "--json-schema", json.dumps(IMPROVE_SCHEMA),
                "--permission-mode", "plan",
            ]
            if provider == "claude":
                effort_args = ["--effort", effort] if effort else []
                args = ["claude", "--model", slug, *effort_args, *options, "-p", prompt]
            else:
                effort_args = ["--reasoning-effort", effort] if effort else []
                args = ["grok", "-m", slug, *effort_args, *options, "--no-subagents", "-p", prompt]
            proc = run(args, repo_root)
            if proc.returncode != 0:
                raise RuntimeError(f"observer {provider} failed: {proc.stderr or proc.stdout}")
            envelope = json_object(proc.stdout)
            structured = (
                envelope.get("structured_output")
                if envelope.get("structured_output") is not None
                else envelope.get("structuredOutput")
            )
            if isinstance(structured, dict):
                return structured
            if isinstance(structured, str) and structured.strip():
                return json_object(structured)
            return json_object(envelope.get("result" if provider == "claude" else "text"))

    return _observe


def render_improvement_pr_body(proposal: Mapping[str, Any], paths: Iterable[str]) -> str:
    """Format a public-safe improvement PR body referencing regressions addressed."""
    summary = redact_text(str(proposal.get("summary") or "Refine stage instructions from telemetry feedback."), redact_pii=True)
    regressions = [
        redact_text(str(item), redact_pii=True)
        for item in (proposal.get("regressions_addressed") or [])
        if str(item).strip()
    ]
    reg_lines = "\n".join(f"- {item}" for item in regressions) or "- Aggregated telemetry failure cluster"
    file_lines = "\n".join(f"- `{p}`" for p in paths)
    body = (
        f"## Summary\n{summary}\n\n"
        f"## Regressions addressed\n{reg_lines}\n\n"
        f"## Files changed\n{file_lines}\n\n"
        "This automated proposal does not modify `models.toml`, `measure.toml`, or Python graph routing, "
        "and never arms auto-merge."
    )
    return redact_text(body, redact_pii=True)


def run_self_improvement(
    store: TelemetryStore,
    repo_root: Path,
    *,
    repo: str | None = None,
    mode: str = "all",
    days: int = 7,
    min_failing_runs: int = 3,
    now: bool = False,
    dry_run: bool = True,
    open_pr: bool = False,
    observer: ObserverRunner | None = None,
    review_feedback: list[dict[str, Any]] | None = None,
    triage_feedback: list[dict[str, Any]] | None = None,
    gh_runner: GhRunner | None = None,
    current_time: datetime | None = None,
) -> dict[str, Any]:
    """Run one guarded self-improvement observation and proposal cycle."""
    clock_now = current_time or datetime.now(timezone.utc)
    if not now:
        prev_ts = last_proposal_timestamp(store.path)
        if prev_ts is not None and (clock_now - prev_ts) < timedelta(days=7):
            return {
                "status": "skipped",
                "reason": "cadence_window",
                "last_proposal_at": prev_ts.isoformat().replace("+00:00", "Z"),
            }

    bundle = collect_improvement_bundle(
        store,
        repo=repo,
        days=days,
        min_failing_runs=min_failing_runs,
        mode=mode,
        review_feedback=review_feedback,
        triage_feedback=triage_feedback,
        gh_runner=gh_runner,
    )
    if not now and bundle["failing_count"] < min_failing_runs:
        return {
            "status": "skipped",
            "reason": "below_threshold",
            "failing_count": bundle["failing_count"],
            "min_failing_runs": min_failing_runs,
        }

    observe = observer or default_observer_runner(repo_root=repo_root)
    decision = observe(bundle)
    if not isinstance(decision, dict):
        raise WriteSurfaceError("observer did not return a JSON object")

    action = str(decision.get("action") or "").strip()
    if action == "no_change":
        reason = redact_text(str(decision.get("reason") or "No prompt change warranted."), redact_pii=True)
        if not dry_run:
            record_improvement_run(store.path, mode=mode, action="no_change", title=reason[:120])
        return {"status": "no_change", "reason": reason, "bundle": bundle}
    if action != "propose":
        raise WriteSurfaceError(f"invalid observer action {action!r}")

    raw_edits = decision.get("edits")
    if not isinstance(raw_edits, list) or not raw_edits:
        raise WriteSurfaceError("propose action requires at least one edit")

    edit_paths: list[str] = []
    cleaned_edits: list[tuple[str, str]] = []
    for item in raw_edits:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            raise WriteSurfaceError("each edit requires string path and content")
        p = str(item.get("path") or "").strip()
        edit_paths.append(p)
        cleaned_edits.append((p, item["content"]))

    validated_paths = check_write_surface(edit_paths)
    targets = [_assert_contained_path(repo_root, rel_path) for rel_path in validated_paths]
    for rel_path, (_, new_content) in zip(validated_paths, cleaned_edits):
        _assert_public_safe(new_content, f"proposed edit {rel_path}")

    diffs: dict[str, str] = {}
    for rel_path, target, (_, new_content) in zip(validated_paths, targets, cleaned_edits):
        before = target.read_text(encoding="utf-8") if target.exists() else ""
        diff_lines = difflib.unified_diff(
            before.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        )
        # Context and removed lines come from files already on disk, so scrub the whole diff.
        diffs[rel_path] = redact_text("".join(diff_lines), redact_pii=True)

    title = redact_text(str(decision.get("title") or "Tighten foreman instructions").strip(), redact_pii=True)
    pr_body = render_improvement_pr_body(decision, validated_paths)

    if dry_run:
        return {
            "status": "dry_run",
            "title": title,
            "paths": validated_paths,
            "diffs": diffs,
            "pr_body": pr_body,
        }

    if open_pr or _inside_git_worktree(repo_root):
        _assert_clean_worktree(repo_root)
    for rel_path, (_, new_content) in zip(validated_paths, cleaned_edits):
        dest = _assert_contained_path(repo_root, rel_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(new_content, encoding="utf-8")

    pr_url = ""
    if open_pr:
        pr_url = _open_improvement_pr(
            repo_root, validated_paths, repo=repo, title=title, pr_body=pr_body, gh_runner=gh_runner
        )

    record_improvement_run(
        store.path,
        mode=mode,
        action="propose",
        title=title,
        files=validated_paths,
        pr_url=pr_url,
        created_at=clock_now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    )
    return {
        "status": "applied",
        "title": title,
        "paths": validated_paths,
        "diffs": diffs,
        "pr_body": pr_body,
        "pr_url": pr_url,
    }


def _open_improvement_pr(
    repo_root: Path,
    paths: list[str],
    *,
    repo: str | None,
    title: str,
    pr_body: str,
    gh_runner: GhRunner | None = None,
) -> str:
    branch = f"improve/{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    if _git(repo_root, "checkout", "-b", branch).returncode != 0:
        raise RuntimeError("could not create improvement branch")
    if _git(repo_root, "add", "--", *paths).returncode != 0:
        raise RuntimeError("could not stage improvement edits")
    staged = _git(repo_root, "diff", "--cached", "--name-only")
    staged_paths = {ln.strip() for ln in staged.stdout.splitlines() if ln.strip()}
    if staged.returncode != 0 or staged_paths != set(paths):
        raise WriteSurfaceError(f"staged paths do not match validated edits: {sorted(staged_paths)}")
    check_write_surface(sorted(staged_paths))
    staged_diff = _git(repo_root, "diff", "--cached", "--no-color")
    if staged_diff.returncode != 0:
        raise RuntimeError("could not read staged improvement diff")
    _assert_public_safe(staged_diff.stdout, "staged improvement diff")
    if _git(repo_root, "commit", "-m", title).returncode != 0:
        raise RuntimeError("could not commit improvement edits")

    run_gh = gh_runner or (
        lambda cmd: subprocess.run(cmd, cwd=repo_root, shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    )
    if _git(repo_root, "push", "-u", "origin", branch).returncode != 0:
        raise RuntimeError("could not push improvement branch")
    cmd = ["gh", "pr", "create"]
    if repo:
        cmd.extend(["--repo", repo])
    cmd.extend(["--head", branch, "--title", title, "--body", pr_body])
    created = run_gh(cmd)
    if created.returncode != 0:
        raise RuntimeError(created.stderr.strip() or created.stdout.strip() or "gh pr create failed")
    return (created.stdout or "").strip().splitlines()[-1]
