"""Promptfoo provider. It walks the delivery graph without calling a model."""

from collections.abc import Mapping
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from foreman.adapters import Outcome  # noqa: E402
from foreman.config import ModelSpec  # noqa: E402
from foreman.delivery import DeliveryState, run_delivery  # noqa: E402
from foreman.dispatch import FailoverDispatcher, Handler  # noqa: E402
from foreman.dry_run import ScriptHandler  # noqa: E402
from foreman.measure import grade, propose, selected  # noqa: E402


FIELD_SLICE: dict[str, object] = {
    "title": "Add a field",
    "acceptance": "The response includes the new field.",
    "depends_on": [],
    "screen": "",
    "properties": [],
    "stateful": False,
}


def _stop(handler: Handler, *, slice: dict[str, object] | None = None) -> str:
    result = run_delivery(
        FailoverDispatcher(handler),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check="npm run check",
        slice=slice,
    )
    return result["stop"]


def _with_quota_gap() -> str:
    handler = ScriptHandler()

    def exhaust_claude(node: str, model: ModelSpec | None, state: DeliveryState, instruction: str) -> Outcome:
        if model and model.name == "claude-opus-5-5-high":
            return Outcome("quota")
        return handler(node, model, state, instruction)

    result = run_delivery(
        FailoverDispatcher(exhaust_claude),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check="npm run check",
        slice=FIELD_SLICE,
    )
    models = result.get("models_used", {})
    implement = models["implement"]
    review = models["review"]
    if implement.split("-", 1)[0] == review.split("-", 1)[0]:
        return "same-family"
    return result["stop"]


def call_api(prompt: str, options: object, context: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    case = (context.get("vars") or {}).get("case") or prompt
    if case == "clear":
        output = _stop(ScriptHandler(triage="implement"), slice=FIELD_SLICE)
    elif case == "spec-ready":
        output = _stop(ScriptHandler(triage="spec", spec_gate="ready"), slice=FIELD_SLICE)
    elif case == "spec-gate":
        output = _stop(ScriptHandler(triage="spec", spec_gate="needs_human"), slice=FIELD_SLICE)
    elif case == "parent":
        output = _stop(ScriptHandler(triage="implement", slice=FIELD_SLICE))
    elif case == "parent-spec":
        output = _stop(
            ScriptHandler(
                triage="spec",
                spec_gate="ready",
                slices=[
                    {
                        "title": "Board lists the deals",
                        "acceptance": "The board shows the deals.",
                        "depends_on": [],
                        "screen": "docs/screens/deals.md",
                        "properties": [{"name": "Deal external IDs are unique", "constraint": "deals_external_id_key"}],
                        "stateful": False,
                    },
                    {
                        "title": "Board saves a move",
                        "acceptance": "A move is stored.",
                        "depends_on": ["Board lists the deals"],
                        "screen": "",
                        "properties": [],
                        "stateful": False,
                    },
                ],
            ),
        )
    elif case == "question":
        output = _stop(ScriptHandler(triage="question"), slice=FIELD_SLICE)
    elif case == "failover":
        output = _with_quota_gap()
    elif case == "score-approve":
        graded = grade("approve")
        output = "pass" if graded.passed and graded.score == 1.0 else "fail"
    elif case == "score-block":
        graded = grade("block")
        output = "fail" if not graded.passed and graded.score == 0.2 else "pass"
    elif case == "score-thin":
        graded = grade("insufficient_evidence")
        output = "insufficient_evidence" if not graded.passed and graded.score == 0.5 else "fail"
    elif case == "sample":
        output = "yes" if selected(0) and selected(10) and not selected(1) else "no"
    elif case == "proposal":
        output = propose("redundant-tests")
    else:
        return {"error": f"unknown case {case}"}
    return {"output": output}
