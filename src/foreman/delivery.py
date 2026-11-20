"""The delivery graph. Edges live here. Models live in models.toml."""

from pathlib import Path
from typing import Annotated, TypedDict
import operator

from langgraph.graph import END, START, StateGraph

from foreman.config import Config, load_config, order_for
from foreman.dispatch import FailoverDispatcher

GRAPH_NAME = "delivery"
PACKAGE = Path(__file__).parent
REQUIRED_NODES = ("triage", "research", "spec", "implement", "review", "verify")
STAGE_ORDER = REQUIRED_NODES
SKILLS_FOR = {
    "implement": ("code-quality",),
    "review": ("merge-risk",),
}
TRIAGE_OUTCOMES = {"implement", "spec", "question", "needs_human"}
SPEC_GATES = {"ready", "needs_human"}
REVIEW_OUTCOMES = {"approve", "changes"}
VERIFY_OUTCOMES = {"pass", "fail"}
RISK_FIELDS = ("alignment", "impact", "verification", "rollback", "error_detection")
RISK_LEVELS = {"low", "medium", "high"}
TEXT_FIELDS = {"spec": "spec", "implement": "implementation", "research": "research"}


class DeliveryState(TypedDict, total=False):
    request: str
    repo: str
    check: str
    triage: str
    spec: str
    spec_gate: str
    research: str
    implementation: str
    review: str
    risk: dict[str, str]
    verify: str
    review_rounds: int
    models_used: dict[str, str]
    stop: str
    path: Annotated[list[str], operator.add]


def stage_text(name: str) -> str:
    path = PACKAGE / "stages" / f"{name}.md"
    text = path.read_text().strip()
    for skill in SKILLS_FOR.get(name, ()):
        skill_text = (PACKAGE / "skills" / f"{skill}.md").read_text().strip()
        text = f"{text}\n\n{skill_text}"
    return text


def clean_risk(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        raise ValueError("review did not report merge risk")
    risk: dict[str, str] = {}
    for field in RISK_FIELDS:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"review risk is missing {field}")
        risk[field] = value.strip()
    level = raw.get("level")
    if level not in RISK_LEVELS:
        raise ValueError("review risk level must be low, medium, or high")
    risk["level"] = level
    return risk


def stop_reason(state: dict) -> str:
    if state.get("triage") == "needs_human" or state.get("spec_gate") == "needs_human":
        return "needs_human"
    if state.get("triage") == "question":
        return "answered"
    if state.get("verify") == "pass":
        return "verified"
    if state.get("verify") == "fail":
        return "verify_cap"
    if state.get("review") == "changes":
        return "review_cap"
    return "stopped"


def compile_delivery(dispatcher: FailoverDispatcher, config: Config | None = None):
    config = config or load_config()
    missing = [name for name in REQUIRED_NODES if name not in config.nodes]
    if missing:
        raise ValueError(f"delivery config is missing {', '.join(missing)}")
    prompts = {name: stage_text(name) for name in REQUIRED_NODES}

    def bind(name: str):
        def node(state: DeliveryState) -> dict:
            models = order_for(name, config, state.get("models_used") or {})
            instruction = prompts[name]
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction}\n\nCommand: {command}"
            raw = dispatcher.run(name, models, state, instruction)
            update: dict = {"path": [name]}
            if name == "triage":
                value = raw.get("triage")
                if value not in TRIAGE_OUTCOMES:
                    raise ValueError(f"triage returned {value!r}")
                update["triage"] = value
            elif name == "spec":
                text = raw.get("spec")
                gate = raw.get("spec_gate")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("spec did not return text")
                if gate not in SPEC_GATES:
                    raise ValueError(f"spec returned {gate!r}")
                update["spec"] = text
                update["spec_gate"] = gate
            elif name in TEXT_FIELDS:
                key = TEXT_FIELDS[name]
                text = raw.get(key)
                if not isinstance(text, str) or not text.strip():
                    raise ValueError(f"{name} did not return text")
                update[key] = text
            elif name == "review":
                verdict = raw.get("review")
                if verdict not in REVIEW_OUTCOMES:
                    raise ValueError(f"review returned {verdict!r}")
                update["review"] = verdict
                update["risk"] = clean_risk(raw.get("risk"))
                if verdict == "changes":
                    update["review_rounds"] = state.get("review_rounds", 0) + 1
            elif name == "verify":
                verdict = raw.get("verify")
                if verdict not in VERIFY_OUTCOMES:
                    raise ValueError(f"verify returned {verdict!r}")
                update["verify"] = verdict
                if verdict == "fail":
                    update["review_rounds"] = state.get("review_rounds", 0) + 1
            else:
                raise ValueError(f"unknown node {name}")
            model = raw.get("model")
            if model:
                used = dict(state.get("models_used") or {})
                used[name] = model
                update["models_used"] = used
            return update

        return node

    def after_triage(state: DeliveryState) -> str:
        decision = state.get("triage")
        if decision == "needs_human":
            return "done"
        if decision == "question":
            return "research"
        return decision or "done"

    def after_spec(state: DeliveryState) -> str:
        return "implement" if state.get("spec_gate") == "ready" else "done"

    def after_review(state: DeliveryState) -> str:
        if state.get("review") == "approve":
            return "verify"
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def after_verify(state: DeliveryState) -> str:
        if state.get("verify") == "pass":
            return "done"
        if state.get("review_rounds", 0) < config.max_review_rounds:
            return "implement"
        return "done"

    def finish(state: DeliveryState) -> dict:
        return {"stop": stop_reason(state), "path": ["done"]}

    builder = StateGraph(DeliveryState)
    for name in REQUIRED_NODES:
        builder.add_node(name, bind(name))
    builder.add_node("done", finish)
    builder.add_edge(START, "triage")
    builder.add_conditional_edges(
        "triage",
        after_triage,
        {"implement": "implement", "spec": "spec", "research": "research", "done": "done"},
    )
    builder.add_conditional_edges("spec", after_spec, {"implement": "implement", "done": "done"})
    builder.add_edge("research", "done")
    builder.add_edge("implement", "review")
    builder.add_conditional_edges(
        "review",
        after_review,
        {"verify": "verify", "implement": "implement", "done": "done"},
    )
    builder.add_conditional_edges(
        "verify",
        after_verify,
        {"done": "done", "implement": "implement"},
    )
    builder.add_edge("done", END)
    return builder.compile()


def run_delivery(
    dispatcher: FailoverDispatcher,
    *,
    repo: str,
    request: str,
    check: str = "",
    config: Config | None = None,
) -> dict:
    graph = compile_delivery(dispatcher, config)
    return graph.invoke(
        {
            "repo": repo,
            "request": request,
            "check": check,
            "review_rounds": 0,
            "models_used": {},
            "path": [],
        }
    )


def diagram(dispatcher: FailoverDispatcher | None = None, config: Config | None = None) -> str:
    if dispatcher is None:
        dispatcher = FailoverDispatcher(lambda *args: {"triage": "implement"})
    graph = compile_delivery(dispatcher, config)
    return graph.get_graph().draw_mermaid()
