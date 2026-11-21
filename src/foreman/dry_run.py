"""A handler that walks the graph without calling a model."""

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
    ):
        self.triage = triage
        self.spec_gate = spec_gate
        self.reviews = list(reviews if reviews is not None else ["approve"])
        self.verify = verify
        self.spec = spec
        self.slices = slices
        self.slice = slice if slice is not None else {
            "title": "Implement the requested change",
            "acceptance": "The requested change passes verification.",
            "depends_on": [],
            "screen": "",
            "properties": [],
        }
        self.implementation = implementation
        self.head_sha = head_sha
        self.research = research
        self.risk = dict(DEFAULT_RISK if risk is None else risk)
        self.calls: list[dict[str, str | None]] = []

    def __call__(self, node: str, model: str | None, state: dict, instruction: str) -> dict:
        self.calls.append({"node": node, "model": model, "instruction": instruction})
        if node == "triage":
            if self.triage == "implement" and state["issue_kind"] == "parent":
                return {"triage": self.triage, "slice": self.slice}
            return {"triage": self.triage}
        if node == "spec":
            payload = {"spec": self.spec, "spec_gate": self.spec_gate}
            if self.slices is not None:
                payload["slices"] = self.slices
            return payload
        if node == "implement":
            return {"implementation": self.implementation, "head_sha": self.head_sha}
        if node == "research":
            return {"research": self.research}
        if node == "review":
            verdict = self.reviews.pop(0) if self.reviews else "changes"
            return {"review": verdict, "risk": dict(self.risk), "findings": [], "detail": "Scripted review."}
        if node == "verify":
            return {"verify": self.verify}
        raise ValueError(f"no script for {node}")
