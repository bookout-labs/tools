"""The delivery graph. Edges live here. Models live in models.toml."""

from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Callable, TypedDict
import operator
import re

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from foreman.config import Config, ConfigError, load_config, order_for
from foreman.dispatch import Attempt, BudgetExceeded, CliMissing, FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.proof import browser_proof_label, formal_proof_label
from foreman.redaction import redact_text
from foreman.review_validation import (
    annotate_unified_diff,
    build_diff_hunk_maps,
    build_diff_maps_from_annotated_diff,
    validate_review_comments,
)
from foreman.telemetry import load_pricing, state_usage_summary
from foreman.watcher import Proposal, watch_run

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "verify", "review")
STAGE_ORDER = REQUIRED_NODES + ("merge",)
SKILLS_FOR = {
    "review": ("review-quality", "merge-risk"),
}
TRIAGE_OUTCOMES = {"implement", "spec", "question", "needs_human"}
SPEC_GATES = {"ready", "needs_human"}
REVIEW_OUTCOMES = {"approve", "changes"}
VERIFY_OUTCOMES = {"pass", "fail"}
RISK_FIELDS = ("alignment", "impact", "verification", "rollback", "error_detection")
RISK_LEVELS = {"low", "medium", "high"}
TEXT_FIELDS = {"implement": "implementation", "research": "research"}
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
# 127: the shell could not find a program. 126: it found one that could not start.
UNAVAILABLE_CHECK = {126: "could not run", 127: "was not found"}
ENV_FAILURE_PATTERNS = re.compile(
    r"No PostgreSQL server answered|could not connect to server|ECONNREFUSED|"
    r"database [\"']?\S+[\"']? does not exist|ENOSPC|ENOMEM|Cannot connect to the Docker daemon",
    re.IGNORECASE,
)


class SliceProperty(TypedDict):
    name: str
    constraint: str


class Slice(TypedDict):
    title: str
    acceptance: str
    depends_on: list[str]
    screen: str
    properties: list[SliceProperty]


class DeliveryState(TypedDict, total=False):
    run_id: str
    started_at: str
    request: str
    repo: str
    issue: int
    workspace: str
    check: str
    check_exit_code: int
    check_output: str
    check_exits: Annotated[list[int], operator.add]
    review_log: Annotated[list[dict[str, str]], operator.add]
    triage: str
    spec: str
    spec_gate: str
    slices: list[Slice]
    child_issue_numbers: list[int]
    slice: Slice
    issue_kind: str
    research: str
    implementation: str
    pr_summary: str
    head_sha: str | None
    head_checked: bool
    merge_armed: bool
    merge_args: list[str]
    pull_request: str
    browser_proof: dict | None
    formal_proof: dict | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    review: str
    review_note: str
    review_comments: list[dict]
    review_validation_errors: list[str]
    risk: dict[str, str]
    verify: str
    verify_rounds: int
    review_rounds: int
    models_used: dict[str, str]
    implement_models: list[str]
    sessions: dict[str, str]
    usage_records: Annotated[list[dict], operator.add]
    attempts: Annotated[list[Attempt], operator.add]
    proposal: Proposal | None
    halt: str
    halt_detail: str
    failure_phase: str
    failure_reason: str
    budget_warning: str
    stop: str
    path: Annotated[list[str], operator.add]


def stage_text(name: str) -> str:
    path = PACKAGE / "stages" / f"{name}.md"
    text = path.read_text().strip()
    for skill in SKILLS_FOR.get(name, ()):
        skill_text = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
        text = f"{text}\n\n{skill_text}"
    return text


def required_text(value: object, detail: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InvalidOutput(detail)
    return value


def stage_document(value: object, name: str) -> str:
    """The graph stores one document for a text stage.

    A string is that document. An object of strings is the same document split
    into sections. Lists, nested values, and blank sections are not a document.
    """
    detail = f"{name} did not return text"
    if isinstance(value, str):
        return value
    if not isinstance(value, dict) or not value:
        raise InvalidOutput(detail)
    parts: list[str] = []
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
            raise InvalidOutput(detail)
        parts.append(f"{key}\n{item.strip()}")
    return "\n\n".join(parts)


def choice(value: object, options: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise InvalidOutput(f"{field} must be one of {', '.join(sorted(options))}")
    return value


def review_verdict(value: object) -> tuple[str, str]:
    """A review is approve or changes, optionally followed by punctuation and a note.

    A sentence that merely contains one of those words is not a verdict.
    """
    detail = "review must be one of approve, changes"
    if not isinstance(value, str):
        raise InvalidOutput(detail)
    match = re.fullmatch(
        r"(approve|changes)(?:[.,:;]\s*([\s\S]*))?$",
        value.strip(),
        re.IGNORECASE,
    )
    if not match:
        raise InvalidOutput(detail)
    return match.group(1).lower(), (match.group(2) or "").strip()


def review_comment(state: DeliveryState) -> str:
    """The pull-request record of who reviewed this commit and what they decided.

    The issue request stays out of the comment. The pull request is public.
    """
    model = (state.get("models_used") or {}).get("review")
    if not isinstance(model, str) or not model.strip():
        raise InvalidOutput("the review model was not recorded")
    verdict = state.get("review")
    if verdict not in REVIEW_OUTCOMES:
        raise InvalidOutput("the review verdict was not recorded")
    risk = state.get("risk") or {}
    level = risk.get("level")
    if level not in RISK_LEVELS:
        raise InvalidOutput("the review risk was not recorded")
    lines = [
        f"Reviewed by {model.strip()}.",
        f"Verdict: {verdict}.",
        f"Risk: {level}.",
    ]
    labels = {
        "alignment": "Alignment",
        "impact": "Impact",
        "verification": "Verification",
        "rollback": "Rollback",
        "error_detection": "Error detection",
    }
    for field, label in labels.items():
        text = risk.get(field)
        if isinstance(text, str) and text.strip():
            lines.append(f"{label}: {text.strip()}")
    note = state.get("review_note")
    if isinstance(note, str) and note.strip():
        lines.extend(["", note.strip()])
    return redact_text("\n".join(lines), redact_pii=True)


def render_pr_body(state: DeliveryState, issue: int) -> str:
    """Build a public-safe pull request body with summary, verification, and merge risk."""
    sections = [f"Closes #{issue}"]
    pr_summary = state.get("pr_summary")
    if isinstance(pr_summary, str) and pr_summary.strip():
        summary = pr_summary.strip()
    elif isinstance(state.get("slice"), dict) and isinstance(state["slice"].get("title"), str):
        summary = f"Implements slice: {state['slice']['title'].strip()}"
    else:
        summary = f"Implements requested changes for #{issue}."
    sections.append(f"## Summary\n{summary}")

    check_cmd = state.get("check") or "configured check"
    exit_code = state.get("check_exit_code")
    head = state.get("head_sha") or "unknown"
    ver_lines = [f"- Check `{check_cmd}` exit `{exit_code}` at `{head}`"]
    proof_labels = state.get("proof_labels") or {}
    for proof_kind, label in sorted(proof_labels.items()):
        ver_lines.append(f"- Proof `{proof_kind}`: `{label}`")
    sections.append("## Verification\n" + "\n".join(ver_lines))

    risk = state.get("risk") or {}
    if risk.get("level") in RISK_LEVELS:
        risk_lines = [f"- **Level:** `{risk['level']}`"]
        for field in RISK_FIELDS:
            val = risk.get(field)
            if isinstance(val, str) and val.strip():
                risk_lines.append(f"- **{field.replace('_', ' ').title()}:** {val.strip()}")
        sections.append("## Merge Risk\n" + "\n".join(risk_lines))

    return redact_text("\n\n".join(sections), redact_pii=True)


def clean_risk(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise InvalidOutput("review did not report merge risk")
    risk = {
        field: required_text(raw.get(field), f"review risk is missing {field}").strip()
        for field in RISK_FIELDS
    }
    risk["level"] = choice(raw.get("level"), RISK_LEVELS, "review risk level")
    return risk


def clean_slices(raw: object, *, allow_external_dependencies: bool = False) -> list[Slice]:
    if not isinstance(raw, list) or not raw:
        raise InvalidOutput("spec did not return slices")
    cleaned: list[Slice] = []
    for item in raw:
        if not isinstance(item, dict) or item.keys() != Slice.__required_keys__:
            raise InvalidOutput("slice needs exactly title, acceptance, depends_on, screen, and properties")
        title = required_text(item["title"], "slice needs a title").strip()
        acceptance = required_text(item["acceptance"], "slice needs acceptance").strip()
        depends = item["depends_on"]
        if not isinstance(depends, list):
            raise InvalidOutput("slice dependencies must be a list of titles")
        screen = item["screen"]
        if not isinstance(screen, str) or (
            screen and (".." in screen or not re.fullmatch(r"docs/screens/[^/\\\x00-\x1f]+\.md", screen))
        ):
            raise InvalidOutput("slice screen must be empty or a docs/screens/<file>.md path without '..'")
        properties = item["properties"]
        if not isinstance(properties, list):
            raise InvalidOutput("slice properties must be a list")
        cleaned_properties: list[SliceProperty] = []
        for prop in properties:
            if not isinstance(prop, dict) or prop.keys() != SliceProperty.__required_keys__:
                raise InvalidOutput("slice property needs exactly name and constraint")
            cleaned_properties.append({
                "name": required_text(prop["name"], "slice property needs a name").strip(),
                "constraint": required_text(prop["constraint"], "slice property needs a constraint").strip(),
            })
        cleaned.append({
            "title": title,
            "acceptance": acceptance,
            "depends_on": [required_text(dep, "slice dependency needs a title").strip() for dep in depends],
            "screen": screen,
            "properties": cleaned_properties,
        })
    graph = {item["title"]: item["depends_on"] for item in cleaned}
    if len(graph) != len(cleaned):
        raise InvalidOutput("slice titles must be unique")
    for deps in graph.values():
        for dep in deps:
            if dep not in graph and not allow_external_dependencies:
                raise InvalidOutput(f"unknown dependency {dep}")
    try:
        TopologicalSorter(graph).prepare()
    except CycleError as exc:
        raise InvalidOutput("slice dependencies cycle") from exc
    return cleaned


def read_output(name: str, raw: dict, state: DeliveryState) -> dict:
    update: dict = {"path": [name]}
    if name == "triage":
        update["triage"] = choice(raw.get("triage"), TRIAGE_OUTCOMES, "triage")
    elif name == "spec":
        gate = choice(raw.get("spec_gate"), SPEC_GATES, "spec gate")
        update["spec"] = required_text(stage_document(raw.get("spec"), "spec"), "spec did not return text")
        update["spec_gate"] = gate
        if state.get("issue_kind") == "parent" and gate == "ready":
            update["slices"] = clean_slices(raw.get("slices"))
    elif name in TEXT_FIELDS:
        key = TEXT_FIELDS[name]
        update[key] = required_text(stage_document(raw.get(key), name), f"{name} did not return text")
        if name == "implement":
            head = raw.get("head_sha")
            update["head_sha"] = (head.strip() or None) if isinstance(head, str) else None
            pr_summary = raw.get("pr_summary")
            if isinstance(pr_summary, str) and pr_summary.strip():
                update["pr_summary"] = pr_summary.strip()
            for field in ("browser_proof", "formal_proof"):
                artifact = raw.get(field)
                update[field] = artifact if isinstance(artifact, dict) else None
    elif name == "verify":
        verdict = choice(raw.get("verify"), VERIFY_OUTCOMES, "verify")
        update["verify"] = verdict
        if verdict == "fail":
            update["verify_rounds"] = state.get("verify_rounds", 0) + 1
    elif name == "review":
        verdict, note = review_verdict(raw.get("review"))
        explicit_note = raw.get("review_note")
        if not note and isinstance(explicit_note, str) and explicit_note.strip():
            note = explicit_note.strip()
        update["review"] = verdict
        update["review_note"] = note
        update["risk"] = clean_risk(raw.get("risk"))
        comments = raw.get("comments")
        update["review_comments"] = [c for c in comments if isinstance(c, dict)] if isinstance(comments, list) else []
        update["review_validation_errors"] = []
        if verdict == "changes":
            update["review_rounds"] = state.get("review_rounds", 0) + 1
    if name in {"triage", "spec"}:
        question = raw.get("human_question")
        update["human_question"] = question if isinstance(question, str) else ""
        ready = update.get("triage") == "implement" or update.get("spec_gate") == "ready"
        if ready and state.get("issue_kind") == "slice" and "slice" not in state and "slice" in raw:
            update["slice"] = clean_slices([raw["slice"]])[0]
    model = raw.get("model")
    if model:
        used = state.get("models_used", {})
        update["models_used"] = {**used, name: model}
        if name == "implement":
            latest = used.get("implement")
            history = state.get("implement_models", [latest] if latest else [])
            update["implement_models"] = [*history, model]
    if name == "review" and "review" in update:
        entry = {
            "verdict": update["review"],
            "level": update["risk"]["level"],
            "note": update.get("review_note") or "",
        }
        if model:
            entry["model"] = model
        update["review_log"] = [entry]
    return update


def stop_reason(state: DeliveryState) -> str:
    if halt := state.get("halt"):
        return halt
    if state.get("triage") == "needs_human" or state.get("spec_gate") == "needs_human":
        return "needs_human"
    if state.get("issue_kind") == "parent" and state.get("spec_gate") == "ready":
        return "sliced"
    if state.get("triage") == "question":
        return "answered"
    if state.get("verify") == "fail":
        return "verify_cap"
    if state.get("review") == "changes":
        return "review_cap"
    if state.get("verify") == "pass":
        return "verified"
    return "stopped"


def _budget_exceeded_detail(usage_records: list[dict], budget: dict | None, pricing_data: dict | None) -> str | None:
    if budget is None:
        return None
    usage = state_usage_summary(usage_records, pricing_data)
    hard_usd = float(budget.get("hard_usd") or 0)
    hard_tokens = int(budget.get("hard_tokens") or 0)
    if (hard_usd > 0 and usage["cost_usd"] >= hard_usd) or (hard_tokens > 0 and usage["total_tokens"] >= hard_tokens):
        return f"run budget ceiling reached: ${usage['cost_usd']:.2f} / {usage['total_tokens']} tokens"
    return None


def _budget_halt(name: str, detail: str) -> dict:
    return {
        "path": [name],
        "halt": "budget_exceeded",
        "halt_detail": detail,
        "failure_phase": "budget",
        "failure_reason": "budget_exceeded",
    }


def compile_delivery(
    dispatcher: FailoverDispatcher | None,
    config: Config | None = None,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    run_check: Callable[[str], int | tuple[int, str]] | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
    inspect_worktree: Callable[[], tuple[str, bool]] | None = None,
    commit_worktree: Callable[[str], str] | None = None,
    publish_worktree: Callable[[str], str] | None = None,
    record_review: Callable[[DeliveryState, str], None] | None = None,
    read_merge_hold: Callable[[str], str | None] | None = None,
    preflight: Callable[[], tuple[bool, str]] | None = None,
    budget: dict | None = None,
    read_diff: Callable[[], str] | None = None,
):
    config = config or load_config()
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}
    pricing_data = load_pricing() if budget is not None else None

    def bind(name: str):
        def node(state: DeliveryState) -> dict:
            prior_usage = state.get("usage_records") or []
            if detail := _budget_exceeded_detail(prior_usage, budget, pricing_data):
                return _budget_halt(name, detail)

            def budget_check(metas: list[dict]) -> str | None:
                return _budget_exceeded_detail([*prior_usage, *metas], budget, pricing_data)

            if name == "implement" and preflight is not None:
                ok, detail = preflight()
                if not ok:
                    return {
                        "path": [name],
                        "halt": "env_unavailable",
                        "halt_detail": redact_text(detail or "environment preflight failed", redact_pii=True),
                        "failure_phase": "preflight",
                        "failure_reason": "env_unavailable",
                    }
            if dispatcher is None:
                return {"path": [name], "halt": "no_dispatcher", "failure_phase": "setup", "failure_reason": "no_dispatcher"}
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return {
                    "path": [name],
                    "halt": "no_reviewer",
                    "halt_detail": str(exc),
                    "failure_phase": "setup",
                    "failure_reason": "no_reviewer",
                }
            instruction = prompts[name]
            if name == "implement" and (slice := state.get("slice")):
                for field, skill in (("screen", "browser-proof"), ("properties", "formal-proof")):
                    if slice[field]:
                        proof = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
                        instruction = f"{instruction}\n\n{proof}"
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction}\n\nCommand: {command}"
            diff_line_map = None
            diff_content_map = None
            diff_hunk_map = None
            if name == "review" and read_diff is not None:
                raw_diff = read_diff() or ""
                annotated_diff = annotate_unified_diff(raw_diff)
                diff_line_map, diff_content_map = build_diff_maps_from_annotated_diff(annotated_diff)
                diff_hunk_map = build_diff_hunk_maps(raw_diff)
                if raw_diff.strip():
                    prompt_diff = redact_text(annotated_diff, redact_pii=True)
                    instruction = (
                        f"{instruction}\n\n"
                        "Annotated diff (use [NEW:line] on side RIGHT and [OLD:line] on side LEFT):\n"
                        f"{prompt_diff[:20000]}"
                    )
            raw = None
            recorded: list[Attempt] = []
            try:
                raw = dispatcher.run(
                    name, models, state, instruction, attempts=recorded,
                    budget_check=budget_check if budget is not None else None,
                )
                metas = list(dispatcher.last_meta)
                if budget is not None and (detail := budget_check(metas)):
                    raise BudgetExceeded(detail)
                update = read_output(name, raw, state)
                if name == "review" and diff_line_map is not None and update.get("review_comments"):
                    validated = validate_review_comments(
                        update["review_comments"],
                        diff_line_map,
                        diff_content_map,
                        diff_hunk_map=diff_hunk_map,
                    )
                    update["review_comments"] = list(validated.comments)
                    if validated.errors:
                        update["review_validation_errors"] = list(validated.errors)
                update["attempts"] = recorded
                if metas:
                    update["usage_records"] = metas
                    last_sid = next((m.get("session_id") for m in reversed(metas) if m.get("session_id")), "")
                    if last_sid:
                        update["sessions"] = {**(state.get("sessions") or {}), name: last_sid}
                    if budget is not None:
                        soft_usd = float(budget.get("soft_usd") or 0)
                        if soft_usd > 0:
                            combined = [*prior_usage, *metas]
                            after = state_usage_summary(combined, pricing_data)
                            if after["cost_usd"] >= soft_usd:
                                update["budget_warning"] = (
                                    f"run spend reached ${after['cost_usd']:.2f} (soft limit ${soft_usd:.2f})"
                                )
                if name == "implement" and commit_worktree is not None:
                    request = state.get("request")
                    title = request.strip().splitlines()[0] if isinstance(request, str) and request.strip() else ""
                    try:
                        update["head_sha"] = commit_worktree(title)
                        update["head_checked"] = False
                    except InvalidOutput as exc:
                        if recorded and recorded[-1]["status"] == "ran":
                            recorded[-1] = {
                                "node": name,
                                "model": recorded[-1]["model"],
                                "status": "stopped",
                                "detail": str(exc),
                            }
                        halted = {
                            "path": [name], "attempts": recorded,
                            "halt": "invalid_output", "halt_detail": str(exc),
                            "failure_phase": "git", "failure_reason": "invalid_output",
                        }
                        if metas:
                            halted["usage_records"] = metas
                        model = raw.get("model") if isinstance(raw, dict) else None
                        if isinstance(model, str) and model:
                            halted["models_used"] = {**(state.get("models_used") or {}), name: model}
                        return halted
                elif name == "implement" and inspect_worktree is not None:
                    update.update(head_sha=None, head_checked=False)
                return update
            except BudgetExceeded as exc:
                return {**_budget_halt(name, str(exc)), "attempts": recorded, "usage_records": list(dispatcher.last_meta)}
            except InvalidOutput as exc:
                if recorded and recorded[-1]["status"] == "ran":
                    recorded[-1] = {
                        "node": name,
                        "model": recorded[-1]["model"],
                        "status": "stopped",
                        "detail": str(exc),
                    }
                halted = {
                    "path": [name], "attempts": recorded, "halt": "invalid_output", "halt_detail": str(exc),
                    "failure_phase": "harness", "failure_reason": "invalid_output",
                }
                metas = list(getattr(dispatcher, "last_meta", None) or [])
                if metas:
                    halted["usage_records"] = metas
                model = raw.get("model") if isinstance(raw, dict) else (recorded[-1]["model"] if recorded else None)
                if isinstance(model, str) and model:
                    halted["models_used"] = {**(state.get("models_used") or {}), name: model}
                return halted
            except QuotaExhausted as exc:
                metas = list(getattr(dispatcher, "last_meta", None) or [])
                base_halt: dict = {"path": [name], "attempts": recorded, "failure_phase": "harness"}
                if metas:
                    base_halt["usage_records"] = metas
                if name == "review":
                    return {
                        **base_halt,
                        "halt": "no_reviewer",
                        "halt_detail": f"review quota exhausted: {exc}",
                        "failure_reason": "no_reviewer",
                    }
                return {
                    **base_halt,
                    "halt": "quota_exhausted",
                    "halt_detail": f"{name} quota exhausted: {exc}",
                    "failure_reason": "quota_exhausted",
                }
            except CliMissing as exc:
                base_halt = {"path": [name], "attempts": recorded, "failure_phase": "setup"}
                if name == "review":
                    return {
                        **base_halt,
                        "halt": "no_reviewer",
                        "halt_detail": f"review CLI missing: {exc}",
                        "failure_reason": "cli_missing",
                    }
                return {
                    **base_halt,
                    "halt": "cli_missing",
                    "halt_detail": str(exc),
                    "failure_reason": "cli_missing",
                }

        return node

    def verify(state: DeliveryState) -> dict:
        assert run_check is not None
        checked = {}
        if inspect_worktree is not None:
            head, clean = inspect_worktree()
            if not clean or not FULL_SHA.fullmatch(head):
                return {
                    "path": ["verify"],
                    "halt": "dirty_worktree",
                    "failure_phase": "verify",
                    "failure_reason": "dirty_worktree",
                }
            checked = {"head_sha": head, "head_checked": True}
        outcome = run_check(state["check"])
        if isinstance(outcome, tuple):
            exit_code = int(outcome[0])
            raw_output = str(outcome[1] or "")
        else:
            exit_code = int(outcome)
            raw_output = ""
        check_output = redact_text(raw_output)[-4000:] if raw_output else ""
        recorded = {
            "check_exit_code": exit_code,
            "check_output": check_output if exit_code != 0 else "",
            "check_exits": [exit_code],
            "proof_labels": {},
        }
        if exit_code in UNAVAILABLE_CHECK:
            return {
                "path": ["verify"],
                "halt": "check_unavailable",
                "halt_detail": (
                    f"the check exited {exit_code} because a program it runs {UNAVAILABLE_CHECK[exit_code]}"
                ),
                "failure_phase": "verify",
                "failure_reason": "check_unavailable",
                "verify": "fail",
                **recorded,
            }
        if exit_code != 0 and check_output and ENV_FAILURE_PATTERNS.search(check_output):
            detail = " ".join(check_output.splitlines()[-4:]).strip()[:240]
            return {
                "path": ["verify"],
                "halt": "env_unavailable",
                "halt_detail": redact_text(f"the check hit an environment error (exit {exit_code}): {detail}", redact_pii=True),
                "failure_phase": "verify",
                "failure_reason": "env_unavailable",
                "verify": "fail",
                **recorded,
            }
        verdict = "pass" if exit_code == 0 else "fail"
        return {
            **read_output("verify", {"verify": verdict}, state),
            **recorded,
            **(checked if exit_code == 0 else {}),
        }

    def prove(state: DeliveryState) -> dict:
        slice = state["slice"]
        labels = {
            "browser": browser_proof_label(slice, state.get("browser_proof")),
            "formal": formal_proof_label(slice, state.get("formal_proof")),
        }
        update = {"path": ["proof"], "proof_labels": {key: label for key, label in labels.items() if label}}
        if "insufficient_evidence" in labels.values():
            update["halt"] = "insufficient_evidence"
            update["failure_phase"] = "proof"
            update["failure_reason"] = "insufficient_evidence"
        elif "block" in labels.values():
            update["review_rounds"] = state.get("review_rounds", 0) + 1
            if update["review_rounds"] >= config.max_review_rounds:
                update["halt"] = "review_cap"
                update["failure_phase"] = "proof"
                update["failure_reason"] = "review_cap"
        return update

    def needs_human(state: DeliveryState) -> dict:
        stage = state["path"][-1]
        question = state.get("human_question") or "What should Foreman know to continue?"
        answer = interrupt({"stage": stage, "question": question})
        return {"human_answers": [*state.get("human_answers", []), answer]}

    def after_triage(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        decision = state.get("triage")
        if decision == "needs_human":
            return "needs_human" if checkpointer is not None else "done"
        if decision == "implement":
            return "implement"
        if state.get("issue_kind") == "parent":
            return "spec"
        if decision == "question":
            return "research"
        return decision or "done"

    def after_spec(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("spec_gate") == "needs_human" and checkpointer is not None:
            return "needs_human"
        if state.get("issue_kind") == "parent":
            return "done"
        return "implement" if state.get("spec_gate") == "ready" else "done"

    def after_review(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("review") == "approve":
            return "merge"
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_verify(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        if state.get("verify") == "pass":
            slice = state.get("slice")
            if slice and (slice["screen"] or slice["properties"]):
                return "proof"
            return "review"
        if state.get("verify_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_proof(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        return "implement" if state.get("proof_labels") else "review"

    def after_implement(state: DeliveryState) -> str:
        return "done" if state.get("halt") else "verify"

    def arm_merge(state: DeliveryState) -> dict:
        if detail := _budget_exceeded_detail(state.get("usage_records") or [], budget, pricing_data):
            return {**_budget_halt("merge", detail), "merge_armed": False}
        head = state.get("head_sha")
        args = ["gh", "pr", "merge", "--auto", "--match-head-commit", head] if head and FULL_SHA.fullmatch(head) else None
        if read_merge_hold is not None:
            hold = read_merge_hold(state["repo"])
            if isinstance(hold, str) and hold.strip():
                halted: dict = {
                    "merge_armed": False,
                    "halt": "merge_held",
                    "halt_detail": hold.strip(),
                    "path": ["merge"],
                }
                if args is not None:
                    halted["merge_args"] = args
                return halted
        if args is None:
            return {"merge_armed": False, "path": ["merge"]}
        if inspect_worktree is not None:
            current, clean = inspect_worktree()
            if not state.get("head_checked") or not clean or current != head:
                return {"merge_armed": False, "merge_args": args, "path": ["merge"]}
        if publish_worktree is not None:
            try:
                published = publish_worktree(head)
            except InvalidOutput as exc:
                return {
                    "merge_armed": False, "merge_args": args, "path": ["merge"],
                    "halt": "unpublished", "halt_detail": str(exc),
                }
            if not isinstance(published, str) or not published.strip():
                return {
                    "merge_armed": False, "merge_args": args, "path": ["merge"],
                    "halt": "unpublished", "halt_detail": "could not open the pull request",
                }
            published = published.strip()
            if record_review is not None:
                try:
                    record_review(state, published)
                except InvalidOutput as exc:
                    return {
                        "merge_armed": False, "merge_args": args, "path": ["merge"],
                        "pull_request": published, "halt": "review_unrecorded", "halt_detail": str(exc),
                    }
        else:
            published = None
        armed = run_gh(args) == 0 if run_gh is not None else run_check is None
        result = {"merge_armed": armed, "merge_args": args, "path": ["merge"]}
        if published:
            result["pull_request"] = published
        if run_gh is not None and not armed:
            result["halt"] = "unpublished"
            result["halt_detail"] = (
                "merge was not armed for the open pull request"
                if published
                else "the reviewed commit has no open pull request"
            )
        return result

    def finish(state: DeliveryState) -> dict:
        stop = stop_reason(state)
        out: dict = {"stop": stop, "proposal": watch_run(state, stop), "path": ["done"]}
        if not state.get("failure_reason") and stop in {"verify_cap", "review_cap"}:
            out["failure_phase"] = "verify" if stop == "verify_cap" else "review"
            out["failure_reason"] = stop
        return out

    builder = StateGraph(DeliveryState)
    for name in REQUIRED_NODES:
        builder.add_node(name, verify if name == "verify" and run_check is not None else bind(name))
    builder.add_node("proof", prove)
    builder.add_node("needs_human", needs_human)
    builder.add_node("merge", arm_merge)
    builder.add_node("done", finish)
    builder.add_conditional_edges(
        START, lambda state: "done" if state.get("halt") else "triage", {"done": "done", "triage": "triage"},
    )
    builder.add_conditional_edges(
        "triage",
        after_triage,
        {"implement": "implement", "spec": "spec", "research": "research",
         "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges(
        "spec", after_spec, {"implement": "implement", "needs_human": "needs_human", "done": "done"},
    )
    builder.add_conditional_edges(
        "needs_human", lambda state: state["path"][-1], {"triage": "triage", "spec": "spec"},
    )
    builder.add_edge("research", "done")
    builder.add_conditional_edges("implement", after_implement, {"verify": "verify", "done": "done"})
    builder.add_conditional_edges(
        "review",
        after_review,
        {"merge": "merge", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges(
        "verify",
        after_verify,
        {"proof": "proof", "review": "review", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges(
        "proof", after_proof, {"review": "review", "implement": "implement", "done": "done"},
    )
    builder.add_edge("merge", "done")
    builder.add_edge("done", END)
    return builder.compile(checkpointer=checkpointer)


def run_delivery(
    dispatcher: FailoverDispatcher,
    *,
    repo: str,
    request: str,
    check: str = "",
    config: Config | None = None,
    issue_kind: str = "slice",
    read_merge_hold: Callable[[str], str | None] | None = None,
    budget: dict | None = None,
) -> dict:
    if issue_kind not in {"parent", "slice"}:
        raise ValueError(f"unknown issue kind {issue_kind}")
    graph = compile_delivery(dispatcher, config, read_merge_hold=read_merge_hold, budget=budget)
    return graph.invoke(
        {
            "repo": repo,
            "request": request,
            "check": check,
            "issue_kind": issue_kind,
            "verify_rounds": 0,
            "review_rounds": 0,
            "models_used": {},
            "sessions": {},
            "usage_records": [],
            "attempts": [],
            "path": [],
        }
    )


def diagram(dispatcher: FailoverDispatcher | None = None, config: Config | None = None) -> str:
    if dispatcher is None:
        dispatcher = FailoverDispatcher(lambda *args: {"triage": "implement"})
    graph = compile_delivery(dispatcher, config)
    return graph.get_graph().draw_mermaid()
