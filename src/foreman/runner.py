# Resolve the delivery types in #52 before restoring strict checking.
# pyright: basic
"""Run one delivery issue against its SQLite checkpoint."""

import json
from pathlib import Path
import re

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from foreman.config import load_config
from foreman.delivery import DeliveryState, LiveRun, MergeHalt, compile_delivery, load_slice, read_delivery_state
from foreman.dispatch import FailoverDispatcher, InvalidOutput

SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)
RESUMABLE_HALTS: frozenset[MergeHalt] = frozenset({"merge_held", "merge_not_armed"})


def _load_slice(body: str) -> dict[str, object]:
    blocks = SLICE_FENCE.findall(body)
    if not blocks:
        raise InvalidOutput("slice issue needs a fenced JSON object")
    return load_slice(json.loads(blocks[-1]))


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
                    initial["slice"] = _load_slice(loaded["body"])
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halt="missing_slice", halt_detail=str(exc))
            graph.invoke(initial, config, durability="sync")
        elif saved.values.get("halt") in RESUMABLE_HALTS:
            _replay_halted_node(graph, config, saved.values.get("path", []))
        elif saved.next and not saved.interrupts:
            graph.invoke(None, config, durability="sync")

        saved = graph.get_state(config)
        if saved.values.get("stop") == "sliced":
            numbers = list(saved.values.get("child_issue_numbers", []))
            for item in saved.values["slices"][len(numbers):]:
                dependencies = "\n".join(f"- {title}" for title in item["depends_on"]) or "None"
                constraints = "\n".join(f"- {prop['constraint']}" for prop in item["properties"]) or "None"
                body = (f"Acceptance:\n{item['acceptance']}\n\nDependencies:\n{dependencies}"
                        f"\n\nScreen:\n{item['screen'] or 'None'}\n\nConstraints:\n{constraints}"
                        f"\n\n```json\n{json.dumps(item, indent=2)}\n```")
                labels = ["slice"] + ([] if item["depends_on"] else ["factory"])
                numbers.append(client.create_issue(repo, item["title"], body, labels))
                graph.update_state(config, {"child_issue_numbers": numbers})
            siblings = {item["title"]: number for item, number in zip(saved.values["slices"], numbers)}
            for item, number in zip(saved.values["slices"], numbers):
                deps = item["depends_on"]
                if (deps and all(title in siblings and client.is_merged(repo, siblings[title]) for title in deps)
                        and "factory" not in client.load_issue(repo, number)["labels"]):
                    client.add_label(repo, number, "factory")
            saved = graph.get_state(config)
        if saved.interrupts:
            question: object = saved.interrupts[0].value["question"]
            return read_delivery_state({**saved.values, "stop": "needs_human", "halt_detail": question})
        result = read_delivery_state(saved.values)
        if "merge" in result.get("path", []) and "merge" not in result:
            legacy = saver.get(saved.config)
            if legacy is not None:
                armed = legacy["channel_values"].get("merge_armed")
                if isinstance(armed, bool):
                    result["merge"] = "armed" if armed else "not_armed"
        return result
