"""Run one delivery issue against its SQLite checkpoint."""

from dataclasses import replace
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from foreman.config import PstackPin, load_config
from foreman.delivery import DeliveryState, LiveRun, MergeHalt, compile_delivery, load_slice
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.skill_rules import SkillMissing
from foreman.stage_output import StoredSlice, parse_output

SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)
RESUMABLE_HALTS: frozenset[MergeHalt] = frozenset({"merge_held", "merge_not_armed"})


def _load_slice(body: str) -> DeliveryState:
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
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver, TemporaryDirectory(prefix="foreman-skills-") as scratch:
        delivery_config = load_config()
        prior = saver.get(config)
        channels = prior["channel_values"] if prior else {}
        saved_pin = channels.get("pstack_pin")
        if prior and not channels.get("stop") and saved_pin is None:
            raise ValueError("Cannot resume a delivery checkpoint without a pstack pin.")
        pin = PstackPin.model_validate(saved_pin) if saved_pin is not None else delivery_config.pstack
        try:
            graph = compile_delivery(
                dispatcher, replace(delivery_config, pstack=pin), run=run, checkpointer=saver,
                scratch=None if channels.get("stop") else Path(scratch),
            )
        except SkillMissing as exc:
            return {"path": ["done"], "halt": exc.halt, "stop": exc.halt,
                    "halt_detail": str(exc), "skill_reason": exc.reason, "pstack_pin": pin.model_dump()}
        saved = graph.get_state(config)
        saved_workspace = saved.values.get("workspace")
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        waiting_for_quota = bool(saved.interrupts) and saved.values.get("halt") == "quota_exhausted"
        if answer is not None:
            if not saved.interrupts or waiting_for_quota:
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
                "pstack_pin": pin.model_dump(),
                "issue_kind": "slice" if "slice" in loaded["labels"] else "parent",
            }
            if initial["issue_kind"] == "slice":
                try:
                    initial.update(_load_slice(loaded["body"]))
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halt="missing_slice", halt_detail=str(exc))
            graph.invoke(initial, config, durability="sync")
        elif waiting_for_quota:
            graph.invoke(Command(resume=True), config, durability="sync")
        elif saved.values.get("halt") in RESUMABLE_HALTS:
            _replay_halted_node(graph, config, saved.values.get("path", []))
        elif saved.next and not saved.interrupts:
            graph.invoke(None, config, durability="sync")

        saved = graph.get_state(config)
        if saved.values.get("stop") == "sliced":
            numbers = list(saved.values.get("child_issue_numbers", []))
            work_kind = saved.values.get("work_kind")
            for item in saved.values["slices"][len(numbers):]:
                child = parse_output(StoredSlice, {**item, "work_kind": work_kind})
                labels = ["slice"] + ([] if child.depends_on else ["factory"])
                numbers.append(client.create_issue(repo, child.title, "", labels, child=child))
                graph.update_state(config, {"child_issue_numbers": numbers})
            siblings = {item["title"]: number for item, number in zip(saved.values["slices"], numbers)}
            for item, number in zip(saved.values["slices"], numbers):
                deps = item["depends_on"]
                if (deps and all(title in siblings and client.is_merged(repo, siblings[title]) for title in deps)
                        and "factory" not in client.load_issue(repo, number)["labels"]):
                    client.add_label(repo, number, "factory")
            saved = graph.get_state(config)
        result = cast(DeliveryState, dict(saved.values))
        if saved.interrupts:
            if result.get("halt") == "quota_exhausted":
                result["stop"] = "quota_exhausted"
            else:
                result["stop"] = "needs_human"
                result["halt_detail"] = saved.interrupts[0].value["question"]
            return result
        if "merge" in result.get("path", []) and "merge" not in result:
            legacy = saver.get(saved.config)
            if legacy is not None:
                armed = legacy["channel_values"].get("merge_armed")
                if isinstance(armed, bool):
                    result["merge"] = "armed" if armed else "not_armed"
        return result
