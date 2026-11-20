"""Rule router. Quota exhaustion is the only reason to leave the chosen family."""

SMALL_CHARS = 400
SMALL_FILES = 3


def decision_inputs(state: dict) -> dict:
    request = state.get("request") if isinstance(state.get("request"), str) else ""
    files = state.get("files")
    labels = state.get("labels") if isinstance(state.get("labels"), list) else []
    risk = state.get("risk") if isinstance(state.get("risk"), dict) else {}
    rounds = state.get("review_rounds") or 0
    return {
        "request_chars": len(request),
        "file_count": len(files) if isinstance(files, list) else 0,
        "review_rounds": rounds,
        "risk_high": "risk:high" in labels or risk.get("level") == "high",
        "empty_request": request.strip() == "",
    }


def choose_order(node: str, models: list[str], state: dict) -> tuple[list[str], str, dict]:
    inputs = decision_inputs(state)
    if node != "implement" or not models:
        return list(models), "configured order", inputs
    preferred: str | None = None
    reason = "configured order"
    if inputs["review_rounds"] > 0 and "sol" in models:
        preferred, reason = "sol", "retry after failure"
    elif inputs["risk_high"] and "sol" in models:
        preferred, reason = "sol", "risk:high"
    elif (
        inputs["empty_request"] or inputs["request_chars"] > SMALL_CHARS or inputs["file_count"] > SMALL_FILES
    ) and "sol" in models:
        preferred, reason = "sol", "large or ambiguous build"
    elif "terra" in models:
        preferred, reason = "terra", "small scoped build"
    if preferred is None:
        return list(models), reason, inputs
    rest = [model for model in models if model != preferred]
    return [preferred, *rest], reason, inputs
