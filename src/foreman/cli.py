"""Command line for the foreman tool."""

import argparse
from pathlib import Path
import sys

import yaml

from foreman.accept import AcceptError, accept_event
from foreman.delivery import GRAPH_NAME, diagram, run_delivery
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.measure import MeasureError, propose
from foreman.pointer import PointerError, load_pointer


def _print_run(result: dict) -> None:
    models = result["models_used"]
    for node in result["path"]:
        if node == "verify":
            print(f"verify {result.get('check') or '(no check)'}")
        elif node == "done":
            print(f"done {result['stop']}")
            if detail := result.get("halt_detail"):
                print(detail)
        elif node == "merge":
            print("merge armed" if result["merge_armed"] else "merge unarmed")
        else:
            fields = [node]
            if model := models.get(node):
                fields.append(model)
            if node == "review" and (risk := result.get("risk")):
                fields.extend(("risk", risk["level"]))
            print(" ".join(fields))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="foreman")
    commands = parser.add_subparsers(dest="command", required=True)

    draw = commands.add_parser("diagram", help="print the graph")
    draw.add_argument("graph")

    check = commands.add_parser("check-pointer", help="check an engine pointer file")
    check.add_argument("path")

    run = commands.add_parser("run", help="walk a graph")
    run.add_argument("graph")
    run.add_argument("--repo", required=True)
    run.add_argument("--request", required=True)
    run.add_argument("--check", default="")
    run.add_argument("--dry-run", action="store_true")

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
    if not args.dry_run:
        print("A live dispatcher is not configured. Pass --dry-run to walk the graph.", file=sys.stderr)
        return 2
    result = run_delivery(
        FailoverDispatcher(ScriptHandler()),
        repo=args.repo,
        request=args.request,
        check=args.check,
    )
    _print_run(result)
    return 0
