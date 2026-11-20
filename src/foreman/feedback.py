"""Collect redacted human review and triage feedback signals from GitHub."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
from typing import Any, Callable, Mapping

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


def is_human_actor(login: object) -> bool:
    if not isinstance(login, str) or not login.strip():
        return False
    lower = login.strip().lower()
    return not (lower.endswith("[bot]") or lower.startswith("app/") or "foreman" in lower)


def is_foreman_body(body: object) -> bool:
    return isinstance(body, str) and body.lstrip().startswith("Reviewed by ")


def author_login(record: dict[str, Any]) -> object:
    author = record.get("author")
    return author.get("login") if isinstance(author, dict) else None


def _is_human_comment(record: dict[str, Any]) -> bool:
    body = record.get("body")
    return (
        is_human_actor(author_login(record))
        and isinstance(body, str)
        and bool(body.strip())
        and not is_foreman_body(body)
    )


def _commit_authors(commit: dict[str, Any]) -> list[dict[str, Any]]:
    authors = commit.get("authors")
    people = [a for a in authors if isinstance(a, dict)] if isinstance(authors, list) else []
    if isinstance(commit.get("author"), dict):
        people.append(commit["author"])
    return people


def _commit_is_human(commit: dict[str, Any]) -> bool:
    for author in _commit_authors(commit):
        login = author.get("login")
        if isinstance(login, str) and login.strip():
            if is_human_actor(login):
                return True
            continue
        email = str(author.get("email") or "").lower()
        if email and "[bot]" not in email and "foreman" not in email:
            return True
    return False


def human_followup_commits(commits: object, head_sha: str | None = None) -> int:
    """Count commits a person added: after the run's head when known, else by human authorship."""
    items = [c for c in commits if isinstance(c, dict)] if isinstance(commits, list) else []
    oids = [str(c.get("oid") or "").lower() for c in items]
    head = (head_sha or "").strip().lower()
    if head and head in oids:
        later = items[oids.index(head) + 1 :]
        return sum(1 for c in later if not _commit_authors(c) or _commit_is_human(c))
    return sum(1 for c in items if _commit_is_human(c))


def collect_review_feedback(
    repo: str,
    *,
    since_days: int = 14,
    runner: GhRunner | None = None,
    known_pr_urls: set[str] | None = None,
    head_shas: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Gather human PR review overrides, follow-up commits, and closed-unmerged PRs.

    ``known_pr_urls`` limits results to PRs foreman opened; ``head_shas`` maps a PR URL to the
    head foreman pushed so later commits count as human follow-ups.
    """
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
        url = str(pr.get("url") or "")
        if known_pr_urls is not None and url not in known_pr_urls:
            continue
        signals: list[str] = []
        notes: list[str] = []

        if str(pr.get("state") or "").upper() == "CLOSED" and not pr.get("mergedAt"):
            signals.append("closed_without_merge")

        for rev in pr.get("reviews") or []:
            if not isinstance(rev, dict) or str(rev.get("state") or "").upper() != "CHANGES_REQUESTED":
                continue
            if is_human_actor(author_login(rev)) and not is_foreman_body(rev.get("body")):
                signals.append("changes_requested")
                if isinstance(rev.get("body"), str) and rev["body"].strip():
                    notes.append(redact_text(rev["body"][:300], redact_pii=True))

        for comment in pr.get("comments") or []:
            if isinstance(comment, dict) and _is_human_comment(comment):
                signals.append("human_comment")
                notes.append(redact_text(str(comment["body"])[:300], redact_pii=True))

        followups = human_followup_commits(pr.get("commits"), (head_shas or {}).get(url))
        if followups:
            signals.append(f"followup_commits:{followups}")

        if signals:
            items.append({
                "kind": "pr_feedback",
                "number": pr.get("number"),
                "url": url,
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
    known_issues: set[int] | None = None,
) -> list[dict[str, Any]]:
    """Gather relabelled, reopened, or closed-not-planned issue triage signals.

    ``known_issues`` limits results to issues foreman ran.
    """
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
        if known_issues is not None and issue.get("number") not in known_issues:
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
            body = str(comment.get("body") or "")
            if _is_human_comment(comment) and any(
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
