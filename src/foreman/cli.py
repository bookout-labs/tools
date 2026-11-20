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
from foreman.scoring import JudgeRunner, ScorerError, calibration_report, load_scorers, score_run
from foreman.telemetry import TelemetryStore, default_telemetry_path, load_pricing, render_stats_html, render_stats_text
from foreman.watcher import TOOLS_REPO, file_proposal_issue
from foreman.worker import CliWorker, CommandRunner, run_worker_command

MERGE_HOLD_VARIABLE = "FOREMAN_MERGE_HOLD"


def _print_run(result: dict) -> None:
    models = result.get("models_used", {})
    exits = result.get("check_exits") if isinstance(result.get("check_exits"), list) else []
    reviews = result.get("review_log") if isinstance(result.get("review_log"), list) else []
    verify_index = 0
    review_index = 0
    for node in result["path"]:
        if node == "verify":
            label = result.get("check") or "(no check)"
            if verify_index < len(exits):
                print(f"verify {label} exit {exits[verify_index]}")
            else:
                print(f"verify {label}")
            verify_index += 1
        elif node == "done":
            if "check_exit_code" in result:
                print(f"check exit {result['check_exit_code']}")
            for proof, label in result.get("proof_labels", {}).items():
                print(f"{proof} proof {label}")
            print(f"done {result['stop']}")
            if detail := result.get("halt_detail"):
                print(detail)
            elif result.get("stop") == "review_cap" and not reviews and (note := result.get("review_note")):
                print(f"review note: {note}")
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
            entry = None
            if node == "review" and review_index < len(reviews) and isinstance(reviews[review_index], dict):
                entry = reviews[review_index]
                review_index += 1
            model = entry.get("model") if entry and entry.get("model") else models.get(node)
            if model:
                fields.append(model)
            if node == "review":
                verdict = entry.get("verdict") if entry else result.get("review")
                risk = entry.get("level") if entry else (result.get("risk") or {}).get("level")
                if verdict:
                    fields.append(verdict)
                if risk:
                    fields.extend(("risk", risk))
            print(" ".join(fields))
            if entry and (note := entry.get("note")):
                print(f"review note: {note}")
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
    judge_runner: JudgeRunner | None = None,
    checkpoint: Path | None = None,
    telemetry_path: Path | None = None,
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
    run.add_argument("--preflight", help="optional shell check run before implementation")
    run.add_argument("--telemetry", help="SQLite telemetry database path")
    run.add_argument("--budget-usd", type=float, help="hard per-run USD ceiling")
    run.add_argument("--budget-tokens", type=int, help="hard per-run token ceiling")
    run.add_argument("--score", action="store_true", help="score the completed run with configured LLM scorers")

    accept = commands.add_parser("accept", help="decide whether an event enters the graph")
    accept.add_argument("path")

    improve = commands.add_parser("improve", help="propose a change from a failing score")
    improve.add_argument("failure")

    scorers_cmd = commands.add_parser("scorers", help="list configured LLM scorer rubrics")
    scorers_cmd.add_argument("--json", action="store_true")

    score_cmd = commands.add_parser("score", help="run LLM scorers against recorded telemetry runs")
    score_cmd.add_argument("run_id_pos", nargs="?")
    score_cmd.add_argument("--run", dest="run_id")
    score_cmd.add_argument("--all", action="store_true", help="score recent unscored runs")
    score_cmd.add_argument("--db", help="SQLite telemetry database path")
    score_cmd.add_argument("--scorer", action="append", dest="scorer_names", help="specific scorer name(s)")
    score_cmd.add_argument("--force", action="store_true", help="re-score even if scores already exist")
    score_cmd.add_argument("--all-samples", action="store_true", help="ignore samplingRate and score every selected run")
    score_cmd.add_argument("--calibration", action="store_true", help="print scorer calibration vs GitHub outcomes")

    stats_cmd = commands.add_parser("stats", help="summarize run telemetry, spend, and autonomy")
    stats_cmd.add_argument("--db", help="SQLite telemetry database path")
    stats_cmd.add_argument("--repo")
    stats_cmd.add_argument("--host")
    stats_cmd.add_argument("--days", type=int)
    stats_cmd.add_argument("--refresh", action="store_true", help="refresh GitHub PR outcome fields via gh")
    stats_cmd.add_argument("--format", choices=("text", "json", "html"), default="text")
    stats_cmd.add_argument("--out", help="optional file path to write formatted stats")

    tel_cmd = commands.add_parser("telemetry", help="export or import multi-host telemetry JSONL")
    tel_sub = tel_cmd.add_subparsers(dest="telemetry_action", required=True)
    tel_export = tel_sub.add_parser("export", help="export runs, stage calls, and scores as JSONL")
    tel_export.add_argument("--db", help="SQLite telemetry database path")
    tel_export.add_argument("--out", help="output JSONL file path (defaults to stdout)")
    tel_export.add_argument("--since")
    tel_export.add_argument("--host")

    tel_import = tel_sub.add_parser("import", help="import one or more JSONL telemetry snapshots")
    tel_import.add_argument("paths", nargs="+")
    tel_import.add_argument("--db", help="SQLite telemetry database path")

    args = parser.parse_args(argv)
    if args.command == "scorers":
        try:
            loaded = load_scorers()
        except ScorerError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            payload = [
                {
                    "name": s.name,
                    "description": s.description,
                    "stages": list(s.stages),
                    "passingScore": s.passing_score,
                    "samplingRate": s.sampling_rate,
                    "model": s.model,
                    "selfImprovement": s.self_improvement,
                    "labels": list(s.labels.keys()),
                }
                for s in loaded.values()
            ]
            print(json.dumps(payload, indent=2))
        else:
            for s in loaded.values():
                print(
                    f"{s.name:<22} model={s.model:<20} pass>={s.passing_score} "
                    f"sample={s.sampling_rate} stages={','.join(s.stages)}"
                )
        return 0
    if args.command == "score":
        db = Path(args.db) if args.db else (telemetry_path or default_telemetry_path())
        store = TelemetryStore(db)
        if args.calibration:
            rep = calibration_report(store)
            print("Calibration:")
            for sname, bucket in sorted(rep["scorers"].items()):
                gh_pct = f"{bucket['github_agreement'] * 100:.1f}%" if bucket["github_agreement"] is not None else "n/a"
                print(
                    f"  {sname:<22} total={bucket['total']} insuf={bucket['insufficient_evidence']} "
                    f"gh_cmp={bucket['github_compared']} gh_agree={gh_pct}"
                )
            return 0
        try:
            all_scorers = load_scorers()
            if args.scorer_names:
                missing = [n for n in args.scorer_names if n not in all_scorers]
                if missing:
                    raise ScorerError(f"unknown scorer(s): {', '.join(missing)}")
                selected_specs = [all_scorers[n] for n in args.scorer_names]
            else:
                selected_specs = list(all_scorers.values())
            target_id = args.run_id or args.run_id_pos
            if args.all:
                target_ids = [r["run_id"] for r in store.list_runs(limit=100)]
            elif target_id:
                target_ids = [target_id]
            else:
                recent = store.list_runs(limit=1)
                if not recent:
                    print("no telemetry runs recorded", file=sys.stderr)
                    return 1
                target_ids = [recent[0]["run_id"]]
            for rid in target_ids:
                results = score_run(
                    store,
                    rid,
                    selected_specs,
                    judge=judge_runner,
                    force=args.force,
                    respect_sampling=not args.all_samples,
                )
                for row in results:
                    status = "PASS" if row["passed"] else "FAIL"
                    print(f"{row['run_id']} {row['scorer']}={row['label']} ({status}, {row['score']}) [{row['judge_model']}]")
        except ScorerError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if args.command == "stats":
        db = Path(args.db) if args.db else (telemetry_path or default_telemetry_path())
        store = TelemetryStore(db)
        if args.refresh:
            store.enrich_github_outcomes(repo=args.repo)
        stats = store.compute_stats(repo=args.repo, host=args.host, days=args.days)
        if args.format == "json":
            rendered = json.dumps(stats, indent=2, sort_keys=True)
        elif args.format == "html":
            rendered = render_stats_html(stats)
        else:
            rendered = render_stats_text(stats)
        if args.out:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(rendered + ("\n" if not rendered.endswith("\n") else ""), encoding="utf-8")
            print(str(out_path))
        else:
            print(rendered)
        return 0
    if args.command == "telemetry":
        db = Path(args.db) if args.db else (telemetry_path or default_telemetry_path())
        store = TelemetryStore(db)
        if args.telemetry_action == "export":
            out_path = Path(args.out) if args.out else None
            text = store.export_jsonl(out_path, since=args.since, host=args.host)
            if out_path is None:
                print(text, end="")
            else:
                print(str(out_path))
            return 0
        totals = {"runs": 0, "stage_calls": 0, "scores": 0}
        for src in args.paths:
            counts = store.import_jsonl(Path(src))
            for key, val in counts.items():
                totals[key] += val
        print(f"imported runs={totals['runs']} stage_calls={totals['stage_calls']} scores={totals['scores']}")
        return 0
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
            effective_telemetry = Path(args.telemetry) if args.telemetry else (telemetry_path or default_telemetry_path())
        else:
            effective_telemetry = (
                Path(args.telemetry)
                if args.telemetry
                else (telemetry_path or checkpoint.with_name("telemetry.sqlite3"))
            )
        budget = dict(load_pricing().get("budget") or {})
        if args.budget_usd is not None:
            budget["hard_usd"] = args.budget_usd
        if args.budget_tokens is not None:
            budget["hard_tokens"] = args.budget_tokens
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
        preflight_fn = None
        if args.preflight:
            def preflight_fn() -> tuple[bool, str]:
                proc = subprocess.run(
                    args.preflight, shell=True, cwd=cwd, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                )
                return proc.returncode == 0, (proc.stdout or "").strip()
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
                preflight=preflight_fn,
                telemetry_path=effective_telemetry,
                budget=budget,
            )
        except (OSError, QuotaExhausted, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if warning := result.get("budget_warning"):
            print(f"warning: {warning}", file=sys.stderr)
        _print_run(result)
        if args.score and (rid := result.get("run_id")):
            try:
                store = TelemetryStore(effective_telemetry)
                scored = score_run(store, rid, judge=judge_runner)
                for row in scored:
                    status = "PASS" if row["passed"] else "FAIL"
                    print(f"score {row['scorer']}={row['label']} ({status}) [{row['judge_model']}]")
            except (ScorerError, ValueError) as exc:
                print(f"scorer: {exc}", file=sys.stderr)
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
