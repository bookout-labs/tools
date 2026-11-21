"""Command line for the foreman tool."""

import argparse
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import shlex
import sys
from typing import TextIO, assert_never

import yaml

from foreman.accept import AcceptError, accept_event
from foreman.budget import claude_spend
from foreman.delivery import DEFAULT_TOKEN_CAP, DeliveryResult, GRAPH_NAME, LiveRun, diagram, run_delivery, run_tokens
from foreman.diagrams import write_diagrams
from foreman.dry_run import ScriptHandler
from foreman.fixture_manifest import MANIFEST_PATH, fact_record_path
from foreman.github import ForgeError, LiveForge
from foreman.pointer import PointerError, load_pointer
from foreman.redaction import redact
from foreman.outcomes import Halt, exit_code
from foreman.runner import AnswerError, RunPresentation, run_live_delivery
from foreman.usd import BudgetUsd
from foreman.worker import CliWorker, CommandRunner, run_worker_command
from foreman.workspace import LiveWorkspace, commit_sha, head_mismatch_detail


def _write_run_log(text: str, *, file: TextIO | None = None, end: str = "\n") -> None:
    print(redact(text), file=file, end=end, flush=True)


def _write_check_log(line: str) -> None:
    _write_run_log(line, end="")


def _print_run(result: DeliveryResult, presentation: RunPresentation | None = None) -> None:
    if presentation is None or presentation.restored is None:
        _print_run_rows(result)
        return
    rows: list[tuple[str, str | None]] = []
    current: DeliveryResult = {"repo": result["repo"], "check": result["check"], "path": [], "stop": result["stop"]}
    for update in presentation.updates:
        current.update(update)
        rows.extend((node, update.get("models_used", {}).get(node)) for node in update.get("path", []))
    _print_run_rows(presentation.restored if rows else result, prefix="restored ")
    if not rows:
        _write_run_log("this invocation: no nodes executed")
        return
    current["path"] = [node for node, _ in rows]
    current["stop"] = result["stop"]
    current["max_budget_usd"] = result.get("max_budget_usd")
    current["attempts"] = result.get("attempts", [])
    if result["stop"] in {"needs_human", Halt.quota_exhausted}:
        current["halt_detail"] = result.get("halt_detail", "")
    _write_run_log("this invocation:")
    _print_run_rows(current, rows=rows)


def _print_run_rows(
    result: DeliveryResult, *, prefix: str = "", rows: list[tuple[str, str | None]] | None = None,
) -> None:
    def write(text: str) -> None:
        _write_run_log("\n".join(prefix + line for line in text.split("\n")))

    def write_literal(text: str) -> None:
        print("\n".join(prefix + line for line in text.split("\n")))

    if (limit := result.get("max_budget_usd")) is not None:
        spent = claude_spend(result.get("attempts", []))
        amount = "unknown" if spent is None else f"${spent:f}"
        write_literal(f"claude spend {amount} of ${limit}")
    for command in result.get("commands", []):
        write(f"dry run: {shlex.join(command)}")
    models = result.get("models_used", {})
    if size := result.get("pr_size"):
        write_literal(f"pull request lines {size['lines']}")
    if rows is None:
        rows = [(node, models.get(node)) for node in result["path"]]
    for node, model in rows:
        if node == "verify":
            write(f"verify {result.get('check') or '(no check)'}")
        elif node == "done":
            if "check_exit_code" in result:
                write_literal(f"check exit {result['check_exit_code']}")
            for proof, label in result.get("proof_labels", {}).items():
                write(f"{proof} proof {label}")
            if "token_cap" in result:
                write_literal(f"tokens {run_tokens(result.get('attempts', []))} of {result['token_cap']}")
            write(f"done {result['stop']}")
            if result.get("stop") == Halt.skill_missing:
                pin_commit = commit_sha(result.get("pstack_pin", {}).get("commit", ""))
                if pin_commit:
                    write_literal(f"pstack commit {pin_commit}")
            observed = commit_sha(result.get("merge_observed_head", ""))
            verified = commit_sha(result.get("verified_head", ""))
            if observed and verified:
                write_literal(head_mismatch_detail(observed, verified))
            elif detail := result.get("halt_detail"):
                write(detail)
        elif node == "merge":
            match outcome := result.get("merge"):
                case "armed":
                    write_literal("merge armed")
                case "skipped":
                    command = result.get("merge_command")
                    if command:
                        write_literal(f"merge skipped (dry run): {shlex.join(command)}")
                    else:
                        write_literal("merge skipped (dry run)")
                case "not_armed":
                    write_literal("merge not armed")
                case None:
                    write_literal("merge outcome unavailable")
                case _:
                    assert_never(outcome)
        else:
            fields = [node]
            if model:
                fields.append(model)
            if node == "review" and (risk := result.get("risk")):
                fields.extend(("risk", risk["level"]))
            write(" ".join(fields))
    stop = result.get("stop")
    if stop in {"needs_human", Halt.quota_exhausted} and "done" not in result["path"]:
        write_literal(f"paused {stop}")
        if detail := result.get("halt_detail"):
            write(detail)


def positive_tokens(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError("--max-tokens must be positive")
    return value


def positive_budget(raw: str) -> BudgetUsd:
    try:
        return BudgetUsd(Decimal(raw))
    except (InvalidOperation, ValueError) as exc:
        raise argparse.ArgumentTypeError("--max-budget-usd must be finite and positive") from exc


def main(
    argv: list[str] | None = None,
    *,
    worker_runner: CommandRunner = run_worker_command,
    checkpoint: Path | None = None,
    live_run: LiveRun | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="foreman")
    commands = parser.add_subparsers(dest="command", required=True)

    draw = commands.add_parser("diagram", help="print the graph")
    draw.add_argument("graph", nargs="?", default=GRAPH_NAME)
    draw.add_argument("--write", action="store_true", help="update docs/graph and README in the current checkout")

    check = commands.add_parser("check-pointer", help="check an engine pointer file")
    check.add_argument("path")

    run = commands.add_parser("run", help="walk a graph")
    run.add_argument("graph")
    run.add_argument("--repo", required=True)
    run.add_argument("--issue", type=int)
    run.add_argument("--request")
    run.add_argument("--check", default="")
    run.add_argument("--preflight", default="")
    run.add_argument("--fact-record", type=fact_record_path, default=None)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--answer")
    run.add_argument("--max-tokens", type=positive_tokens)
    run.add_argument("--max-budget-usd", type=positive_budget)

    accept = commands.add_parser("accept", help="decide whether an event enters the graph")
    accept.add_argument("path")

    args = parser.parse_args(argv)
    if args.command == "diagram":
        if args.graph != GRAPH_NAME:
            print(f"unknown graph {args.graph}", file=sys.stderr)
            return 2
        if args.write:
            try:
                write_diagrams(Path.cwd())
            except (OSError, ValueError) as exc:
                print(f"cannot write diagrams: {exc}", file=sys.stderr)
                return 2
        else:
            print(diagram(), end="")
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
        if decision["start"] is False:
            print("ignored")
            return 0
        print(f"start {decision['source']}")
        print(decision["request"])
        return 0
    if args.graph != GRAPH_NAME:
        print(f"unknown graph {args.graph}", file=sys.stderr)
        return 2
    presentation = RunPresentation()
    if args.dry_run:
        if args.answer is not None:
            run.error("--answer requires a live run")
        if args.request is None and args.issue is None:
            run.error("a dry run requires --request or --issue")
        result = run_delivery(
            ScriptHandler(),
            repo=args.repo,
            request=args.request or f"Issue #{args.issue}",
            check=args.check,
            max_tokens=args.max_tokens if args.max_tokens is not None else DEFAULT_TOKEN_CAP,
            max_budget_usd=args.max_budget_usd,
            fact_record=args.fact_record or MANIFEST_PATH,
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
            live_run = LiveRun(LiveForge(), LiveWorkspace(Path.cwd(), args.repo))
        live_run = replace(live_run, check_log=_write_check_log)
        dispatcher = CliWorker(live_run.workspace.root, worker_runner)
        try:
            result = run_live_delivery(
                repo=args.repo,
                issue=args.issue,
                check=args.check,
                preflight=args.preflight,
                fact_record=args.fact_record,
                checkpoint=checkpoint,
                dispatcher=dispatcher,
                request=args.request,
                run=live_run,
                answer=args.answer,
                presentation=presentation,
                max_tokens=args.max_tokens,
                max_budget_usd=args.max_budget_usd,
            )
        except (AnswerError, ForgeError, OSError, ValueError) as exc:
            _write_run_log(str(exc), file=sys.stderr)
            return 1
    _print_run(result, presentation)
    return run_exit_code(result)


def run_exit_code(result: DeliveryResult) -> int:
    stop = result.get("stop")
    if stop == "needs_human":
        return 3
    if stop in ("verified", "sliced", "answered") and result.get("path", [])[-1:] == ["done"]:
        return 0
    return exit_code(result.get("halt"))
