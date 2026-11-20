"""Command line for the foreman tool."""

import argparse
from pathlib import Path
import sys

from foreman.delivery import GRAPH_NAME, diagram, run_delivery
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler
from foreman.pointer import PointerError, load_pointer


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
    for node in result["path"]:
        if node == "verify":
            print(f"verify {result.get('check') or '(no check)'}")
        elif node == "done":
            print(f"done {result['stop']}")
        else:
            print(f"{node} {result['models_used'][node]}")
    return 0
