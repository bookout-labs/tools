"""The delivery graph. Edges live here. Models live in models.toml."""

from typing import Annotated, TypedDict
import operator

from langgraph.graph import END, START, StateGraph

from foreman.config import Config, load_config, order_for
from foreman.dispatch import FailoverDispatcher

GRAPH_NAME = "delivery"
REQUIRED_NODES = ("intake", "spec", "implement", "review", "verify")
INSTRUCTIONS = {
    "intake": (
        "Read the request. Reply ready or needs_human. "
        "The model list is config outside this node. Do not edit it."
    ),
    "spec": (
        "Write the spec for the request. "
        "The model list is config outside this node. Do not edit it."
    ),
    "implement": (
        "Implement the spec in the repository. "
        "The model list is config outside this node. Do not edit it."
    ),
    "review": (
        "Review the implementation. Reply approve or changes. "
        "The model list is config outside this node. Do not edit it."
    ),
    "verify": "Run the repository check. Reply pass or fail.",
}
OUTCOMES = {
    "intake": {"ready", "needs_human"},
    "review": {"approve", "changes"},
    "verify": {"pass", "fail"},
}
TEXT_NODES = {"spec": "spec", "implement": "implementation"}


class DeliveryState(TypedDict, total=False):
    request: str
    repo: str
    check: str
    intake: str
    spec: str
    implementation: str
    review: str
    verify: str
    review_rounds: int
    models_used: dict[str, str]
    stop: str
    path: Annotated[list[str], operator.add]


def stop_reason(state: dict) -> str:
    if state.get("intake") == "needs_human":
        return "needs_human"
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

    def bind(name: str):
        def node(state: DeliveryState) -> dict:
            models = order_for(name, config, state.get("models_used") or {})
            instruction = INSTRUCTIONS[name]
            if name == "verify":
                command = state.get("check") or "(none)"
                instruction = f"{instruction} Command: {command}"
            raw = dispatcher.run(name, models, state, instruction)
            key = TEXT_NODES.get(name, name)
            value = raw.get(key)
            if name in OUTCOMES and value not in OUTCOMES[name]:
                raise ValueError(f"{name} returned {value!r}")
            if name in TEXT_NODES and not isinstance(value, str):
                raise ValueError(f"{name} did not return text")
            update: dict = {key: value, "path": [name]}
            model = raw.get("model")
            if model:
                used = dict(state.get("models_used") or {})
                used[name] = model
                update["models_used"] = used
            # The edge below reads the cap from config. A node cannot raise it.
            rounds = state.get("review_rounds", 0)
            if name == "review" and value == "changes":
                update["review_rounds"] = rounds + 1
            if name == "verify" and value == "fail":
                update["review_rounds"] = rounds + 1
            return update

        return node

    def after_intake(state: DeliveryState) -> str:
        return "spec" if state.get("intake") == "ready" else "done"

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
    builder.add_edge(START, "intake")
    builder.add_conditional_edges("intake", after_intake, {"spec": "spec", "done": "done"})
    builder.add_edge("spec", "implement")
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
        dispatcher = FailoverDispatcher(lambda *args: {"intake": "ready"})
    graph = compile_delivery(dispatcher, config)
    return graph.get_graph().draw_mermaid()
