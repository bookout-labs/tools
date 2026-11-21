"""Run one delivery issue against its SQLite checkpoint."""

import json
from pathlib import Path
import re

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from foreman.config import load_config
from foreman.delivery import DeliveryState, LiveRun, MergeHalt, compile_delivery
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.github import Forge
from foreman.stage_output import StoredSlice, parse_output

SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)
RESUMABLE_HALTS: frozenset[MergeHalt] = frozenset({"merge_held", "merge_not_armed"})


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
    planned = item.blocker_numbers
    if planned is None:
        # Older child issues identify their siblings by title.
        by_title = {blocker.title: blocker.number for blocker in local.values()}
        planned = [by_title[title] for title in item.depends_on if title in by_title]
    if len(planned) != len(item.depends_on) or not set(planned) <= local.keys():
        return "Planned blocker links are missing."
    if unmerged := [number for number in planned if not forge.is_merged(repo, number)]:
        return "Blockers have no merged pull request: " + ", ".join(f"#{number}" for number in unmerged)
    return ""


def _replay_halted_node(graph: CompiledStateGraph, config: RunnableConfig, path: list[str]) -> None:
    if path and path[-1] == "done":
        path = path[:-1]
    if not path:
        raise ValueError("Cannot resume a run without a halted node.")
    node = path[-1]
    before = next((snapshot for snapshot in graph.get_state_history(config) if snapshot.next == (node,)), None)
    if before is None:
        raise ValueError(f"Cannot resume a run without a checkpoint before {node}.")
    graph.invoke(None, before.config, durability="sync")


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    check: str,
    checkpoint: Path,
    run: LiveRun,
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
        saved = graph.get_state(config)
        saved_workspace = saved.values.get("workspace")
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        if answer is not None:
            if not saved.interrupts:
                raise ValueError("This issue has no pending question.")
            if not answer.strip():
                raise ValueError("The answer must contain text.")
            graph.invoke(Command(resume=answer), config, durability="sync")
        elif not saved.values:
            loaded = client.load_issue(repo, issue)
            text = request if request is not None else loaded["title"] + ("\n\n" + loaded["body"] if loaded["body"] else "")
            initial: DeliveryState = {
                "repo": repo,
                "issue": issue,
                "workspace": workspace,
                "request": text,
                "check": check,
                "issue_kind": "slice" if "slice" in loaded["labels"] else "parent",
            }
            if initial["issue_kind"] == "slice":
                try:
                    item = _load_slice(loaded["body"])
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halt="missing_slice", halt_detail=str(exc))
                else:
                    if detail := _blocker_detail(client, repo, issue, item):
                        return {**initial, "path": ["done"], "stop": "blocked", "halt_detail": detail}
                    initial["slice"] = item.model_dump(exclude_unset=True)
            graph.invoke(initial, config, durability="sync")
        elif saved.values.get("halt") in RESUMABLE_HALTS:
            _replay_halted_node(graph, config, saved.values.get("path", []))
        elif saved.next and not saved.interrupts:
            graph.invoke(None, config, durability="sync")

        saved = graph.get_state(config)
        if saved.values.get("stop") == "sliced":
            numbers = list(saved.values.get("child_issue_numbers", []))
            plan = [parse_output(StoredSlice, raw) for raw in saved.values["slices"]]
            siblings = {item.title: number for item, number in zip(plan, numbers)}
            for item in plan[len(numbers):]:
                payload = item.model_dump(exclude_unset=True)
                # Plans saved by older versions can list consumers before builders.
                if all(title in siblings for title in item.depends_on):
                    payload["blocker_numbers"] = [siblings[title] for title in item.depends_on]
                dependencies = "\n".join(f"- {title}" for title in item.depends_on) or "None"
                constraints = "\n".join(f"- {prop.constraint}" for prop in item.properties) or "None"
                body = (f"Acceptance:\n{item.acceptance}\n\nDependencies:\n{dependencies}"
                        f"\n\nScreen:\n{item.screen or 'None'}\n\nConstraints:\n{constraints}"
                        f"\n\n```json\n{json.dumps(payload, indent=2)}\n```")
                labels = ["slice"] + ([] if item.depends_on else ["factory"])
                number = client.create_issue(repo, item.title, body, labels)
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
        if "merge" in result.get("path", []) and "merge" not in result:
            legacy = saver.get(saved.config)
            if legacy is not None:
                armed = legacy["channel_values"].get("merge_armed")
                if isinstance(armed, bool):
                    result["merge"] = "armed" if armed else "not_armed"
        return result
