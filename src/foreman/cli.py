"""Command line for the foreman tool."""

import argparse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import shlex
import sys
from typing import TextIO, assert_never

from foreman.accept import AcceptError, accept_event
from foreman.budget import claude_spend, token_summary, work_tokens
from foreman.credentials import CredentialFailure, credentials
from foreman.readiness import MergeState
from foreman.state import ReviewState, ChecksState, DeliveryResult
from foreman.delivery import DEFAULT_TOKEN_CAP, GRAPH_NAME, LiveRun, diagram, run_delivery, run_tokens
from foreman.diagrams import write_diagrams
from foreman.dry_run import ScriptHandler
from foreman.github import ForgeError, LiveForge
from foreman.pointer import PointerError, load_pointer
from foreman.process_group import signals_end_foreman
from foreman.redaction import redact
from foreman.outcomes import PAUSES, Halt, exit_code
from foreman.status import Status, write_status
from foreman.runner import AnswerError, IssueLocked, RunPresentation, issue_lock, issue_thread, run_live_delivery, saved_workspace
from foreman.usd import BudgetUsd
from foreman.worker import CliWorker, CommandRunner, run_worker_command
from foreman.workspace import LiveWorkspace, commit_sha, head_mismatch_detail


def _write_run_log(text: str, *, file: TextIO | None = None, end: str = "\n") -> None:
    print(redact(text), file=file, end=end, flush=True)


def _write_check_log(line: str) -> None:
    _write_run_log(line, end="")


def _print_run(result: DeliveryResult, presentation: RunPresentation | None = None, *, dry_run: bool = False) -> None:
    if presentation is None or presentation.restored is None:
        _print_run_rows(result, dry_run=dry_run, commands=presentation.commands if presentation else None)
        return
    checks = result["checks"]
    run_state = result["run"]
    current: DeliveryResult = {"review": ReviewState(), "plan": result["plan"], "run": run_state, "checks": ChecksState(check=checks.check), "path": [], "stop": result["stop"]}
    for update in presentation.updates:
        current.update(update)
    # The result repeats the restored path, except the stage a quota pause names while its call waits.
    restored = presentation.restored["path"][:-1] if presentation.restored["stop"] in PAUSES else presentation.restored["path"]
    models = result.get("models_used", {})
    rows = [(node, models.get(node)) for node in result["path"][len(restored):]]
    _print_run_rows(presentation.restored if rows else result, prefix="restored ")
    if not rows:
        _write_run_log("this invocation: no nodes executed")
        return
    current["path"] = [node for node, _ in rows]
    current["stop"] = result["stop"]
    current["attempts"] = result.get("attempts", [])
    if result["stop"] in {"needs_human", *PAUSES}:
        current["halt_detail"] = result.get("halt_detail", "")
    _write_run_log("this invocation:")
    _write_run_log("usage below is cumulative across all invocations")
    _print_run_rows(current, rows=rows)


def _print_run_rows(
    result: DeliveryResult, *, commands: list[list[str]] | None = None, dry_run: bool = False, prefix: str = "", rows: list[tuple[str, str | None]] | None = None,
) -> None:
    def write(text: str) -> None:
        _write_run_log("\n".join(prefix + line for line in text.split("\n")))

    def write_literal(text: str) -> None:
        print("\n".join(prefix + line for line in text.split("\n")))

    def write_tokens() -> None:
        attempts = result.get("attempts", [])
        summary = token_summary(attempts)
        write_literal(f"raw tokens {run_tokens(attempts)} (known subtotal)")
        write_literal(f"work tokens used / work cap: {summary.known} / {run_state.token_cap}; "
                      f"{'complete' if summary.unknown == 0 else 'partial'}; unknown attempts {summary.unknown}")
        for index, attempt in enumerate(attempts, 1):
            usage = attempt["usage"]
            buckets = " ".join(f"{key}={value if value is not None else 'unknown'}"
                               for key, value in usage.items() if key != "cost_usd") if usage else "all buckets unknown"
            work = work_tokens([attempt])
            shown = f"unknown (charged {work})" if "work_charge" in attempt else work if work is not None else "unknown"
            write_literal(f"attempt {index} {attempt['stage']} {attempt['model']} {attempt['outcome']}: {buckets}; work={shown}")
        for bucket, (total, available) in summary.buckets.items():
            write_literal(f"{bucket} tokens {total}; available {available}/{summary.charged}")

    checks = result["checks"]
    run_state = result["run"]
    if (limit := run_state.max_budget_usd) is not None:
        spent = claude_spend(result.get("attempts", []))
        amount = "unknown" if spent is None else f"${spent:f}"
        write_literal(f"claude spend {amount} of ${limit}")
    for command in commands or []:
        write(f"dry run: {shlex.join(command)}")
    models = result.get("models_used", {})
    if (size := result.get("pr_size")) and not dry_run:
        write_literal(f"pull request lines {size['lines']}")
    if rows is None:
        rows = [(node, models.get(node)) for node in result["path"]]
    def write_done() -> None:
        if checks.check_exit_code is not None:
            write_literal(f"check exit {checks.check_exit_code}")
        if run_state.token_cap is not None:
            write_tokens()
        write(f"done {result['stop']}")
        if result.get("stop") == Halt.skill_missing:
            pin_commit = commit_sha(result.get("pstack_pin", {}).get("commit", ""))
            if pin_commit:
                write_literal(f"pstack commit {pin_commit}")
        observed = commit_sha(result.get("merge", MergeState()).observed_head)
        verified = commit_sha(checks.verified_head)
        if observed and verified:
            write_literal(head_mismatch_detail(observed, verified))
        elif detail := result.get("halt_detail"):
            write(detail)

    for node, model in rows:
        if node == "check":
            write(f"check {checks.check or '(no check)'}")
        elif node == "merge":
            match outcome := result.get("merge", MergeState()).status:
                case "armed":
                    write_literal("merge armed")
                case "skipped":
                    command = result.get("merge", MergeState()).command
                    if command:
                        write_literal(f"merge skipped (dry run): {shlex.join(command)}")
                    else:
                        write_literal("merge skipped (dry run)")
                case "not_armed":
                    write_literal("merge not armed")
                case "merged":
                    write_literal(f"merged {result.get('merge', MergeState()).merge_commit_sha}")
                case "closed":
                    write_literal("pull request closed without merge")
                case None:
                    write_literal("merge outcome unavailable")
                case _:
                    assert_never(outcome)
        else:
            fields = [node]
            if model:
                fields.append(model)
            write(" ".join(fields))
    stop = result.get("stop")
    if not stop:
        return
    if stop in {"needs_human", *PAUSES}:
        if run_state.token_cap is not None:
            write_tokens()
        label = "done" if stop in {"needs_human", "merge_pending"} and dry_run else "paused"
        write_literal(f"{label} {stop}")
        if detail := result.get("halt_detail"):
            write(detail)
    else:
        write_done()


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


# Joe, 2026-11-23 on #26: "Cap 3". A repository raises it with FOREMAN_MAX_BUDGET_USD.
DEFAULT_BUDGET_USD = "3"


@dataclass(frozen=True)
class RunSettings:
    """An unattended run's settings, from the target repository's GitHub Actions variables. The check comes from its contract file."""
    preflight: str
    max_tokens: int | None
    max_budget_usd: BudgetUsd | None
    # Answers and stop requests count only from this login, so a run without one must not start.
    human_login: str

    @classmethod
    def from_variables(cls, variables: Mapping[str, str]) -> "RunSettings":
        human_login = variables.get("FOREMAN_HUMAN_LOGIN", "").strip()
        if not human_login:
            raise ValueError("Set the FOREMAN_HUMAN_LOGIN repository variable before starting runs from Ready.")
        try:
            tokens = variables.get("FOREMAN_MAX_TOKENS")
            max_tokens = positive_tokens(tokens) if tokens is not None else DEFAULT_TOKEN_CAP
            max_budget_usd = positive_budget(variables.get("FOREMAN_MAX_BUDGET_USD", DEFAULT_BUDGET_USD))
        except (argparse.ArgumentTypeError, ValueError) as exc:
            raise ValueError("FOREMAN_MAX_TOKENS must be a positive integer and FOREMAN_MAX_BUDGET_USD a positive amount.") from exc
        return cls(preflight=variables.get("FOREMAN_PREFLIGHT", ""), max_tokens=max_tokens, max_budget_usd=max_budget_usd,
                   human_login=human_login)


def main(
    argv: list[str] | None = None,
    *,
    worker_runner: CommandRunner = run_worker_command,
    checkpoint: Path | None = None,
    live_run: LiveRun | None = None,
    report: Callable[[DeliveryResult], None] = lambda result: None,
) -> int:
    parser = argparse.ArgumentParser(prog="foreman")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("smoke", help="run the bounded live scratch-repository smoke")

    commands.add_parser("coordinator", help="start an interactive Claude coordinator session")

    draw = commands.add_parser("diagram", help="print the graph")
    draw.add_argument("graph", nargs="?", default=GRAPH_NAME)
    draw.add_argument("--write", action="store_true", help="update docs/graph and README in the current checkout")

    check = commands.add_parser("check-pointer", help="check an engine pointer file")
    check.add_argument("path")

    run = commands.add_parser("run", help="walk a graph")
    run.add_argument("graph")
    run.add_argument("--repo", required=True)
    run.add_argument("--watch-repo", default="bookout-labs/tools")
    run.add_argument("--issue", type=int)
    run.add_argument("--request")
    run.add_argument("--human-login", default=os.environ.get("FOREMAN_HUMAN_LOGIN", ""),
                     help="the GitHub login whose issue comments answer or stop the run (default: FOREMAN_HUMAN_LOGIN)")
    run.add_argument("--preflight", default="")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--answer")
    run.add_argument("--max-tokens", type=positive_tokens, help="cap uncached input + cache creation + output work tokens")
    run.add_argument("--max-budget-usd", type=positive_budget)

    start = commands.add_parser("start", help="start an issue's run from Ready with its repository's settings")
    start.add_argument("--repo", required=True)
    start.add_argument("--issue", type=int, required=True)
    start.add_argument("--watch-repo", default="bookout-labs/tools")

    accept = commands.add_parser("accept", help="decide whether a GitHub delivery starts a run")
    accept.add_argument("--event", required=True, help="the delivery's X-GitHub-Event header")
    accept.add_argument("path", help="the delivery's JSON body")

    args = parser.parse_args(argv)
    if args.command == "smoke":
        from foreman.smoke_command import smoke_command
        return smoke_command()
    if args.command == "coordinator":
        try:
            instruction = files("foreman").joinpath("roles/coordinator.md").read_text(encoding="utf-8")
            if not instruction.strip():
                raise ValueError("empty role")
        except (OSError, UnicodeError, ValueError):
            print("cannot start coordinator: installed roles/coordinator.md is missing, unreadable, or empty; "
                  "reinstall the selected bookout-tools revision", file=sys.stderr)
            return 1
        try:
            os.execvp("claude", ["claude", "--append-system-prompt", instruction])
        except OSError as exc:
            detail = exc.strerror or str(exc)
            print(f"cannot start coordinator: failed to run claude: {detail}", file=sys.stderr)
            return 1
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
            decision = accept_event(args.event, json.loads(Path(args.path).read_text()))
        except (OSError, json.JSONDecodeError, AcceptError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if decision["start"] is False:
            print("ignored")
            return 0
        print(f"start {decision['repo']}#{decision['issue']}")
        return 0
    if args.command == "run" and args.graph != GRAPH_NAME:
        print(f"unknown graph {args.graph}", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.watch_repo):
        parser.error("--watch-repo must be OWNER/NAME")
    presentation = RunPresentation()
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    store = checkpoint or state_home / "foreman" / "checkpoints.sqlite3"

    def live(repo: str, issue: int, settings: RunSettings, workspace: LiveWorkspace, *,
             answer: str | None = None, request: str | None = None) -> int:
        target = live_run or LiveRun(LiveForge(), workspace, settings.human_login)
        target = replace(target, check_log=_write_check_log, human_login=settings.human_login)
        dispatcher = CliWorker(target.workspace.root, worker_runner, scratch_root=store.parent)

        def reported(result: DeliveryResult) -> None:
            _print_run(result, presentation)
            report(result)

        try:
            with signals_end_foreman():
                result = run_live_delivery(
                    repo=repo,
                    issue=issue,
                    preflight=settings.preflight,
                    checkpoint=store,
                    dispatcher=dispatcher,
                    request=request,
                    run=target,
                    answer=answer,
                    presentation=presentation,
                    max_tokens=settings.max_tokens,
                    watch_repo=args.watch_repo,
                    report=reported,
                    log=lambda message: _write_run_log(message, file=sys.stderr),
                    max_budget_usd=settings.max_budget_usd,
                )
        except (AnswerError, ForgeError, OSError, ValueError) as exc:
            _write_run_log(str(exc), file=sys.stderr)
            return 1
        # The host starts each returned issue as it starts an accepted delivery, so a replay names them again.
        for released in result.get("released", []):
            print(f"start {released}")
        return run_exit_code(result)

    if args.command == "start":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo) or args.issue < 1:
            start.error("start requires --repo OWNER/NAME and a positive --issue")
        thread = issue_thread(args.repo, args.issue)
        try:
            # Unattended runs act only as the App installed on the repository, never as a personal gh login.
            if credentials.environment(args.repo) is None:
                raise CredentialFailure("Configure FOREMAN_APP_ID before starting runs from Ready.")
            # A held lock may be a blocked admission about to delete its checkpoint, so start waits for it.
            with issue_lock(thread, wait=True):
                if saved_workspace(store, thread) is not None:
                    # A repeated delivery never resumes a paused, waiting or finished run.
                    print(f"already started {thread}")
                    return 0
                forge = live_run.forge if live_run is not None else LiveForge()
                try:
                    settings = RunSettings.from_variables(forge.variables(args.repo))
                except ValueError as exc:
                    # The issue says which setting Ready waits for, once. A delivery that outlived Ready writes nothing.
                    try:
                        write_status(forge, args.repo, args.issue, Status(None, f"Foreman cannot start this issue. {exc}"))
                    except ForgeError as error:
                        _write_run_log(f"status not written: {error}", file=sys.stderr)
                    raise
                return live(args.repo, args.issue, settings, LiveWorkspace.managed(state_home, args.repo, args.issue))
        except (CredentialFailure, ForgeError, OSError, ValueError) as exc:
            _write_run_log(str(exc), file=sys.stderr)
            return 1
    if args.dry_run:
        if args.answer is not None:
            run.error("--answer requires a live run")
        if args.request is None and args.issue is None:
            run.error("a dry run requires --request or --issue")
        result = run_delivery(
            ScriptHandler(),
            repo=args.repo,
            request=args.request or f"Issue #{args.issue}",
            max_tokens=args.max_tokens if args.max_tokens is not None else DEFAULT_TOKEN_CAP,
            max_budget_usd=args.max_budget_usd,
            commands=presentation.commands,
        )
        print("pstack pin not checked (dry run)")
        _print_run(result, presentation, dry_run=True)
        if result["stop"] == "merge_pending":
            # A dry run ends where a live run waits for GitHub to merge the queued pull request.
            return 0
        return run_exit_code(result)
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", args.repo):
        run.error("--repo must be OWNER/NAME")
    if args.issue is None or args.issue < 1:
        run.error("a live run requires a positive --issue")
    thread = issue_thread(args.repo, args.issue)
    try:
        with issue_lock(thread):
            # A resume uses its checkout from start, even before admission created it.
            saved = saved_workspace(store, thread)
            if not args.human_login.strip():
                run.error("a live run requires --human-login or FOREMAN_HUMAN_LOGIN, the login whose comments answer it")
            managed = LiveWorkspace.managed(state_home, args.repo, args.issue)
            workspace = managed if saved == str(managed.root) else LiveWorkspace(Path.cwd(), args.repo)
            return live(args.repo, args.issue, RunSettings(preflight=args.preflight, max_tokens=args.max_tokens,
                                                           max_budget_usd=args.max_budget_usd, human_login=args.human_login.strip()), workspace,
                        answer=args.answer, request=args.request)
    except IssueLocked as exc:
        _write_run_log(str(exc), file=sys.stderr)
        return 1


def run_exit_code(result: DeliveryResult) -> int:
    stop = result.get("stop")
    if stop == "needs_human":
        return 3
    if stop in PAUSES:
        return 75
    # A queued pull request is pending. Only GitHub's confirmed merge of the reviewed head completes delivery.
    if stop in ("answered", "children_filed", "merged"):
        return 0
    return exit_code(result.get("halt"))
