"""Run one delivery issue against its SQLite checkpoint."""

from dataclasses import replace
import json
from pathlib import Path
import re
from typing import cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, StateSnapshot

from foreman.config import load_config
from foreman.delivery import DeliveryState, EnvironmentProbe, LiveRun, PullRequestSizeDecision, RESUMABLE_HALTS, compile_delivery, halted
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.github import Forge
from foreman.stage_output import StoredSlice, parse_output

SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)


class AnswerError(RuntimeError):
    """An answer cannot be applied to this checkpoint's question."""


def _load_slice(body: str) -> StoredSlice:
    blocks = SLICE_FENCE.findall(body)
    if not blocks:
        raise InvalidOutput("slice issue needs a fenced JSON object")
    return parse_output(StoredSlice, json.loads(blocks[-1]))


def _blocker_detail(forge: Forge, repo: str, issue: int, item: StoredSlice) -> str:
    blockers = forge.blockers(repo, issue)
    if open_urls := [blocker.html_url for blocker in blockers if blocker.state == "open"]:
        return "Open blockers: " + ", ".join(open_urls)
    local = {blocker.number: blocker for blocker in blockers if blocker.in_repo(repo)}
    planned = item.blocker_numbers or []
    if len(planned) != len(item.depends_on) or not set(planned) <= local.keys():
        return "Planned blocker links are missing."
    if unmerged := [number for number in planned if not forge.is_merged(repo, number)]:
        return "Blockers have no merged pull request: " + ", ".join(f"#{number}" for number in unmerged)
    return ""


def _checkpoint_before_halt(
    graph: CompiledStateGraph[DeliveryState, LiveRun, DeliveryState, DeliveryState],
    config: RunnableConfig,
    path: list[str],
) -> StateSnapshot:
    if path and path[-1] == "done":
        path = path[:-1]
    if not path:
        raise ValueError("Cannot resume a run without a halted node.")
    node = path[-1]
    before = next((snapshot for snapshot in graph.get_state_history(config) if snapshot.next == (node,)), None)
    if before is None:
        raise ValueError(f"Cannot resume a run without a checkpoint before {node}.")
    return before


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    check: str,
    checkpoint: Path,
    run: LiveRun,
    preflight: str = "",
    dispatcher: FailoverDispatcher | None = None,
    request: str | None = None,
    answer: str | None = None,
) -> DeliveryState:
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    config: RunnableConfig = {"configurable": {"thread_id": f"{repo.lower()}#{issue}"}}
    workspace = str(run.workspace.root.resolve())
    client = run.forge
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = compile_delivery(dispatcher, load_config(), run=run, checkpointer=saver)

        def invoke(
            graph_input: DeliveryState | Command | None,
            state: DeliveryState,
            invocation_config: RunnableConfig = config,
        ) -> None:
            environment = EnvironmentProbe(run.workspace, state.get("preflight", ""), run.check_log)
            graph.invoke(graph_input, invocation_config, context=replace(run, environment=environment), durability="sync")

        saved = graph.get_state(config)
        saved_values = cast(DeliveryState, saved.values)
        saved_workspace = saved.values.get("workspace")
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        if answer is not None:
            if not answer.strip():
                raise AnswerError("The answer must contain text.")
            if not saved.interrupts:
                answers = saved_values.get("human_answers", [])
                if not answers:
                    raise AnswerError("This issue has no pending question.")
                if answer != answers[-1]:
                    raise AnswerError("This question was already answered differently. Resume with the recorded answer.")
        if answer is not None and saved.interrupts:
            if saved.interrupts[0].value["stage"] == "pr_size":
                try:
                    answer = PullRequestSizeDecision(answer).value
                except ValueError as exc:
                    raise AnswerError("Answer split or override at the pull request size prompt.") from exc
            invoke(Command(resume=answer), saved_values)
        elif not saved.values:
            loaded = client.load_issue(repo, issue)
            text = request if request is not None else loaded["title"] + ("\n\n" + loaded["body"] if loaded["body"] else "")
            initial: DeliveryState = {
                "repo": repo,
                "issue": issue,
                "workspace": workspace,
                "request": text,
                "check": check,
                "preflight": preflight,
                "issue_kind": "slice" if "slice" in loaded["labels"] else "parent",
                "review_rounds": 0,
            }
            if initial["issue_kind"] == "slice":
                try:
                    item = _load_slice(loaded["body"])
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halted("missing_slice", str(exc), None))
                else:
                    if detail := _blocker_detail(client, repo, issue, item):
                        return {**initial, "path": ["done"], "stop": "blocked", "halt_detail": detail}
                    initial["slice"] = item.model_dump(exclude_unset=True)
            invoke(initial, initial)
        elif saved.values.get("halt") in RESUMABLE_HALTS:
            before = _checkpoint_before_halt(graph, config, saved.values.get("path", []))
            replay = before.config
            if partial := saved.values.get("partial_edits"):
                replay = graph.update_state(replay, {"partial_edits": partial})
            invoke(None, cast(DeliveryState, before.values), replay)
        elif saved.next and not saved.interrupts:
            invoke(None, saved_values)

        saved = graph.get_state(config)
        if saved.values.get("stop") == "sliced":
            numbers = list(saved.values.get("child_issue_numbers", []))
            plan = [parse_output(StoredSlice, raw) for raw in saved.values["slices"]]
            siblings = {item.title: number for item, number in zip(plan, numbers)}
            for item in plan[len(numbers):]:
                child = item.model_copy(update={"blocker_numbers": [siblings[title] for title in item.depends_on]})
                labels = ["slice"] + ([] if item.depends_on else ["factory"])
                number = client.create_issue(repo, item.title, "", labels, child=child)
                numbers.append(number)
                siblings[item.title] = number
                graph.update_state(config, {"child_issue_numbers": numbers})
            for item, number in zip(plan, numbers):
                if not item.depends_on:
                    continue
                blockers = client.blockers(repo, number)
                linked = {blocker.number for blocker in blockers if blocker.in_repo(repo)}
                planned = [siblings[title] for title in item.depends_on]
                for blocker in planned:
                    if blocker not in linked:
                        client.add_blocker(repo, number, blocker)
                if (all(blocker.state == "closed" for blocker in blockers)
                        and all(client.is_merged(repo, blocker) for blocker in planned)
                        and "factory" not in client.load_issue(repo, number)["labels"]):
                    client.add_label(repo, number, "factory")
            saved = graph.get_state(config)
        if saved.interrupts:
            question = saved.interrupts[0].value["question"]
            return {**saved.values, "stop": "needs_human", "halt_detail": question}
        result: DeliveryState = dict(saved.values)
        return result
