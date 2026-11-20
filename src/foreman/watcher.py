"""Watch delivery runs for halts, failovers, and odd outputs and propose graph fixes."""

import re
from typing import TypedDict

from foreman.github import GitHub

TOOLS_REPO = "bookout-labs/tools"


class Proposal(TypedDict):
    kind: str
    title: str
    target: str
    evidence: str
    summary: str
    file_issue: bool


def _format_attempt(attempt: dict) -> str:
    node = attempt.get("node") or "unknown"
    model = attempt.get("model") or "none"
    status = attempt.get("status") or "unknown"
    detail = " ".join(str(attempt.get("detail") or "").split())[:100]
    base = f"{node}/{model}={status}"
    return f"{base}({detail})" if detail else base


def _format_evidence(state: dict, stop: str) -> str:
    parts: list[str] = []
    repo = state.get("repo")
    issue = state.get("issue")
    if isinstance(repo, str) and repo:
        ref = f"{repo}#{issue}" if isinstance(issue, int) and not isinstance(issue, bool) and issue > 0 else repo
        parts.append(ref)
    parts.append(f"stop={stop}")
    detail = " ".join(str(state.get("halt_detail") or "").split())[:200]
    if detail:
        parts.append(f"detail={detail}")
    attempts = state.get("attempts") or []
    selected = (
        attempts
        if state.get("halt") or stop in {"verify_cap", "review_cap"}
        else [a for a in attempts if a.get("status") != "ran"]
    )
    if selected:
        parts.append("attempts=" + ", ".join(_format_attempt(a) for a in selected))
    return "; ".join(parts)


def _make_graph_proposal(title: str, target: str, cause: str, evidence: str) -> Proposal:
    if "models.toml" in target or target.endswith("models.toml"):
        raise ValueError("watcher cannot target models.toml")
    summary = (
        f"Proposed graph fix: {title}\n"
        f"Cause: {cause}\n"
        f"Evidence: {evidence}\n"
        f"File to change: {target}\n"
        "This proposal does not change models.toml or max_review_rounds.\n"
        "It does not edit the graph, merge, or launch a coding agent.\n"
        "A person opens the pull request."
    )
    return {
        "kind": "graph_fix",
        "title": title,
        "target": target,
        "evidence": evidence,
        "summary": summary,
        "file_issue": True,
    }


def _make_quota_proposal(title: str, cause: str, evidence: str) -> Proposal:
    summary = (
        f"Quota report: {title}\n"
        f"Cause: {cause}\n"
        f"Evidence: {evidence}\n"
        "No graph change.\n"
        "This proposal does not change models.toml or max_review_rounds.\n"
        "It does not edit the graph, merge, or launch a coding agent."
    )
    return {
        "kind": "quota",
        "title": title,
        "target": "",
        "evidence": evidence,
        "summary": summary,
        "file_issue": False,
    }


def _missing_cli(missing_attempts: list[dict], detail: str) -> str:
    sources = [str(a.get("detail") or "") for a in missing_attempts] + [detail]
    for text in sources:
        match = re.search(r"\b(codex|claude|grok)\b", text, re.IGNORECASE)
        if match:
            return match.group(1).lower()
    for attempt in missing_attempts:
        model = attempt.get("model")
        if isinstance(model, str) and model:
            return model
    return "worker"


def _invalid_output_proposal(node: str, detail: str, evidence: str) -> Proposal:
    lower = detail.lower()
    if "usage reported" in lower or "rerouted" in lower or "reported model" in lower:
        return _make_graph_proposal(
            f"Fix worker model verification on {node}",
            "src/foreman/worker.py",
            "Worker model verification rejected the CLI response.",
            evidence,
        )
    if "worker exited" in lower or "worker reported an error" in lower:
        return _make_graph_proposal(
            f"Fix worker CLI invocation on {node}",
            "src/foreman/worker.py",
            "Worker CLI invocation failed or returned an error.",
            evidence,
        )
    if "did not return a json object" in lower or "response keys do not match" in lower:
        return _make_graph_proposal(
            f"Fix {node} worker JSON response contract",
            "src/foreman/worker.py",
            "Worker output did not match the stage JSON contract.",
            evidence,
        )
    if "spec did not return text" in lower or "spec gate" in lower:
        return _make_graph_proposal(
            "Fix spec output shape handling",
            "src/foreman/stages/spec.md",
            "Spec stage output did not match the expected document shape in "
            "src/foreman/stages/spec.md and src/foreman/delivery.py.",
            evidence,
        )
    if "slice" in lower or "dependency" in lower or "dependencies" in lower:
        return _make_graph_proposal(
            "Fix spec slice schema handling",
            "src/foreman/stages/spec.md",
            "Spec slices violated the slice schema or dependency rules.",
            evidence,
        )
    if "merge risk" in lower or "review risk" in lower or "review must be" in lower:
        return _make_graph_proposal(
            "Fix review output schema handling",
            "src/foreman/stages/review.md",
            "Review output missed required verdict or merge risk fields.",
            evidence,
        )
    if "could not commit" in lower or "could not create branch" in lower or "could not stage" in lower:
        return _make_graph_proposal(
            "Commit the implementation from the runner",
            "src/foreman/runner.py",
            "The implementation was still uncommitted when the runner tried to record it.",
            evidence,
        )
    if "triage must be" in lower:
        return _make_graph_proposal(
            "Fix triage output contract",
            "src/foreman/stages/triage.md",
            "Triage output was not a valid routing outcome.",
            evidence,
        )
    return _make_graph_proposal(
        f"Fix invalid output on {node}",
        "src/foreman/delivery.py",
        "Stage output failed delivery validation.",
        evidence,
    )


def watch_run(state: dict, stop: str) -> Proposal | None:
    attempts = state.get("attempts") or []
    detail = (state.get("halt_detail") or "").strip()
    path = state.get("path") or []
    node = next((n for n in reversed(path) if n != "done"), "delivery")

    stopped_attempts = [a for a in attempts if a.get("status") == "stopped"]
    missing_attempts = [a for a in attempts if a.get("status") == "cli_missing"]
    quota_attempts = [a for a in attempts if a.get("status") == "quota"]
    evidence = _format_evidence(state, stop)
    lower = detail.lower()

    if stop == "quota_exhausted" or (stop == "no_reviewer" and "quota exhausted" in lower):
        return _make_quota_proposal(
            "Quota exhausted across configured models",
            "Every configured model reported quota exhaustion.",
            evidence,
        )
    if stop == "cli_missing" or (stop == "no_reviewer" and ("cli missing" in lower or "cli is not installed" in lower)):
        cli = _missing_cli(missing_attempts, detail)
        return _make_graph_proposal(
            f"Handle missing {cli} CLI",
            "src/foreman/worker.py",
            "A configured worker CLI was not installed.",
            evidence,
        )
    if stop == "invalid_output":
        return _invalid_output_proposal(node, detail, evidence)
    if stop == "missing_slice":
        return _make_graph_proposal(
            "Fix slice issue metadata loading",
            "src/foreman/runner.py",
            "Slice issue did not contain valid fenced JSON metadata.",
            evidence,
        )
    if stop == "dirty_worktree":
        return _make_graph_proposal(
            "Handle dirty worktree before verify",
            "src/foreman/runner.py",
            "Verify halted because the checkout was dirty or HEAD was invalid.",
            evidence,
        )
    if stop == "insufficient_evidence":
        return _make_graph_proposal(
            "Fix missing proof evidence from implement",
            "src/foreman/stages/implement.md",
            "Implementation finished without required browser or formal proof evidence.",
            evidence,
        )
    if stop == "verify_cap":
        return _make_graph_proposal(
            "Investigate repeated verify failures reaching cap",
            "src/foreman/stages/implement.md",
            "Checks failed across all allowed review rounds.",
            evidence,
        )
    if stop == "review_cap":
        return _make_graph_proposal(
            "Investigate repeated review or proof blocks reaching cap",
            "src/foreman/stages/implement.md",
            "Review or proof blocked across all allowed rounds.",
            evidence,
        )
    if stop == "no_reviewer":
        return _make_graph_proposal(
            "Fix reviewer eligibility selection",
            "src/foreman/delivery.py",
            "No eligible reviewer remained outside the builder families.",
            evidence,
        )
    if stop == "no_dispatcher":
        return _make_graph_proposal(
            "Fix missing worker dispatcher configuration",
            "src/foreman/cli.py",
            "Run started without a configured worker dispatcher.",
            evidence,
        )
    if stop == "save_failed":
        return _make_graph_proposal(
            "Fix reviewed commit publish step",
            "src/foreman/save.py",
            "Publishing the reviewed commit failed before merge.",
            evidence,
        )
    if stop == "merge_held" and detail.startswith("could not read FOREMAN_MERGE_HOLD"):
        return _make_graph_proposal(
            "Fix FOREMAN_MERGE_HOLD lookup failure",
            "src/foreman/cli.py",
            "Reading the repository merge-hold variable failed.",
            evidence,
        )

    if stopped_attempts:
        stopped_node = stopped_attempts[0].get("node") or node
        return _make_graph_proposal(
            f"Investigate stopped attempt on {stopped_node}",
            "src/foreman/worker.py",
            "An earlier worker attempt stopped with an invalid result.",
            evidence,
        )
    if missing_attempts:
        cli = _missing_cli(missing_attempts, detail)
        return _make_graph_proposal(
            f"Handle missing {cli} CLI",
            "src/foreman/worker.py",
            "A configured worker CLI was not installed during failover.",
            evidence,
        )
    if quota_attempts:
        return _make_quota_proposal(
            "Quota failover to next configured model",
            "Quota failed over to the next configured model.",
            evidence,
        )
    return None


def file_proposal_issue(proposal: Proposal | None, client: GitHub, *, repo: str = TOOLS_REPO) -> int | None:
    if proposal is None or not proposal.get("file_issue"):
        return None
    existing = {title.strip() for title in client.open_issue_titles(repo)}
    title = proposal["title"].strip()
    if title in existing:
        return None
    return client.create_issue(repo, title, proposal["summary"], [])
