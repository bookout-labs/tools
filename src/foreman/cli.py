"""Command line for the foreman tool."""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
from typing import Callable

import yaml

from foreman.accept import AcceptError, accept_event
from foreman.delivery import GRAPH_NAME, diagram, run_delivery
from foreman.dispatch import FailoverDispatcher, QuotaExhausted
from foreman.dry_run import ScriptHandler
from foreman.github import GhClient, GitHub
from foreman.measure import MeasureError, propose
from foreman.pointer import PointerError, load_pointer
from foreman.runner import WorktreeInspector, run_live_delivery
from foreman.watcher import TOOLS_REPO, file_proposal_issue
from foreman.worker import CliWorker, CommandRunner, run_worker_command

MERGE_HOLD_VARIABLE = "FOREMAN_MERGE_HOLD"


def _print_run(result: dict) -> None:
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
            if proposal := result.get("proposal"):
                print(proposal["summary"].rstrip())
        elif node == "merge":
            if url := result.get("pull_request"):
                print(url)
            if result.get("halt") == "merge_held":
                print("merge held")
            elif result.get("halt") in {"unpublished", "review_unrecorded"}:
                print("merge not armed")
            elif result["merge_armed"]:
                print("merge armed")
            elif result.get("merge_args"):
                print("merge arm recorded")
            else:
                print("merge unarmed")
            if args := result.get("merge_args"):
                print(shlex.join(args))
        else:
            fields = [node]
            if model := models.get(node):
                fields.append(model)
            if node == "review" and (risk := result.get("risk")):
                fields.extend(("risk", risk["level"]))
            print(" ".join(fields))
    if result.get("stop") == "needs_human" and "done" not in result["path"]:
        print("paused needs_human")
        print(result["halt_detail"])


def _read_merge_hold(repo: str) -> str | None:
    try:
        result = subprocess.run(
            ["gh", "variable", "get", MERGE_HOLD_VARIABLE, "--repo", repo],
            shell=False, stdin=subprocess.DEVNULL, capture_output=True, text=True,
        )
    except OSError as exc:
        return f"could not read {MERGE_HOLD_VARIABLE}: {exc}"
    if result.returncode == 0:
        return result.stdout.strip() or None
    detail = " ".join(f"{result.stderr}\n{result.stdout}".split())
    lower = detail.lower()
    if "was not found" in lower or "404" in lower:
        return None
    return f"could not read {MERGE_HOLD_VARIABLE}: {detail or f'gh exited {result.returncode}'}"


def _run_gh(args: list[str]) -> int:
    return subprocess.run(args, shell=False, stdin=subprocess.DEVNULL).returncode


def _find_pr(repo: str) -> dict[str, object] | None:
    # An empty selector keeps current-branch lookup while satisfying gh's --repo argument check.
    try:
        result = subprocess.run(
            ["gh", "pr", "view", "--repo", repo, "--json", "number,state,headRefOid,headRefName,baseRefName", ""],
            shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
        if result.returncode != 0:
            return None
        pr = json.loads(result.stdout)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return pr if isinstance(pr, dict) else None


def main(
    argv: list[str] | None = None,
    *,
    worker_runner: CommandRunner | None = run_worker_command,
    checkpoint: Path | None = None,
    github: GitHub | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
    find_pr: Callable[[str], dict[str, object] | None] | None = None,
    inspect_worktree: WorktreeInspector | None = None,
    read_merge_hold: Callable[[str], str | None] | None = None,
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
    else:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
            run.error("--repo must be OWNER/NAME")
        if args.issue is None or args.issue < 1 or not args.check.strip():
            run.error("a live run requires a positive --issue and a --check command")
        if checkpoint is None:
            state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
            checkpoint = state_home / "foreman" / "checkpoints.sqlite3"
        cwd = Path.cwd()
        dispatcher = FailoverDispatcher(CliWorker(cwd, worker_runner)) if worker_runner else None
        lookup = _find_pr if find_pr is None else find_pr
        merge = _run_gh if run_gh is None else run_gh

        def merge_if_matching_pr(command: list[str]) -> int:
            if command[:-1] != ["gh", "pr", "merge", "--auto", "--match-head-commit"]:
                return 1
            sha = command[-1]
            if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
                return 1
            pr = lookup(args.repo)
            if pr is None or pr.get("state") != "OPEN":
                return 1
            head = pr.get("headRefOid")
            if not isinstance(head, str) or head.lower() != sha.lower():
                return 1
            branch = pr.get("headRefName")
            if not isinstance(branch, str) or not branch.startswith(f"{args.issue}-") or pr.get("baseRefName") != "main":
                return 1
            return merge(command)

        hold_reader = _read_merge_hold if read_merge_hold is None else read_merge_hold
        try:
            result = run_live_delivery(
                repo=args.repo,
                issue=args.issue,
                check=args.check,
                cwd=cwd,
                checkpoint=checkpoint,
                dispatcher=dispatcher,
                request=args.request,
                github=github,
                run_gh=merge_if_matching_pr,
                answer=args.answer,
                inspect_worktree=inspect_worktree,
                read_merge_hold=hold_reader,
            )
        except (OSError, QuotaExhausted, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        _print_run(result)
        if (proposal := result.get("proposal")) and proposal.get("file_issue"):
            try:
                issue_number = file_proposal_issue(proposal, github or GhClient())
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"watcher: could not file issue: {exc}", file=sys.stderr)
            else:
                if issue_number is not None:
                    print(f"watcher issue {TOOLS_REPO}#{issue_number}")
        return 0
    _print_run(result)
    return 0
