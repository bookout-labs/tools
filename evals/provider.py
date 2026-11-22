"""Promptfoo provider. It walks the delivery graph without calling a model."""

from collections.abc import Mapping
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from foreman.readiness import MergeState  # noqa: E402
from foreman.adapters import Outcome  # noqa: E402
from foreman.config import ModelSpec  # noqa: E402
from foreman.delivery import DeliveryState, run_delivery  # noqa: E402
from foreman.dispatch import Handler  # noqa: E402
from foreman.dry_run import ScriptHandler, SCRIPTED_USAGE  # noqa: E402
from tests.skill_fixture import skill_fixture  # noqa: E402


FIELD_SLICE: dict[str, object] = {
    "title": "Add a field",
    "acceptance": "The response includes the new field.",
    "builds": [],
    "uses": [],
    "screen": "",
    "properties": [],
    "stateful": False,
}


def _walk(handler: Handler, *, slice: dict[str, object] | None = None) -> DeliveryState:
    with skill_fixture() as (config, scratch):
        return run_delivery(
            handler, repo="DoctorCRE/doctorcre-engine", request="Add a field",
            check="npm run check", slice=slice, config=config, scratch=scratch,
        )


def _stop(handler: Handler, *, slice: dict[str, object] | None = None) -> str:
    return _walk(handler, slice=slice)["stop"]


def _with_quota_gap() -> str:
    handler = ScriptHandler()

    def exhaust_claude(node: str, model: ModelSpec | None, state: DeliveryState, instruction: str) -> Outcome:
        if node == "triage" and model and model.name == "claude-opus-5-5-high":
            return Outcome("quota", usage=SCRIPTED_USAGE)
        return handler(node, model, state, instruction)

    result = _walk(exhaust_claude, slice=FIELD_SLICE)
    return result["stop"]


def _review_fallback() -> str:
    handler = ScriptHandler()
    offered: list[str] = []

    def review(node: str, model: ModelSpec | None, state: DeliveryState, instruction: str) -> Outcome:
        if node == "review" and model:
            offered.append(model.name)
            if model.family == "claude":
                return Outcome("quota", usage=SCRIPTED_USAGE)
        return handler(node, model, state, instruction)

    result = _walk(review, slice=FIELD_SLICE)
    selection = result["review"].review_findings[-1]["provenance"]
    return f"{result['stop']} {','.join(offered)} rounds={len(result['review'].review_findings)} same_family={selection['same_family']} reason={selection['reason']}"


def _settled_review() -> str:
    handler = ScriptHandler(reviews=["changes", "approve", "approve"])
    count = 0

    def review(node, model, state, instruction):
        nonlocal count
        reply = handler(node, model, state, instruction)
        if node == "review":
            count += 1
            finding = {"id": "check" if count == 1 else "next", "kind": "docs", "fix": "Add a check",
                       "ruling_id": None, "file": "items.txt", "line": 1, "severity": "nit", "summary": "Add a check"}
            if count == 3:
                finding.update(id="repeat", ruling_id="finding:check")
            return Outcome("ran", {**reply.output, "findings": [finding],
                                   "resolutions": [] if count == 1 else [{"id": "check" if count == 2 else "next", "status": "resolved"}]}, usage=reply.usage)
        return reply

    result = _walk(review, slice=FIELD_SLICE)
    return f"{result['stop']} {result.get('merge', MergeState()).status} {result['review'].detail}"


def call_api(prompt: str, options: object, context: Mapping[str, Mapping[str, object]]) -> dict[str, str]:
    case = (context.get("vars") or {}).get("case") or prompt
    if case == "clear":
        output = _stop(ScriptHandler(triage="implement"), slice=FIELD_SLICE)
    elif case == "review-converges":
        output = _settled_review()
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
                        "builds": [{"name": "deals-board", "exists": False, "path": None}],
                        "uses": [],
                        "screen": "docs/screens/deals.md",
                        "properties": [{"name": "Deal external IDs are unique", "constraint": "deals_external_id_key"}],
                        "stateful": False,
                    },
                    {
                        "title": "Board saves a move",
                        "acceptance": "A move is stored.",
                        "builds": [],
                        "uses": [{"name": "deals-board", "exists": False, "path": None}],
                        "screen": "",
                        "properties": [],
                        "stateful": False,
                    },
                ],
            ),
        )
    elif case == "question":
        output = _stop(ScriptHandler(triage="question"), slice=FIELD_SLICE)
    elif case == "review-fallback":
        output = _review_fallback()
    elif case == "failover":
        output = _with_quota_gap()
    else:
        return {"error": f"unknown case {case}"}
    return {"output": output}
