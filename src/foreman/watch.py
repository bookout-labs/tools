"""Propose one graph fix per halt and file, with one evidence write per run."""

from textwrap import indent
from typing import NewType

from foreman.config import family
from foreman.delivery import DeliveryResult, WatchRecord
from foreman.github import Forge
from foreman.outcomes import HALTS, Halt


RunKey = NewType("RunKey", str)


def run_key(repo: str, issue: int, step: int) -> RunKey:
    return RunKey(f"{repo}#{issue}@{step}")


def watch(state: DeliveryResult, forge: Forge, repo: str, run_key: RunKey) -> WatchRecord | None:
    halt = state.get("halt")
    if halt is None or (row := HALTS[Halt(halt)].watch) is None:
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
    detail = indent(state.get("halt_detail", ""), "    ")
    attempts = ", ".join(f"{a['stage']}/{a['model']}={a['outcome']}" for a in state.get("attempts", []))
    evidence = f"Evidence: {state['repo']}#{state.get('issue', '<issue>')}; stop={halt}; attempts={attempts}\n\nDetail:\n\n{detail}\n"
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
