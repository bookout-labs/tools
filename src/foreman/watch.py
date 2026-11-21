"""Propose one graph fix per halt and file, with one evidence write per run."""

from dataclasses import dataclass
from typing import NewType

from foreman.config import family
from foreman.delivery import DeliveryResult, HaltReason, WatchRecord
from foreman.github import Forge


@dataclass(frozen=True)
class Watch:
    title: str
    cause: str
    file: str


WATCH: dict[HaltReason, Watch | None] = {
    "invalid_output": Watch("Fix invalid output on {node}", "The {node} output did not satisfy its contract.", "src/foreman/stages/{node}.md"),
    "verify_cap": Watch("Investigate repeated verify failures reaching cap", "Verification kept failing after implementation.", "src/foreman/stages/implement.md"),
    "review_cap": Watch("Investigate repeated review or proof blocks reaching cap", "Review or proof remained blocked after implementation.", "src/foreman/stages/implement.md"),
    "spec_cap": Watch("Investigate rejected slice plans reaching cap", "Slice plans kept failing the spec gate.", "src/foreman/stages/spec.md"),
    "dirty_worktree": Watch("Handle dirty worktree before verify", "Implementation left a dirty worktree before verification.", "src/foreman/stages/implement.md"),
    "insufficient_evidence": Watch("Required proof is missing after implement", "Implementation did not supply the required proof.", "src/foreman/stages/implement.md"),
    "missing_slice": Watch("Slice issues carry a loadable slice", "The issue did not carry a loadable slice.", "src/foreman/github.py"),
    "review_unavailable": Watch("Review finds its pull request", "The pull request could not be loaded for review.", "src/foreman/github.py"),
    "review_not_posted": Watch("Reviews post to the pull request", "The accepted review could not be posted.", "src/foreman/github.py"),
    "pr_size_unavailable": Watch("Pull request size is measurable before review", "The pull request size could not be measured.", "src/foreman/workspace.py"),
    "git_failed": Watch("Git operations complete before publication", "A workspace git operation failed.", "src/foreman/workspace.py"),
    "usage_unknown": Watch("{adapter} attempts report usage", "The {adapter} attempt did not report usage.", "src/foreman/adapters/{adapter}.py"),
    "no_dispatcher": Watch("A live run always has a dispatcher", "The live run had no dispatcher.", "src/foreman/cli.py"),
    "skill_missing": Watch("Pinned skills are available before dispatch", "A required skill was unavailable at the pinned commit.", "src/foreman/skill_rules.py"),
    "no_reviewer": None,
    "cli_missing": None,
    "merge_held": None,
    "merge_not_armed": None,
    "pr_conflict": None,
    "ci_pending": None,
    "queue_rejected": None,
    "ci_red": None,
    "readiness_unavailable": None,
    "budget_exceeded": None,
    "env_unavailable": None,
    "quota_exhausted": None,
    "publish_failed": None,
    "implementation_rejected": None,
    "implement_quota": None,
    "failed": None,
    "design_rethink": None,
}

RunKey = NewType("RunKey", str)


def run_key(repo: str, issue: int, step: int) -> RunKey:
    return RunKey(f"{repo}#{issue}@{step}")


def watch(state: DeliveryResult, forge: Forge, repo: str, run_key: RunKey) -> WatchRecord | None:
    halt = state.get("halt")
    if halt is None or (row := WATCH[halt]) is None:
        return None
    values: dict[str, str] = {}
    if "{node}" in row.file:
        values["node"] = next(node for node in reversed(state["path"]) if node != "done")
    if "{adapter}" in row.file:
        model = state.get("attempts", [])[-1]["model"]
        adapter = family(model or "")
        values["adapter"] = "codex" if adapter == "gpt" else adapter
    file = row.file.format(**values)
    title = row.title.format(**values)
    marker = f"<!-- foreman-watch: {halt}@{file} -->"
    run_marker = f"<!-- foreman-watch-run: {run_key} -->"
    matches = sorted((issue for issue in forge.open_issues(repo) if marker in issue.body.splitlines()),
                     key=lambda issue: issue.number)
    for issue in matches:
        if run_marker in issue.body.splitlines():
            return {"issue": issue.number, "action": "filed"}
    for issue in matches:
        if any(run_marker in comment.splitlines() for comment in forge.issue_comments(repo, issue.number)):
            return {"issue": issue.number, "action": "commented"}
    detail = state.get("halt_detail", "").replace("\r", "\\r").replace("\n", "\\n")
    attempts = ", ".join(f"{a['stage']}/{a['model']}={a['outcome']}" for a in state.get("attempts", []))
    evidence = f"Evidence: {state['repo']}#{state.get('issue', '<issue>')}; stop={halt}; detail={detail}; attempts={attempts}"
    if matches:
        number = matches[0].number
        forge.comment_issue(repo, number, f"{evidence}\n{run_marker}")
        return {"issue": number, "action": "commented"}
    body = (
        f"Proposed graph fix: {title}\nCause: {row.cause.format(**values)}\n{evidence}\n"
        f"File to change: {file}\n"
        "This proposal does not change models.toml or max_review_rounds.\n"
        "It does not edit the graph, merge, or launch a coding agent.\n"
        "A person opens the pull request.\n"
        f"{run_marker}\n{marker}"
    )
    return {"issue": forge.create_issue(repo, title, body, []), "action": "filed"}
