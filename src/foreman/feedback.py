"""Collect redacted human review and triage feedback signals from GitHub."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable

from foreman.redaction import redact_text

GhRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _default_gh_runner(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None


def _within_window(record: dict[str, Any], cutoff: datetime) -> bool:
    for key in ("closedAt", "mergedAt", "updatedAt", "createdAt"):
        dt = _parse_ts(record.get(key))
        if dt is not None:
            return dt >= cutoff
    return True


def _is_human_comment(login: object, body: object) -> bool:
    if not isinstance(body, str) or not body.strip():
        return False
    if body.lstrip().startswith("Reviewed by "):
        return False
    if isinstance(login, str):
        lower = login.lower()
        if lower.endswith("[bot]") or lower.startswith("app/") or "foreman" in lower:
            return False
    return True


def collect_review_feedback(
    repo: str,
    *,
    since_days: int = 14,
    runner: GhRunner | None = None,
) -> list[dict[str, Any]]:
    """Gather human PR review overrides, follow-up commits, and closed-unmerged PRs."""
    run = runner or _default_gh_runner
    proc = run([
        "gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "30",
        "--json", "number,title,url,state,createdAt,closedAt,mergedAt,comments,reviews,commits",
    ])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"gh pr list exited {proc.returncode}")
    prs = json.loads(proc.stdout or "[]")
    if not isinstance(prs, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    items: list[dict[str, Any]] = []
    for pr in prs:
        if not isinstance(pr, dict) or not _within_window(pr, cutoff):
            continue
        signals: list[str] = []
        notes: list[str] = []

        if str(pr.get("state") or "").upper() == "CLOSED" and not pr.get("mergedAt"):
            signals.append("closed_without_merge")

        for rev in pr.get("reviews") or []:
            if not isinstance(rev, dict):
                continue
            login = ((rev.get("author") or {}) if isinstance(rev.get("author"), dict) else {}).get("login")
            if str(rev.get("state") or "").upper() == "CHANGES_REQUESTED" and _is_human_comment(login, rev.get("body") or " "):
                signals.append("changes_requested")
                if rev.get("body"):
                    notes.append(redact_text(str(rev["body"])[:300], redact_pii=True))

        for comment in pr.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            login = ((comment.get("author") or {}) if isinstance(comment.get("author"), dict) else {}).get("login")
            if _is_human_comment(login, comment.get("body")):
                signals.append("human_comment")
                notes.append(redact_text(str(comment["body"])[:300], redact_pii=True))

        commits = pr.get("commits") or []
        if isinstance(commits, list) and len(commits) > 1:
            signals.append(f"followup_commits:{len(commits) - 1}")

        if signals:
            items.append({
                "kind": "pr_feedback",
                "number": pr.get("number"),
                "url": str(pr.get("url") or ""),
                "title": redact_text(str(pr.get("title") or ""), redact_pii=True),
                "signals": sorted(set(signals)),
                "notes": notes[:5],
            })
    return items


def collect_triage_feedback(
    repo: str,
    *,
    since_days: int = 14,
    runner: GhRunner | None = None,
) -> list[dict[str, Any]]:
    """Gather relabelled, reopened, or closed-not-planned issue triage signals."""
    run = runner or _default_gh_runner
    proc = run([
        "gh", "issue", "list", "--repo", repo, "--state", "all", "--limit", "40",
        "--json", "number,title,url,state,stateReason,labels,comments,createdAt,closedAt,updatedAt",
    ])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"gh issue list exited {proc.returncode}")
    issues = json.loads(proc.stdout or "[]")
    if not isinstance(issues, list):
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=since_days)
    items: list[dict[str, Any]] = []
    for issue in issues:
        if not isinstance(issue, dict) or not _within_window(issue, cutoff):
            continue
        labels = [
            str(lbl.get("name") or "").lower()
            for lbl in (issue.get("labels") or [])
            if isinstance(lbl, dict)
        ]
        reason = str(issue.get("stateReason") or "").upper()
        signals: list[str] = []
        notes: list[str] = []

        if reason == "NOT_PLANNED":
            signals.append("closed_not_planned")
        elif reason == "REOPENED":
            signals.append("reopened")

        if any(lbl in {"needs-human", "reopened", "wontfix", "invalid"} for lbl in labels):
            signals.append("label_override")

        for comment in issue.get("comments") or []:
            if not isinstance(comment, dict):
                continue
            login = ((comment.get("author") or {}) if isinstance(comment.get("author"), dict) else {}).get("login")
            body = str(comment.get("body") or "")
            if _is_human_comment(login, body) and any(
                kw in body.lower() for kw in ("reopen", "wrong slice", "misrouted", "split", "spec")
            ):
                signals.append("human_triage_note")
                notes.append(redact_text(body[:300], redact_pii=True))

        if signals:
            items.append({
                "kind": "triage_feedback",
                "number": issue.get("number"),
                "url": str(issue.get("url") or ""),
                "title": redact_text(str(issue.get("title") or ""), redact_pii=True),
                "signals": sorted(set(signals)),
                "notes": notes[:5],
            })
    return items
