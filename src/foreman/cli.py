"""Command line for the foreman tool."""

import argparse
import os
from pathlib import Path
import re
import shlex
import sys
from typing import assert_never

import yaml

from foreman.accept import AcceptError, accept_event
from foreman.delivery import DeliveryState, GRAPH_NAME, LiveRun, diagram, run_delivery
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
from foreman.dry_run import ScriptHandler
from foreman.github import ForgeError, LiveForge
from foreman.measure import MeasureError, propose
from foreman.pointer import PointerError, load_pointer
from foreman.runner import run_live_delivery
from foreman.worker import CliWorker, CommandRunner, run_worker_command
from foreman.workspace import LiveWorkspace


def _print_run(result: DeliveryState) -> None:
    models = result.get("models_used", {})
    for node in result["path"]:
        if node == "verify":
            print(f"verify {result.get('check') or '(no check)'}")
        elif node == "done":
            if "check_exit_code" in result:
                print(f"check exit {result['check_exit_code']}")
            for proof, label in result.get("proof_labels", {}).items():
                print(f"{proof} proof {label}")
            print(f"done {result['stop']}")
            if detail := result.get("halt_detail"):
                print(detail)
        elif node == "merge":
            match outcome := result["merge"]:
                case "armed":
                    print("merge armed")
                case "skipped":
                    print(f"merge skipped (dry run): {shlex.join(result['merge_command'])}")
                case "not_armed":
                    print("merge not armed")
                case _:
                    assert_never(outcome)
        else:
            fields = [node]
            if model := models.get(node):
                fields.append(model)
            if node == "review" and (risk := result.get("risk")):
                fields.extend(("risk", risk["level"]))
            print(" ".join(fields))
    stop = result.get("stop")
    if stop in {"needs_human", "quota_exhausted"} and "done" not in result["path"]:
        print(f"paused {stop}")
        print(result["halt_detail"])


def main(
    argv: list[str] | None = None,
    *,
    worker_runner: CommandRunner | None = run_worker_command,
    checkpoint: Path | None = None,
    live_run: LiveRun | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="foreman")
    commands = parser.add_subparsers(dest="command", required=True)

    draw = commands.add_parser("diagram", help="print the graph")
    draw.add_argument("graph")

    check = commands.add_parser("check-pointer", help="check an engine pointer file")
    check.add_argument("path")

    run = commands.add_parser("run", help="walk a graph")
    run.add_argument("graph")
    run.add_argument("--repo", required=True)
    run.add_argument("--issue", type=int)
    run.add_argument("--request")
    run.add_argument("--check", default="")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--answer")

    accept = commands.add_parser("accept", help="decide whether an event enters the graph")
    accept.add_argument("path")

    improve = commands.add_parser("improve", help="propose a change from a failing score")
    improve.add_argument("failure")

    args = parser.parse_args(argv)
    if args.command == "diagram":
        if args.graph != GRAPH_NAME:
            print(f"unknown graph {args.graph}", file=sys.stderr)
            return 2
        print(diagram())
        return 0
    if args.command == "check-pointer":
        try:
            pointer = load_pointer(Path(args.path))
        except (OSError, PointerError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        for key in ("source", "ref", "graph"):
            print(f"{key} {pointer[key]}")
        if "check" in pointer:
            print(f"check {pointer['check']}")
        return 0
    if args.command == "accept":
        try:
            event = yaml.safe_load(Path(args.path).read_text())
            decision = accept_event(event)
        except (OSError, yaml.YAMLError, AcceptError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if not decision["start"]:
            print("ignored")
            return 0
        print(f"start {decision['source']}")
        print(decision["request"])
        return 0
    if args.command == "improve":
        try:
            print(propose(args.failure), end="")
        except MeasureError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        return 0
    if args.graph != GRAPH_NAME:
        print(f"unknown graph {args.graph}", file=sys.stderr)
        return 2
    if args.dry_run:
        if args.answer is not None:
            run.error("--answer requires a live run")
        if args.request is None and args.issue is None:
            run.error("a dry run requires --request or --issue")
        result = run_delivery(
            FailoverDispatcher(ScriptHandler()),
            repo=args.repo,
            request=args.request or f"Issue #{args.issue}",
            check=args.check,
        )
        print("pstack pin not checked (dry run)")
    else:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
            run.error("--repo must be OWNER/NAME")
        if args.issue is None or args.issue < 1 or not args.check.strip():
            run.error("a live run requires a positive --issue and a --check command")
        if checkpoint is None:
            state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
            checkpoint = state_home / "foreman" / "checkpoints.sqlite3"
        if live_run is None:
            live_run = LiveRun(LiveForge(), LiveWorkspace(Path.cwd()))
        dispatcher = FailoverDispatcher(CliWorker(live_run.workspace.root, worker_runner)) if worker_runner else None
        try:
            result = run_live_delivery(
                repo=args.repo,
                issue=args.issue,
                check=args.check,
                checkpoint=checkpoint,
                dispatcher=dispatcher,
                request=args.request,
                run=live_run,
                answer=args.answer,
            )
        except (ForgeError, OSError, QuotaExhausted, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    _print_run(result)
    return 1 if result.get("stop") in {"skill_missing", "quota_exhausted"} else 0
