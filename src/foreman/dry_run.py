"""A handler that walks the graph without calling a model."""


class ScriptHandler:
    def __init__(
        self,
        intake: str = "ready",
        reviews: list[str] | None = None,
        verify: str = "pass",
        spec: str = "spec",
        implementation: str = "diff",
    ):
        self.intake = intake
        self.reviews = list(reviews if reviews is not None else ["approve"])
        self.verify = verify
        self.spec = spec
        self.implementation = implementation
        self.calls: list[dict[str, str | None]] = []

    def __call__(self, node: str, model: str | None, state: dict, instruction: str) -> dict:
        self.calls.append({"node": node, "model": model, "instruction": instruction})
        if node == "intake":
            return {"intake": self.intake}
        if node == "spec":
            return {"spec": self.spec}
        if node == "implement":
            return {"implementation": self.implementation}
        if node == "review":
            verdict = self.reviews.pop(0) if self.reviews else "changes"
            return {"review": verdict}
        if node == "verify":
            return {"verify": self.verify}
        raise ValueError(f"no script for {node}")
