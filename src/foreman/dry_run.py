"""A handler that walks the graph without calling a model."""

from collections.abc import Mapping

from foreman.adapters import Outcome, Usage
from foreman.config import ModelSpec
from foreman.stage_output import MergeRiskRecord, SpecCheckOutput, WorkKind
from foreman.usd import BudgetUsd

SCRIPTED_USAGE = Usage(input_tokens=1, output_tokens=1, total_cost_usd=0.0)
CLEAR = SpecCheckOutput(findings=[], notes=[], resolutions=[])

DEFAULT_RISK: MergeRiskRecord = {
    "alignment": "The change matches the request.",
    "impact": "The change is limited to the requested surface.",
    "verification": "The engine check covers this change.",
    "rollback": "The change reverts in one commit.",
    "error_detection": "A failure of this change is visible in the engine check.",
    "level": "low",
}


class ScriptHandler:
    def __init__(
        self,
        triage: str = "implement",
        spec_gate: str = "ready",
        reviews: list[str] | None = None,
        verify: str = "pass",
        spec: str = "Behavior, decisions, and technical design.",
        slices: list[object] | None = None,
        implementation: str = "diff",
        research: str = "answer",
        risk: Mapping[str, object] | None = None,
        slice: Mapping[str, object] | None = None,
        work_kind: WorkKind = "feature",
    ):
        self.triage = triage
        self.work_kind = work_kind
        self.spec_gate = spec_gate
        self.reviews = list(reviews if reviews is not None else ["approve"])
        self.verify = verify
        self.spec = spec
        self.slices = slices
        self.slice: Mapping[str, object] = slice if slice is not None else {
            "title": "Implement the requested change",
            "acceptance": "The requested change passes verification.",
            "builds": [],
            "uses": [],
            "screen": "",
            "properties": [],
            "stateful": False,
        }
        self.implementation = implementation
        self.research = research
        self.risk = dict(DEFAULT_RISK if risk is None else risk)
        self.calls: list[dict[str, str | BudgetUsd | None]] = []

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str,
                 *, budget_usd: BudgetUsd | None = None) -> Outcome:
        self.calls.append({"node": node, "model": model.name if model else None, "instruction": instruction,
                           "budget_usd": budget_usd})
        if node == "triage":
            return Outcome("ran", {
                "triage": self.triage, "work_kind": self.work_kind, "human_question": None,
                "slice": self.slice if self.triage == "implement" and state["issue_kind"] == "parent" else None,
            }, usage=SCRIPTED_USAGE)
        if node == "spec":
            return Outcome("ran", {
                "spec": self.spec, "spec_gate": self.spec_gate, "slices": self.slices,
                "human_question": None,
            }, usage=SCRIPTED_USAGE)
        if node == "spec_check":
            return Outcome("ran", CLEAR.model_dump(), usage=SCRIPTED_USAGE)
        if node == "implement":
            return Outcome("ran", {
                "implementation": self.implementation,
                "browser_proof": None, "formal_proof": None,
            }, usage=SCRIPTED_USAGE)
        if node == "research":
            return Outcome("ran", {"research": self.research}, usage=SCRIPTED_USAGE)
        if node == "review":
            verdict = self.reviews.pop(0) if self.reviews else "changes"
            findings = (
                [{"id": "check", "kind": "untested-path", "fix": "Add a check.", "ruling_id": None, "file": "example.txt", "line": 1, "severity": "blocking", "summary": "Add a check."}]
                if verdict == "changes" and not state.get("review") else []
            )
            return Outcome("ran", {
                "resolutions": [{"id": "check", "status": "resolved" if verdict == "approve" else "unresolved"}] if state.get("review") else [], "risk": dict(self.risk), "findings": findings, "detail": "Scripted review.", "tool_facts": [],
            }, usage=SCRIPTED_USAGE)
        if node == "verify":
            return Outcome("ran", {"verify": self.verify}, usage=SCRIPTED_USAGE)
        raise ValueError(f"no script for {node}")
