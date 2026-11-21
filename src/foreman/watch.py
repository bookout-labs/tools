"""Propose graph fixes for halted runs through Forge only."""

from dataclasses import dataclass

from foreman.config import family
from foreman.delivery import DeliveryResult, HaltReason, WatchRecord
from foreman.github import Forge

DEFAULT_WATCH_REPO = "bookout-labs/tools"


@dataclass(frozen=True)
class Watch:
    title: str
    cause: str
    file: str


WATCH: dict[HaltReason, Watch | None] = {
    "invalid_output": Watch("Fix invalid output on {node}", "The {node} stage returned invalid output.", "src/foreman/stages/{node}.md"),
    "verify_cap": Watch("Investigate repeated verify failures reaching cap", "Implementation repeatedly failed verification.", "src/foreman/stages/implement.md"),
    "review_cap": Watch("Investigate repeated review or proof blocks reaching cap", "Implementation repeatedly failed review or proof requirements.", "src/foreman/stages/implement.md"),
    "spec_cap": Watch("Investigate rejected slice plans reaching cap", "Slice plans repeatedly failed acceptance.", "src/foreman/stages/spec.md"),
    "dirty_worktree": Watch("Handle dirty worktree before verify", "The worktree was dirty before verification.", "src/foreman/stages/implement.md"),
    "insufficient_evidence": Watch("Required proof is missing after implement", "Implementation did not provide required proof.", "src/foreman/stages/implement.md"),
    "missing_slice": Watch("Slice issues carry a loadable slice", "The slice issue did not contain a loadable slice.", "src/foreman/github.py"),
    "review_unavailable": Watch("Review finds its pull request", "Review could not load its pull request or diff.", "src/foreman/github.py"),
    "review_not_posted": Watch("Reviews post to the pull request", "The review could not be posted to its pull request.", "src/foreman/github.py"),
    "pr_size_unavailable": Watch("Pull request size is measurable before review", "The pull request size could not be measured.", "src/foreman/workspace.py"),
    "usage_unknown": Watch("{adapter} attempts report usage", "The {adapter} attempt did not report usage.", "src/foreman/adapters/{adapter}.py"),
    "no_dispatcher": Watch("A live run always has a dispatcher", "The live run had no dispatcher.", "src/foreman/cli.py"),
    "skill_missing": Watch("Pinned skills are available before dispatch", "The pinned skill export could not be loaded.", "src/foreman/skill_rules.py"),
    "git_failed": Watch("Publish handles git failures", "A git operation failed during publication.", "src/foreman/workspace.py"),
    "no_reviewer": None,
    "cli_missing": None,
    "merge_held": None,
    "merge_not_armed": None,
    "budget_exceeded": None,
    "env_unavailable": None,
    "quota_exhausted": None,
    "publish_failed": None,
    "implement_quota": None,
    "failed": None,
    "design_rethink": None,
}


def watch(state: DeliveryResult, forge: Forge, repo: str, run_key: str) -> WatchRecord | None:
    reason = state.get("halt")
    if reason is None or (row := WATCH[reason]) is None:
        return None
    if recorded := state.get("watch"):
        return recorded
    node = next((node for node in reversed(state["path"]) if node != "done"), "triage")
    adapter = ""
    if "{adapter}" in row.file:
        attempt = state.get("attempts", [])[-1]
        assert attempt["model"] is not None
        provider = family(attempt["model"])
        adapter = "codex" if provider == "gpt" else provider
    title = row.title.format(node=node, adapter=adapter)
    file = row.file.format(node=node, adapter=adapter)
    marker = f"<!-- foreman-watch: {reason}@{file} -->"
    run_marker = f"<!-- foreman-watch-run: {run_key} -->"
    assert "issue" in state and "halt_detail" in state
    attempts = ", ".join(f"{a['stage']}/{a['model']}={a['outcome']}" for a in state.get("attempts", []))
    evidence = f"Evidence: {state['repo']}#{state['issue']}; stop={reason}; detail={state['halt_detail']}; attempts={attempts}"
    matches = sorted((issue for issue in forge.open_issues(repo) if marker in (issue.body or "").splitlines()),
                     key=lambda issue: issue.number)
    for issue in matches:
        if run_marker in (issue.body or "").splitlines():
            return {"issue": issue.number, "action": "filed"}
    for issue in matches:
        if any(run_marker in comment.splitlines() for comment in forge.issue_comments(repo, issue.number)):
            return {"issue": issue.number, "action": "commented"}
    if matches:
        issue = matches[0]
        forge.comment_issue(repo, issue.number, f"{evidence}\n{run_marker}")
        return {"issue": issue.number, "action": "commented"}
    body = (
        f"Proposed graph fix: {title}\nCause: {row.cause.format(node=node, adapter=adapter)}\n{evidence}\n"
        f"File to change: {file}\nThis proposal does not change models.toml or max_review_rounds.\n"
        "It does not edit the graph, merge, or launch a coding agent.\nA person opens the pull request.\n"
        f"{run_marker}\n{marker}"
    )
    return {"issue": forge.create_issue(repo, title, body, []), "action": "filed"}
