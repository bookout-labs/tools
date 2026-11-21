"""A handler that walks the graph without calling a model."""

from collections.abc import Mapping

from foreman.adapters import Outcome
from foreman.config import ModelSpec
from foreman.stage_output import WorkKind

DEFAULT_RISK = {
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
        slices: list | None = None,
        implementation: str = "diff",
        research: str = "answer",
        risk: dict | None = None,
        head_sha: str | None = None,
        slice: dict | None = None,
        work_kind: WorkKind = "feature",
    ):
        self.triage = triage
        self.work_kind = work_kind
        self.spec_gate = spec_gate
        self.reviews = list(reviews if reviews is not None else ["approve"])
        self.verify = verify
        self.spec = spec
        self.slices = slices
        self.slice = slice if slice is not None else {
            "title": "Implement the requested change",
            "acceptance": "The requested change passes verification.",
            "builds": [],
            "uses": [],
            "screen": "",
            "properties": [],
            "stateful": False,
        }
        self.implementation = implementation
        self.head_sha = head_sha
        self.research = research
        self.risk = dict(DEFAULT_RISK if risk is None else risk)
        self.calls: list[dict[str, str | None]] = []

    def __call__(self, node: str, model: ModelSpec | None, state: Mapping[str, object], instruction: str) -> Outcome:
        self.calls.append({"node": node, "model": model.name if model else None, "instruction": instruction})
        if node == "triage":
            return Outcome("ran", {
                "triage": self.triage, "work_kind": self.work_kind, "human_question": None,
                "slice": self.slice if self.triage == "implement" and state["issue_kind"] == "parent" else None,
            })
        if node == "spec":
            return Outcome("ran", {
                "spec": self.spec, "spec_gate": self.spec_gate, "slices": self.slices,
                "human_question": None,
            })
        if node == "implement":
            return Outcome("ran", {
                "implementation": self.implementation, "head_sha": self.head_sha,
                "browser_proof": None, "formal_proof": None,
            })
        if node == "research":
            return Outcome("ran", {"research": self.research})
        if node == "review":
            verdict = self.reviews.pop(0) if self.reviews else "changes"
            findings = (
                [{"file": "example.txt", "line": 1, "severity": "blocking", "summary": "Add a check."}]
                if verdict == "changes" else []
            )
            return Outcome("ran", {
                "review": verdict, "risk": dict(self.risk), "findings": findings, "detail": "Scripted review.",
            })
        if node == "verify":
            return Outcome("ran", {"verify": self.verify})
        raise ValueError(f"no script for {node}")
