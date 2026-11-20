"""The delivery graph. Edges live here. Models live in models.toml."""

from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Callable, TypedDict
import operator
import re

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from foreman.config import Config, ConfigError, family, load_config, order_for
from foreman.dispatch import FailoverDispatcher, InvalidOutput, QuotaExhausted
from foreman.proof import browser_proof_label, formal_proof_label
from foreman.router import choose_order

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
    request: str
    repo: str
    issue: int
    workspace: str
    check: str
    check_exit_code: int
    triage: str
    spec: str
    spec_gate: str
    slices: list[Slice]
    child_issue_numbers: list[int]
    slice: Slice
    issue_kind: str
    research: str
    implementation: str
    head_sha: str | None
    head_checked: bool
    merge_armed: bool
    merge_args: list[str]
    browser_proof: dict | None
    formal_proof: dict | None
    proof_labels: dict[str, str]
    human_question: str
    human_answers: list[str]
    review: str
    risk: dict[str, str]
    verify: str
    review_rounds: int
    models_used: dict[str, str]
    implement_models: list[str]
    model_choices: Annotated[list[dict], operator.add]
    labels: list[str]
    files: list[str]
    halt: str
    halt_detail: str
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


def choice(value: object, options: set[str], field: str) -> str:
    if not isinstance(value, str) or value not in options:
        raise InvalidOutput(f"{field} must be one of {', '.join(sorted(options))}")
    return value


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
        update["spec"] = required_text(raw.get("spec"), "spec did not return text")
        update["spec_gate"] = gate
        if state.get("issue_kind") == "parent" and gate == "ready":
            update["slices"] = clean_slices(raw.get("slices"))
    elif name in TEXT_FIELDS:
        key = TEXT_FIELDS[name]
        update[key] = required_text(raw.get(key), f"{name} did not return text")
        if name == "implement":
            head = raw.get("head_sha")
            update["head_sha"] = (head.strip() or None) if isinstance(head, str) else None
            for field in ("browser_proof", "formal_proof"):
                artifact = raw.get(field)
                update[field] = artifact if isinstance(artifact, dict) else None
    elif name == "verify":
        verdict = choice(raw.get("verify"), VERIFY_OUTCOMES, "verify")
        update["verify"] = verdict
        if verdict == "fail":
            update["review_rounds"] = state.get("review_rounds", 0) + 1
    elif name == "review":
        verdict = choice(raw.get("review"), REVIEW_OUTCOMES, "review")
        update["review"] = verdict
        update["risk"] = clean_risk(raw.get("risk"))
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


def compile_delivery(
    dispatcher: FailoverDispatcher | None,
    config: Config | None = None,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    run_check: Callable[[str], int] | None = None,
    run_gh: Callable[[list[str]], int] | None = None,
    inspect_worktree: Callable[[], tuple[str, bool]] | None = None,
    resolve: Callable[[str, str], str] | None = None,
):
    config = config or load_config()
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}
    identity = lambda route, _effort: route

    def bind(name: str):
        def node(state: DeliveryState) -> dict:
            if dispatcher is None:
                return {"path": [name], "halt": "no_dispatcher"}
            try:
                models = order_for(name, config, state.get("models_used") or {}, state.get("implement_models"))
            except ConfigError as exc:
                return {"path": [name], "halt": "no_reviewer", "halt_detail": str(exc)}
            ordered, reason, inputs = choose_order(name, models, state)
            try:
                resolved = [(resolve or identity)(route, config.effort) for route in ordered]
            except ConfigError as exc:
                return {"path": [name], "halt": "no_reviewer", "halt_detail": str(exc)}
            instruction = prompts[name]
            if name == "implement" and (slice := state.get("slice")):
                for field, skill in (("screen", "browser-proof"), ("properties", "formal-proof")):
                    if slice[field]:
                        proof = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
                        instruction = f"{instruction}\n\n{proof}"
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction}\n\nCommand: {command}"
            call_state = dict(state)
            call_state["effort"] = config.effort
            try:
                raw = dispatcher.run(name, resolved, call_state, instruction)
                update = read_output(name, raw, state)
                if name == "implement" and inspect_worktree is not None:
                    update.update(head_sha=None, head_checked=False)
                used = raw.get("model")
                if used:
                    route = ordered[0] if ordered else used
                    for route_name, slug in zip(ordered, resolved):
                        if slug == used:
                            route = route_name
                            break
                    record = {
                        "node": name,
                        "inputs": inputs,
                        "route": route,
                        "family": family(used),
                        "model": used,
                        "reason": reason if not resolved or used == resolved[0] else "quota fallback",
                        "outcome": "ran",
                    }
                    usage = raw.get("usage")
                    if isinstance(usage, dict):
                        record["tokens"] = usage
                    update["model_choices"] = [record]
                return update
            except InvalidOutput as exc:
                return {"path": [name], "halt": "invalid_output", "halt_detail": str(exc)}
            except QuotaExhausted as exc:
                if name != "review":
                    raise
                return {"path": [name], "halt": "no_reviewer", "halt_detail": f"review quota exhausted: {exc}"}

        return node

    def verify(state: DeliveryState) -> dict:
        assert run_check is not None
        checked = {}
        if inspect_worktree is not None:
            head, clean = inspect_worktree()
            if not clean or not FULL_SHA.fullmatch(head):
                return {"path": ["verify"], "halt": "dirty_worktree"}
            checked = {"head_sha": head, "head_checked": True}
        exit_code = run_check(state["check"])
        verdict = "pass" if exit_code == 0 else "fail"
        return {
            **read_output("verify", {"verify": verdict}, state),
            "check_exit_code": exit_code,
            "proof_labels": {},
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
        elif "block" in labels.values():
            update["review_rounds"] = state.get("review_rounds", 0) + 1
            if update["review_rounds"] >= config.max_review_rounds:
                update["halt"] = "review_cap"
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
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_proof(state: DeliveryState) -> str:
        if state.get("halt"):
            return "done"
        return "implement" if state.get("proof_labels") else "review"

    def after_implement(state: DeliveryState) -> str:
        return "done" if state.get("halt") else "verify"

    def arm_merge(state: DeliveryState) -> dict:
        head = state.get("head_sha")
        if not head or not FULL_SHA.fullmatch(head):
            return {"merge_armed": False, "path": ["merge"]}
        args = ["gh", "pr", "merge", "--auto", "--match-head-commit", head]
        if inspect_worktree is not None:
            current, clean = inspect_worktree()
            if not state.get("head_checked") or not clean or current != head:
                return {"merge_armed": False, "merge_args": args, "path": ["merge"]}
        armed = run_gh(args) == 0 if run_gh is not None else run_check is None
        return {"merge_armed": armed, "merge_args": args, "path": ["merge"]}

    def finish(state: DeliveryState) -> dict:
        return {"stop": stop_reason(state), "path": ["done"]}

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
    labels: list[str] | None = None,
    files: list[str] | None = None,
    resolve: Callable[[str, str], str] | None = None,
) -> dict:
    if issue_kind not in {"parent", "slice"}:
        raise ValueError(f"unknown issue kind {issue_kind}")
    graph = compile_delivery(dispatcher, config, resolve=resolve)
    initial: dict = {
        "repo": repo,
        "request": request,
        "check": check,
        "issue_kind": issue_kind,
        "review_rounds": 0,
        "models_used": {},
        "model_choices": [],
        "path": [],
    }
    if labels is not None:
        initial["labels"] = list(labels)
    if files is not None:
        initial["files"] = list(files)
    return graph.invoke(initial)


def diagram(dispatcher: FailoverDispatcher | None = None, config: Config | None = None) -> str:
    if dispatcher is None:
        dispatcher = FailoverDispatcher(lambda *args: {"triage": "implement"})
    graph = compile_delivery(dispatcher, config)
    return graph.get_graph().draw_mermaid()
