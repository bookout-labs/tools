"""Isolated git-worktree benchmark runner, Pareto frontier analysis, and scatter HTML charts."""

from __future__ import annotations

from dataclasses import replace
import html
import json
from pathlib import Path
import re
import shutil
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Any, Callable
import yaml

from foreman.config import Config, NodeConfig, load_config
from foreman.delivery import clean_slices, compile_delivery, run_delivery
from foreman.dispatch import FailoverDispatcher, InvalidOutput
from foreman.dry_run import ScriptHandler
from foreman.redaction import SecretRedactor
from foreman.runner import _inspect_worktree
from foreman.scoring import JudgeRunner, load_scorers, score_run
from foreman.telemetry import TelemetryStore, state_usage_summary
from foreman.worker import CliWorker, CommandRunner, run_worker_command

FULL_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


class BenchmarkError(ValueError):
    pass


TrialRunner = Callable[[dict[str, Any], dict[str, Any], Path | None], dict[str, Any]]


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise BenchmarkError(f"could not read benchmark YAML {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BenchmarkError(f"benchmark YAML must be a mapping: {path}")
    return data


def load_suite(path: Path) -> dict[str, Any]:
    """Load and validate a benchmark suite.yaml and its task definitions."""
    suite_file = path / "suite.yaml" if path.is_dir() else path
    if not suite_file.is_file():
        raise BenchmarkError(f"benchmark suite not found: {suite_file}")
    suite_dir = suite_file.parent
    raw = _read_yaml_mapping(suite_file)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise BenchmarkError(f"{suite_file}: suite requires a non-empty name")

    configs_raw = raw.get("configs")
    if not isinstance(configs_raw, list) or not configs_raw:
        raise BenchmarkError(f"{suite_file}: suite requires at least one config")
    configs: list[dict[str, Any]] = []
    seen_configs: set[str] = set()
    for cfg in configs_raw:
        if not isinstance(cfg, dict) or not isinstance(cfg.get("name"), str) or not cfg["name"].strip():
            raise BenchmarkError(f"{suite_file}: every config requires a name")
        cname = cfg["name"].strip()
        if cname in seen_configs:
            raise BenchmarkError(f"{suite_file}: duplicate config name {cname}")
        seen_configs.add(cname)
        configs.append({**cfg, "name": cname})

    task_refs = raw.get("tasks")
    task_files: list[Path] = []
    if isinstance(task_refs, list) and task_refs:
        for ref in task_refs:
            p = suite_dir / str(ref)
            if p.is_file():
                task_files.append(p)
            else:
                matches = sorted(suite_dir.glob(str(ref)))
                if not matches:
                    raise BenchmarkError(f"{suite_file}: task pattern matched no files: {ref}")
                task_files.extend(m for m in matches if m.is_file())
    else:
        task_files = sorted((suite_dir / "tasks").glob("*.yaml"))

    if not task_files:
        raise BenchmarkError(f"{suite_file}: suite has no task YAML files")

    tasks: list[dict[str, Any]] = []
    for tf in task_files:
        t = _read_yaml_mapping(tf)
        tid = str(t.get("id") or tf.stem).strip()
        base_sha = str(t.get("base_sha") or "").strip()
        if not FULL_SHA.fullmatch(base_sha):
            raise BenchmarkError(f"{tf}: base_sha must be a 40-char hex commit SHA, got {base_sha!r}")
        request = t.get("request")
        if not isinstance(request, str) or not request.strip():
            raise BenchmarkError(f"{tf}: task {tid} requires request text")
        check = str(t.get("check") or "exit 0")
        tasks.append({
            "id": tid,
            "title": str(t.get("title") or tid).strip(),
            "request": request.strip(),
            "base_sha": base_sha.lower(),
            "check": check,
            "repo": str(t.get("repo") or raw.get("repo") or "Example/Engine"),
            "issue_kind": str(t.get("issue_kind") or "slice"),
            "expected_stop": str(t.get("expected_stop") or "verified"),
            "slice": t.get("slice"),
        })

    return {
        "name": name.strip(),
        "description": str(raw.get("description") or "").strip(),
        "repo": str(raw.get("repo") or "Example/Engine"),
        "configs": configs,
        "tasks": tasks,
        "path": str(suite_file),
    }


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


BENCHMARK_LOCKOUT_KWARGS: dict[str, Any] = {
    "publish_worktree": None,
    "record_review": None,
    "run_gh": None,
    "read_merge_hold": None,
}


def _config_for_trial(cfg: dict[str, Any]) -> Config:
    base = load_config()
    overrides = cfg.get("models")
    if not isinstance(overrides, dict) or not overrides:
        return base
    nodes = dict(base.nodes)
    for stage_name, models in overrides.items():
        if stage_name in nodes and isinstance(models, list) and all(isinstance(m, str) for m in models):
            nodes[stage_name] = NodeConfig(role=nodes[stage_name].role, models=tuple(models))
    return replace(base, nodes=nodes)


def _commit_detached_trial(worktree: Path, title: str) -> str:
    """Commit worktree edits onto detached HEAD so no branch ref leaks into the main repo."""
    status = _git(worktree, "status", "--porcelain")
    if status.returncode != 0:
        raise InvalidOutput("benchmark worktree status failed")
    if status.stdout.strip():
        if _git(worktree, "add", "-A").returncode != 0:
            raise InvalidOutput("benchmark could not stage changes")
        msg = title.strip() or "Benchmark trial commit"
        if _git(worktree, "commit", "-m", msg).returncode != 0:
            raise InvalidOutput("benchmark commit failed")
    head = _git(worktree, "rev-parse", "HEAD")
    if head.returncode != 0 or not FULL_SHA.fullmatch(head.stdout.strip()):
        raise InvalidOutput("benchmark worktree HEAD invalid")
    return head.stdout.strip()


def _read_worktree_diff(worktree: Path, base_sha: str) -> str:
    diff = _git(worktree, "diff", "--no-color", "--unified=3", base_sha, "HEAD")
    return diff.stdout if diff.returncode == 0 else ""


def _run_live_trial(
    task: dict[str, Any],
    cfg: dict[str, Any],
    worktree: Path | None,
    *,
    worker_runner: CommandRunner = run_worker_command,
    judge_runner: JudgeRunner | None = None,
    score_trials: bool = False,
) -> dict[str, Any]:
    """Run a live delivery trial inside an isolated detached worktree with publish/merge disabled."""
    if worktree is None:
        raise BenchmarkError("live benchmark trial requires an isolated worktree")
    t0 = time.monotonic()

    def run_check(command: str) -> tuple[int, str]:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        redacted = SecretRedactor.from_environ().redact(proc.stdout or "")
        return proc.returncode, redacted[-4000:]

    config = _config_for_trial(cfg)
    dispatcher = FailoverDispatcher(CliWorker(worktree, worker_runner))
    graph = compile_delivery(
        dispatcher,
        config,
        run_check=run_check,
        inspect_worktree=lambda: _inspect_worktree(worktree),
        commit_worktree=lambda title: _commit_detached_trial(worktree, title),
        read_diff=lambda: _read_worktree_diff(worktree, task["base_sha"]),
        budget=cfg.get("budget"),
        **BENCHMARK_LOCKOUT_KWARGS,
    )
    initial: dict[str, Any] = {
        "repo": task["repo"],
        "issue": 0,
        "request": f"{task['title']}\n\n{task['request']}",
        "check": task["check"],
        "issue_kind": task["issue_kind"],
        "verify_rounds": 0,
        "review_rounds": 0,
        "models_used": {},
        "sessions": {},
        "usage_records": [],
        "attempts": [],
        "path": [],
    }
    if task["issue_kind"] == "slice":
        raw_slice = task.get("slice") or {
            "title": task["title"],
            "acceptance": task["request"],
            "depends_on": [],
            "screen": "",
            "properties": [],
        }
        initial["slice"] = clean_slices([raw_slice], allow_external_dependencies=True)[0]

    result = graph.invoke(initial)
    elapsed_ms = max(1, int((time.monotonic() - t0) * 1000))
    usage = state_usage_summary(result.get("usage_records"))
    passed = result.get("stop") == task.get("expected_stop", "verified")

    quality_score: float | None = None
    quality_label = "pass" if passed else "fail"
    if score_trials or judge_runner is not None:
        with TemporaryDirectory(prefix="foreman_bench_score_") as td:
            store = TelemetryStore(Path(td) / "bench.sqlite3")
            rid = store.record_run(result, duration_ms=elapsed_ms)
            diff_text = _read_worktree_diff(worktree, task["base_sha"])
            scored = score_run(
                store,
                rid,
                list(load_scorers().values()),
                judge=judge_runner,
                respect_sampling=False,
                diff_text=diff_text,
            )
            evaluable = [s["score"] for s in scored if s["label"] != "insufficient_evidence"]
            if evaluable:
                quality_score = round(sum(evaluable) / len(evaluable), 4)
                quality_label = "pass" if all(s["passed"] for s in scored if s["label"] != "insufficient_evidence") else "fail"
            elif scored:
                quality_score = 0.5
                quality_label = "insufficient_evidence"

    return {
        "task_id": task["id"],
        "config": cfg["name"],
        "stop": result.get("stop"),
        "passed": passed,
        "quality_score": quality_score if quality_score is not None else (1.0 if passed else 0.0),
        "quality_label": quality_label,
        "cost_usd": usage["cost_usd"],
        "duration_ms": elapsed_ms,
        "tokens": usage["total_tokens"],
        "merge_armed": bool(result.get("merge_armed", False)),
    }


def _default_dry_trial(task: dict[str, Any], cfg: dict[str, Any], worktree: Path | None) -> dict[str, Any]:
    """Run deterministic in-memory delivery graph with publish and merge disabled."""
    t0 = time.monotonic()
    res = run_delivery(
        FailoverDispatcher(ScriptHandler(triage=cfg.get("script_triage", "implement"))),
        repo=task["repo"],
        request=task["request"],
        check=task["check"],
        issue_kind=task["issue_kind"],
        budget=cfg.get("budget"),
    )
    elapsed_ms = max(1, int((time.monotonic() - t0) * 1000))
    passed = res.get("stop") == task.get("expected_stop", "verified")
    return {
        "task_id": task["id"],
        "config": cfg["name"],
        "stop": res.get("stop"),
        "passed": passed,
        "quality_score": float(cfg.get("synthetic_quality", 1.0 if passed else 0.2)),
        "quality_label": "pass" if passed else "fail",
        "cost_usd": float(cfg.get("synthetic_cost_usd", 0.15)),
        "duration_ms": int(cfg.get("synthetic_duration_ms", elapsed_ms)),
        "tokens": int(cfg.get("synthetic_tokens", 1200)),
        "merge_armed": bool(res.get("merge_armed", False)),
    }


def run_benchmark(
    suite: dict[str, Any],
    *,
    repo_path: Path | None = None,
    trial_runner: TrialRunner | None = None,
    worktree_root: Path | None = None,
    dry_run: bool = False,
    worker_runner: CommandRunner = run_worker_command,
    judge_runner: JudgeRunner | None = None,
    score_trials: bool = False,
) -> list[dict[str, Any]]:
    """Execute suite configs x tasks in isolated detached worktrees with guaranteed cleanup."""
    results: list[dict[str, Any]] = []
    if trial_runner is not None:
        runner = trial_runner
    elif dry_run:
        runner = _default_dry_trial
    else:
        def runner(t, c, wt):
            return _run_live_trial(
                t, c, wt,
                worker_runner=worker_runner,
                judge_runner=judge_runner,
                score_trials=score_trials,
            )

    for cfg in suite["configs"]:
        for task in suite["tasks"]:
            if dry_run and trial_runner is None and repo_path is None:
                results.append(runner(task, cfg, None))
                continue

            if repo_path is None:
                if not dry_run:
                    raise BenchmarkError("live benchmark trials require repo_path")
                results.append(runner(task, cfg, None))
                continue

            with TemporaryDirectory(prefix="foreman_bench_", dir=worktree_root) as tmpdir:
                wt_dir = Path(tmpdir) / f"{cfg['name']}-{task['id']}"
                # In --dry-run, only create detached worktree when the commit exists locally.
                has_commit = _git(repo_path, "cat-file", "-e", f"{task['base_sha']}^{{commit}}").returncode == 0
                if not has_commit:
                    if dry_run:
                        results.append(runner(task, cfg, None))
                        continue
                    raise BenchmarkError(
                        f"base_sha {task['base_sha']} for task {task['id']} not found in {repo_path}"
                    )
                created = _git(repo_path, "worktree", "add", "--detach", str(wt_dir), task["base_sha"])
                if created.returncode != 0:
                    raise BenchmarkError(f"could not create detached worktree: {created.stderr.strip()}")
                try:
                    # Compile-time publish/merge lockout guarantee if runner inspects env/locks.
                    trial = runner(task, cfg, wt_dir)
                    results.append(trial)
                finally:
                    _git(repo_path, "worktree", "remove", "--force", str(wt_dir))
                    _git(repo_path, "worktree", "prune")
                    if wt_dir.exists():
                        shutil.rmtree(wt_dir, ignore_errors=True)
    return results


def _dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
    aq = a["avg_quality"] if a["avg_quality"] is not None else a["pass_rate"]
    bq = b["avg_quality"] if b["avg_quality"] is not None else b["pass_rate"]
    better_or_eq = (
        a["pass_rate"] >= b["pass_rate"]
        and aq >= bq
        and a["avg_cost_usd"] <= b["avg_cost_usd"]
        and a["avg_duration_ms"] <= b["avg_duration_ms"]
    )
    strictly_better = (
        a["pass_rate"] > b["pass_rate"]
        or aq > bq
        or a["avg_cost_usd"] < b["avg_cost_usd"]
        or a["avg_duration_ms"] < b["avg_duration_ms"]
    )
    return better_or_eq and strictly_better


def summarize_benchmark(raw_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute per-config averages (excluding insufficient_evidence) and Pareto frontier."""
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in raw_results:
        grouped.setdefault(str(row["config"]), []).append(row)

    configs: list[dict[str, Any]] = []
    for cname, rows in grouped.items():
        n = len(rows)
        passed = sum(1 for r in rows if r.get("passed"))
        evaluable_scores = [
            float(r["quality_score"])
            for r in rows
            if r.get("quality_label") != "insufficient_evidence"
            and isinstance(r.get("quality_score"), (int, float))
            and not isinstance(r.get("quality_score"), bool)
        ]
        insuf = sum(1 for r in rows if r.get("quality_label") == "insufficient_evidence")
        avg_cost = sum(float(r.get("cost_usd") or 0.0) for r in rows) / n
        avg_dur = sum(float(r.get("duration_ms") or 0.0) for r in rows) / n
        avg_tok = sum(int(r.get("tokens") or 0) for r in rows) / n
        configs.append({
            "config": cname,
            "trials": n,
            "passed": passed,
            "pass_rate": round(passed / n, 4),
            "avg_quality": round(sum(evaluable_scores) / len(evaluable_scores), 4) if evaluable_scores else None,
            "insufficient_evidence": insuf,
            "avg_cost_usd": round(avg_cost, 6),
            "avg_duration_ms": int(round(avg_dur)),
            "avg_tokens": int(round(avg_tok)),
        })

    frontier: list[str] = []
    for cand in configs:
        dominated = any(_dominates(other, cand) for other in configs if other["config"] != cand["config"])
        cand["pareto"] = not dominated
        if not dominated:
            frontier.append(cand["config"])

    def _quality_key(c: dict[str, Any]) -> tuple[float, float, float]:
        q = c["avg_quality"] if c["avg_quality"] is not None else c["pass_rate"]
        return (q, c["pass_rate"], -c["avg_cost_usd"])

    highest_quality = max(configs, key=_quality_key)["config"] if configs else None
    most_efficient = (
        max(
            configs,
            key=lambda c: (
                (c["avg_quality"] if c["avg_quality"] is not None else c["pass_rate"])
                / max(c["avg_cost_usd"], 0.0001)
            ),
        )["config"]
        if configs
        else None
    )
    passing_cfgs = [c for c in configs if c["pass_rate"] >= 1.0] or [c for c in configs if c["pass_rate"] > 0]
    lowest_cost_passing = (
        min(passing_cfgs, key=lambda c: c["avg_cost_usd"])["config"] if passing_cfgs else None
    )

    return {
        "configs": sorted(configs, key=lambda c: (-c["pass_rate"], c["avg_cost_usd"])),
        "pareto_frontier": frontier,
        "recommendations": {
            "highest_quality": highest_quality,
            "most_efficient": most_efficient,
            "lowest_cost_passing": lowest_cost_passing,
        },
        "trials": raw_results,
    }


def render_benchmark_text(summary: dict[str, Any]) -> str:
    lines = ["Config results:"]
    for cfg in summary.get("configs", []):
        star = "*" if cfg.get("pareto") else " "
        q = f"{cfg['avg_quality']:.2f}" if cfg["avg_quality"] is not None else "n/a"
        lines.append(
            f" {star} {cfg['config']:<20} pass={cfg['pass_rate'] * 100:>5.1f}% "
            f"quality={q:<5} cost=${cfg['avg_cost_usd']:.2f} ms={cfg['avg_duration_ms']} "
            f"insuf={cfg['insufficient_evidence']}"
        )
    recs = summary.get("recommendations", {})
    lines.append("")
    lines.append(
        "Recommendations: "
        f"highest_quality={recs.get('highest_quality')}  "
        f"most_efficient={recs.get('most_efficient')}  "
        f"lowest_cost_passing={recs.get('lowest_cost_passing')}"
    )
    return "\n".join(lines)


def render_benchmark_html(summary: dict[str, Any], *, title: str = "Foreman Benchmark Pareto Report") -> str:
    configs = summary.get("configs", [])
    max_cost = max([c["avg_cost_usd"] for c in configs] + [0.01])

    dots: list[str] = []
    for c in configs:
        q = c["avg_quality"] if c["avg_quality"] is not None else c["pass_rate"]
        x = 60 + int((c["avg_cost_usd"] / max_cost) * 520)
        y = 240 - int(q * 200)
        color = "#22d3ee" if c.get("pareto") else "#94a3b8"
        r = 7 if c.get("pareto") else 5
        dots.append(
            f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}" />'
            f'<text x="{x + 10}" y="{y + 4}" fill="#e2e8f0" font-size="11" font-family="monospace">'
            f'{html.escape(c["config"])}</text>'
        )

    rows = "".join(
        f"<tr><td>{html.escape(c['config'])}{' ★' if c.get('pareto') else ''}</td>"
        f"<td>{c['pass_rate'] * 100:.1f}%</td>"
        f"<td>{c['avg_quality'] if c['avg_quality'] is not None else 'n/a'}</td>"
        f"<td>${c['avg_cost_usd']:.2f}</td><td>{c['avg_duration_ms']}ms</td></tr>"
        for c in configs
    )
    recs = summary.get("recommendations", {})
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0f172a; color: #f8fafc; margin: 24px; }}
  svg {{ background: #1e293b; border: 1px solid #334155; border-radius: 8px; width: 100%; max-width: 700px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 16px; background: #1e293b; }}
  th, td {{ text-align: left; padding: 8px 12px; border-bottom: 1px solid #334155; font-size: 13px; }}
</style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <p>Highest quality: <strong>{html.escape(str(recs.get('highest_quality')))}</strong> |
     Most efficient: <strong>{html.escape(str(recs.get('most_efficient')))}</strong> |
     Lowest cost passing: <strong>{html.escape(str(recs.get('lowest_cost_passing')))}</strong></p>
  <svg viewBox="0 0 680 280" xmlns="http://www.w3.org/2000/svg">
    <line x1="60" y1="240" x2="620" y2="240" stroke="#475569" />
    <line x1="60" y1="30" x2="60" y2="240" stroke="#475569" />
    <text x="300" y="268" fill="#94a3b8" font-size="11">Avg Cost (USD)</text>
    <text x="12" y="140" fill="#94a3b8" font-size="11">Quality</text>
    {"".join(dots)}
  </svg>
  <table><thead><tr><th>Config</th><th>Pass Rate</th><th>Avg Quality</th><th>Avg Cost</th><th>Avg Latency</th></tr></thead><tbody>{rows}</tbody></table>
</body>
</html>
"""
