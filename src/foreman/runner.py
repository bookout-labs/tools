"""Run one delivery issue against its SQLite checkpoint."""

from pathlib import Path
import subprocess
from typing import Callable

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from foreman.delivery import compile_delivery
from foreman.dispatch import FailoverDispatcher


def run_live_delivery(
    *,
    repo: str,
    issue: int,
    check: str,
    cwd: Path,
    checkpoint: Path,
    dispatcher: FailoverDispatcher | None = None,
    request: str = "",
    answer: str | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
) -> dict:
    def run_check(command: str) -> int:
        return subprocess.run(command, shell=True, cwd=cwd, stdin=subprocess.DEVNULL).returncode

    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    config = {"configurable": {"thread_id": f"{repo.lower()}#{issue}"}}
    workspace = str(cwd.resolve())
    with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
        graph = compile_delivery(dispatcher, checkpointer=saver, run_check=run_check, run_gh=run_gh)
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
            graph.invoke({
                "repo": repo,
                "issue": issue,
                "workspace": workspace,
                "request": request,
                "check": check,
                "issue_kind": "slice",
            }, config, durability="sync")
        elif saved.next and not saved.interrupts:
            graph.invoke(None, config, durability="sync")

        saved = graph.get_state(config)
        if saved.interrupts:
            question = saved.interrupts[0].value["question"]
            return {**saved.values, "stop": "needs_human", "halt_detail": question}
        return dict(saved.values)
