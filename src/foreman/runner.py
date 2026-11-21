"""Run one delivery issue against its SQLite checkpoint."""

import json
from pathlib import Path
import re
import subprocess
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from foreman.delivery import DeliveryContext, DeliveryState, clean_slices, compile_delivery, failed_preflight
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.github import GhClient, GitHub
from foreman.stage_output import Slice, parse_output

WorktreeInspector = Callable[[Path], tuple[str, bool]]
SLICE_FENCE = re.compile(r"^```json[ \t]*\r?\n((?:(?!^```)[^\n]*\n)*)^```[ \t]*\r?$", re.MULTILINE)


def _load_slice(body: str) -> dict[str, object]:
    blocks = SLICE_FENCE.findall(body)
    if not blocks:
        raise InvalidOutput("slice issue needs a fenced JSON object")
    return clean_slices([parse_output(Slice, json.loads(blocks[-1]))], "slice", allow_external_dependencies=True)[0]


def _inspect_worktree(cwd: Path) -> tuple[str, bool]:
    head = subprocess.run(["git", "rev-parse", "HEAD"], shell=False, cwd=cwd, capture_output=True, text=True)
    status = subprocess.run(["git", "status", "--porcelain"], shell=False, cwd=cwd, capture_output=True, text=True)
    return head.stdout.strip(), head.returncode == status.returncode == 0 and not status.stdout


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    check: str,
    cwd: Path,
    checkpoint: Path,
    preflight: str = "",
    dispatcher: FailoverDispatcher | None = None,
    request: str | None = None,
    github: GitHub | None = None,
    answer: str | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
    inspect_worktree: WorktreeInspector | None = None,
) -> dict:
    def run_command(command: str) -> int:
        return subprocess.run(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL).returncode

    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    config = {"configurable": {"thread_id": f"{repo.lower()}#{issue}"}}
    workspace = str(cwd.resolve())
    inspect = inspect_worktree or _inspect_worktree
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = compile_delivery(
            dispatcher, checkpointer=saver, run_check=run_command, run_gh=run_gh,
            inspect_worktree=lambda: inspect(cwd),
        )

        def invoke(graph_input: DeliveryState | Command | None, state: DeliveryState) -> None:
            detail = None
            if state.get("issue_kind") != "parent" and not state.get("halt"):
                detail = failed_preflight(state.get("preflight", ""), run_command)
            graph.invoke(graph_input, config, context=DeliveryContext(environment_error=detail), durability="sync")

        saved = graph.get_state(config)
        saved_workspace = saved.values.get("workspace")
        if saved_workspace and saved_workspace != workspace:
            raise ValueError(f"Run this issue from {saved_workspace}.")
        if answer is not None:
            if not saved.interrupts:
                raise ValueError("This issue has no pending question.")
            if not answer.strip():
                raise ValueError("The answer must contain text.")
            invoke(Command(resume=answer), saved.values)
        elif not saved.values:
            loaded = (github or GhClient()).load_issue(repo, issue)
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
                    initial["slice"] = _load_slice(loaded["body"])
                except (InvalidOutput, json.JSONDecodeError) as exc:
                    initial.update(halt="missing_slice", halt_detail=str(exc))
            invoke(initial, initial)
        elif saved.next and not saved.interrupts:
            invoke(None, saved.values)

        saved = graph.get_state(config)
        if saved.values.get("stop") == "sliced":
            client = github or GhClient()
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
            question = saved.interrupts[0].value["question"]
            return {**saved.values, "stop": "needs_human", "halt_detail": question}
        return dict(saved.values)
