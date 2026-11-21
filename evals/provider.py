"""Promptfoo provider. It walks the delivery graph without calling a model."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from foreman.delivery import run_delivery  # noqa: E402
from foreman.dispatch import FailoverDispatcher, QuotaExhausted  # noqa: E402
from foreman.dry_run import ScriptHandler  # noqa: E402
from foreman.measure import grade, propose, selected  # noqa: E402


FIELD_SLICE = {
    "title": "Add a field",
    "acceptance": "The response includes the new field.",
    "depends_on": [],
    "screen": "",
    "properties": [],
}


def _stop(handler, *, slice=None) -> str:
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

    def exhaust_claude(node, model, state, instruction):
        if model == "claude-opus-5-5-high":
            raise QuotaExhausted(model)
        return handler(node, model, state, instruction)

    result = run_delivery(
        FailoverDispatcher(exhaust_claude),
        repo="DoctorCRE/doctorcre-engine",
        request="Add a field",
        check="npm run check",
        slice=FIELD_SLICE,
    )
    implement = result["models_used"]["implement"]
    review = result["models_used"]["review"]
    if implement.split("-", 1)[0] == review.split("-", 1)[0]:
        return "same-family"
    return result["stop"]


def call_api(prompt, options, context):
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
                    },
                    {
                        "title": "Board saves a move",
                        "acceptance": "A move is stored.",
                        "depends_on": ["Board lists the deals"],
                        "screen": "",
                        "properties": [],
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
