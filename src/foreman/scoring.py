"""YAML-frontmatter LLM-as-judge scorers, cross-family selection, sampling, and calibration."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterable, Mapping
import yaml

from foreman.config import ConfigError, family
from foreman.dispatch import InvalidOutput
from foreman.redaction import redact_text
from foreman.telemetry import TelemetryStore
from foreman.transcripts import build_run_evidence
from foreman.worker import CommandRunner, json_object, model_and_effort, run_worker_command

SCORERS_DIR = Path(__file__).with_name("scorers")
SCORE_SCHEMA = {
    "type": "object",
    "required": ["label", "reason"],
    "properties": {
        "label": {"type": "string"},
        "reason": {"type": "string"},
    },
    "additionalProperties": False,
}

JudgeRunner = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class ScorerError(ValueError):
    pass


@dataclass(frozen=True)
class ScorerSpec:
    name: str
    description: str
    stages: tuple[str, ...]
    labels: dict[str, dict[str, Any]]
    passing_score: float
    sampling_rate: float
    model: str
    self_improvement: bool
    prompt: str
    path: Path


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ScorerError("scorer file must begin with YAML frontmatter '---'")
    end_idx = None
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            end_idx = idx
            break
    if end_idx is None:
        raise ScorerError("scorer frontmatter is missing closing '---'")
    raw_yaml = "\n".join(lines[1:end_idx])
    try:
        meta = yaml.safe_load(raw_yaml)
    except yaml.YAMLError as exc:
        raise ScorerError(f"invalid scorer YAML frontmatter: {exc}") from exc
    if not isinstance(meta, dict):
        raise ScorerError("scorer frontmatter must be a mapping")
    body = "\n".join(lines[end_idx + 1 :]).strip()
    if not body:
        raise ScorerError("scorer file must contain judge prompt instructions after frontmatter")
    return meta, body


def parse_scorer_file(path: Path) -> ScorerSpec:
    """Parse and strictly validate a Warp-style scorer.md rubric definition."""
    meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))

    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScorerError(f"{path}: scorer requires non-empty name")
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        raise ScorerError(f"{path}: scorer {name} requires description")

    stages_raw = meta.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw or not all(
        isinstance(s, str) and s.strip() for s in stages_raw
    ):
        raise ScorerError(f"{path}: scorer {name} requires non-empty stages list")

    labels_raw = meta.get("labels")
    if not isinstance(labels_raw, list) or not labels_raw:
        raise ScorerError(f"{path}: scorer {name} requires non-empty labels list")
    labels: dict[str, dict[str, Any]] = {}
    for entry in labels_raw:
        if not isinstance(entry, dict):
            raise ScorerError(f"{path}: label entry must be a mapping")
        val = entry.get("value")
        desc = entry.get("description")
        score = entry.get("score")
        if not isinstance(val, str) or not val.strip():
            raise ScorerError(f"{path}: label missing string value")
        if not isinstance(desc, str) or not desc.strip():
            raise ScorerError(f"{path}: label {val} missing description")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= float(score) <= 1:
            raise ScorerError(f"{path}: label {val} score must be a number in [0, 1]")
        if val in labels:
            raise ScorerError(f"{path}: duplicate label {val}")
        labels[val] = {"value": val, "description": desc.strip(), "score": float(score)}

    if "insufficient_evidence" not in labels:
        raise ScorerError(f"{path}: scorer {name} must define 'insufficient_evidence' label")

    passing = meta.get("passingScore")
    if not isinstance(passing, (int, float)) or isinstance(passing, bool) or not 0 <= float(passing) <= 1:
        raise ScorerError(f"{path}: passingScore must be a number in [0, 1]")

    sampling = meta.get("samplingRate")
    if not isinstance(sampling, (int, float)) or isinstance(sampling, bool) or not 0 < float(sampling) <= 1:
        raise ScorerError(f"{path}: samplingRate must be a number in (0, 1]")

    model = meta.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ScorerError(f"{path}: scorer {name} requires model")
    slug, _ = model_and_effort(model.strip())
    try:
        family(slug)
    except ConfigError as exc:
        raise ScorerError(f"{path}: unknown scorer model family for {model}: {exc}") from exc

    self_imp = meta.get("selfImprovement")
    if not isinstance(self_imp, bool):
        raise ScorerError(f"{path}: selfImprovement must be a boolean")

    return ScorerSpec(
        name=name.strip(),
        description=description.strip(),
        stages=tuple(s.strip() for s in stages_raw),
        labels=labels,
        passing_score=float(passing),
        sampling_rate=float(sampling),
        model=model.strip(),
        self_improvement=self_imp,
        prompt=body,
        path=path,
    )


def load_scorers(directory: Path | None = None) -> dict[str, ScorerSpec]:
    """Load all */scorer.md rubrics keyed by scorer name."""
    root = directory or SCORERS_DIR
    loaded: dict[str, ScorerSpec] = {}
    if not root.exists():
        return loaded
    for scorer_file in sorted(root.glob("*/scorer.md")):
        spec = parse_scorer_file(scorer_file)
        if spec.name in loaded:
            raise ScorerError(f"duplicate scorer name {spec.name}")
        loaded[spec.name] = spec
    return loaded


def should_sample(run_id: str, scorer: ScorerSpec) -> bool:
    """Deterministically decide whether run_id falls inside scorer.sampling_rate."""
    if scorer.sampling_rate >= 1.0:
        return True
    digest = hashlib.sha256(f"{run_id}:{scorer.name}".encode("utf-8")).digest()
    ratio = int.from_bytes(digest[:8], "big") / float(1 << 64)
    return ratio < scorer.sampling_rate


def _safe_family(model_name: object) -> str | None:
    if not isinstance(model_name, str) or not model_name.strip():
        return None
    slug, _ = model_and_effort(model_name.strip())
    try:
        return family(slug)
    except ConfigError:
        return None


def select_judge_model(
    scorer: ScorerSpec,
    models_used: Mapping[str, str] | None = None,
    *,
    implement_models: Iterable[str] | None = None,
    fallback_order: tuple[str, ...] = ("claude-sonnet-5-5-high", "grok-4.7-high", "gpt-6.2-sol-high"),
) -> str:
    """Pick a judge model from a different provider family than every code author."""
    used = models_used or {}
    builders = [*(implement_models or ()), used.get("implement")]
    impl_families = {fam for fam in (_safe_family(m) for m in builders) if fam}
    review_family = _safe_family(used.get("review"))

    avoid = set(impl_families)
    if review_family and ("review" in scorer.stages or scorer.name == "review-quality"):
        avoid.add(review_family)

    candidates = (scorer.model, *fallback_order)
    for exclusions in (avoid, impl_families):
        for cand in candidates:
            cand_family = _safe_family(cand)
            if cand_family and cand_family not in exclusions:
                return cand
    return scorer.model


def default_judge_runner(cwd: Path | None = None, run: CommandRunner = run_worker_command) -> JudgeRunner:
    """Create a read-only CLI judge caller using native schema enforcement."""
    workdir = cwd or Path.cwd()

    def _call(judge_model: str, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        provider = _safe_family(judge_model)
        if provider is None:
            raise ScorerError(f"unsupported judge model {judge_model}")
        slug, effort = model_and_effort(judge_model)
        with TemporaryDirectory(prefix="foreman_judge_") as tmpdir:
            tmp = Path(tmpdir)
            if provider == "gpt":
                schema_file = tmp / "schema.json"
                out_file = tmp / "judge.json"
                schema_file.write_text(json.dumps(schema), encoding="utf-8")
                effort_args = ["-c", f"model_reasoning_effort={effort}"] if effort else []
                args = [
                    "codex", "exec", "-m", slug, *effort_args,
                    "--color", "never", "-s", "read-only",
                    "--json", "--output-schema", str(schema_file), "-o", str(out_file),
                    prompt,
                ]
                proc = run(args, workdir)
                if proc.returncode != 0:
                    raise ScorerError(f"codex judge failed ({proc.returncode}): {proc.stderr or proc.stdout}")
                file_raw = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
                if file_raw.strip():
                    return json_object(file_raw)
                from foreman.worker import _parse_codex_events
                ev_body, _ = _parse_codex_events(proc.stdout, slug)
                return json_object(ev_body if ev_body is not None else proc.stdout)

            options = [
                "--output-format", "json",
                "--json-schema", json.dumps(schema),
                "--permission-mode", "plan",
            ]
            if provider == "claude":
                effort_args = ["--effort", effort] if effort else []
                args = ["claude", "--model", slug, *effort_args, *options, "-p", prompt]
            else:
                effort_args = ["--reasoning-effort", effort] if effort else []
                args = ["grok", "-m", slug, *effort_args, *options, "--no-subagents", "-p", prompt]
            proc = run(args, workdir)
            if proc.returncode != 0:
                raise ScorerError(f"{provider} judge failed ({proc.returncode}): {proc.stderr or proc.stdout}")
            envelope = json_object(proc.stdout)
            structured = (
                envelope.get("structured_output")
                if envelope.get("structured_output") is not None
                else envelope.get("structuredOutput")
            )
            if isinstance(structured, dict):
                return structured
            if isinstance(structured, str) and structured.strip():
                return json_object(structured)
            body = envelope.get("result" if provider == "claude" else "text")
            return json_object(body)

    return _call


def _format_judge_prompt(scorer: ScorerSpec, evidence: str) -> str:
    label_lines = [
        f"- `{spec['value']}` (score={spec['score']}): {spec['description']}"
        for spec in scorer.labels.values()
    ]
    return (
        f"You are an independent LLM judge running scorer `{scorer.name}`.\n"
        f"Description: {scorer.description}\n\n"
        f"Instructions:\n{scorer.prompt}\n\n"
        f"Valid labels (return JSON with `label` and concise `reason`):\n"
        + "\n".join(label_lines)
        + f"\n\nRun evidence (already redacted):\n{evidence}"
    )


def score_run(
    store: TelemetryStore,
    run_id: str,
    scorers: list[ScorerSpec] | None = None,
    *,
    judge: JudgeRunner | None = None,
    force: bool = False,
    respect_sampling: bool = True,
    search_roots: Mapping[str, Path] | None = None,
    diff_text: str = "",
) -> list[dict[str, Any]]:
    """Score one recorded telemetry run across applicable scorers and upsert results."""
    run = store.get_run(run_id)
    if run is None:
        raise ScorerError(f"unknown run_id {run_id}")

    specs = scorers if scorers is not None else list(load_scorers().values())
    existing = {row["scorer"] for row in store.list_scores(run_id=run_id)}
    calls = store.list_stage_calls(run_id)
    evidence = build_run_evidence(
        run,
        stage_calls=calls,
        search_roots=search_roots,
        diff_text=diff_text,
    )
    runner = judge or default_judge_runner()
    recorded: list[dict[str, Any]] = []
    state = run.get("state") or {}
    path = state.get("path") if isinstance(state.get("path"), list) else []
    observed = {str(node) for node in path} | {str(c.get("node")) for c in calls if c.get("node")}

    for spec in specs:
        if not force and spec.name in existing:
            continue
        if observed and not observed.intersection(spec.stages):
            continue
        if respect_sampling and not force and not should_sample(run_id, spec):
            continue
        judge_model = select_judge_model(
            spec, run.get("models_used"), implement_models=state.get("implement_models")
        )
        prompt = _format_judge_prompt(spec, evidence)
        raw_result = runner(judge_model, prompt, SCORE_SCHEMA)
        if not isinstance(raw_result, dict):
            raise InvalidOutput(f"judge for {spec.name} did not return a JSON object")
        label = str(raw_result.get("label") or "").strip()
        if label not in spec.labels:
            raise InvalidOutput(f"judge for {spec.name} returned invalid label {label!r}")
        reason = redact_text(str(raw_result.get("reason") or "").strip(), redact_pii=True)
        label_score = float(spec.labels[label]["score"])
        # Warp invariant: insufficient_evidence never passes regardless of numeric score.
        passed = False if label == "insufficient_evidence" else label_score >= spec.passing_score

        store.upsert_score(
            run_id=run_id,
            scorer=spec.name,
            label=label,
            score=label_score,
            passed=passed,
            reason=reason,
            judge_model=judge_model,
        )
        recorded.append({
            "run_id": run_id,
            "scorer": spec.name,
            "label": label,
            "score": label_score,
            "passed": passed,
            "reason": reason,
            "judge_model": judge_model,
        })
    return recorded


def calibration_report(
    store: TelemetryStore,
    *,
    human_labels: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, Any]:
    """Compare scorer verdicts against human ground-truth labels and GitHub PR outcomes."""
    runs_by_id = {r["run_id"]: r for r in store.list_runs(limit=10000)}
    scores = store.list_scores()
    by_scorer: dict[str, dict[str, Any]] = {}

    for row in scores:
        scorer = str(row["scorer"])
        bucket = by_scorer.setdefault(
            scorer,
            {
                "total": 0,
                "insufficient_evidence": 0,
                "human_compared": 0,
                "human_agreed": 0,
                "human_agreement": None,
                "github_compared": 0,
                "github_agreed": 0,
                "github_agreement": None,
            },
        )
        bucket["total"] += 1
        label = str(row.get("label") or "")
        if label == "insufficient_evidence":
            bucket["insufficient_evidence"] += 1
            continue

        passed = bool(row.get("passed"))
        rid = str(row["run_id"])

        if human_labels and rid in human_labels and scorer in human_labels[rid]:
            expected = str(human_labels[rid][scorer]).strip()
            bucket["human_compared"] += 1
            if expected in {"pass", "fail"}:
                if passed == (expected == "pass"):
                    bucket["human_agreed"] += 1
            elif label == expected:
                bucket["human_agreed"] += 1

        run = runs_by_id.get(rid)
        # Open PRs have no outcome yet; comparing them would score the judge against noise.
        if run and run.get("gh_state") in {"MERGED", "CLOSED"}:
            zero_touch = (
                run.get("gh_state") == "MERGED"
                and run.get("gh_merged") == 1
                and (run.get("gh_human_comments") or 0) == 0
                and (run.get("gh_review_changes") or 0) == 0
                and (run.get("gh_human_commits") or 0) == 0
            )
            bucket["github_compared"] += 1
            if passed == zero_touch:
                bucket["github_agreed"] += 1

    for bucket in by_scorer.values():
        if bucket["human_compared"] > 0:
            bucket["human_agreement"] = round(bucket["human_agreed"] / bucket["human_compared"], 4)
        if bucket["github_compared"] > 0:
            bucket["github_agreement"] = round(bucket["github_agreed"] / bucket["github_compared"], 4)

    return {"scorers": by_scorer}
