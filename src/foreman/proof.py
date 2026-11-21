"""Pure admission gates for browser, constraint, and scoring evidence."""


class ProofError(ValueError):
    pass


def browser_required(slice: dict) -> bool:
    return bool(slice["screen"])


def formal_required(slice: dict) -> bool:
    return bool(slice["properties"])


def score_verdict(label: str) -> str:
    verdicts = {
        "approve": "pass",
        "block": "fail",
        "insufficient_evidence": "insufficient_evidence",
    }
    if not isinstance(label, str) or label not in verdicts:
        raise ProofError(f"unknown score label {label!r}")
    return verdicts[label]


def browser_proof_label(slice: dict, artifact: dict | None) -> str | None:
    """Return a failure label, or None when the gate accepts."""
    if not browser_required(slice):
        return None
    if not isinstance(artifact, dict):
        return "insufficient_evidence"
    trace = artifact.get("trace")
    if not isinstance(trace, str) or not trace.strip():
        return "insufficient_evidence"
    if artifact.get("screen") != slice["screen"] or artifact.get("passed") is False:
        return "block"
    return None


def formal_proof_label(slice: dict, artifact: dict | None) -> str | None:
    """Return a failure label, or None when the gate accepts."""
    if not formal_required(slice):
        return None
    if not isinstance(artifact, dict) or not artifact:
        return "insufficient_evidence"
    if artifact.get("ran") is False:
        return "insufficient_evidence"
    results = artifact.get("constraints")
    if not isinstance(results, list):
        return "block" if artifact.get("typecheck") is not None else "insufficient_evidence"
    missing = False
    for prop in slice["properties"]:
        rejections = [result["rejected"] for result in results if isinstance(result, dict)
                      and result.get("constraint") == prop["constraint"]]
        if not all(rejections):
            return "block"
        if not rejections:
            missing = True
    if missing:
        return "block" if artifact.get("typecheck") is not None else "insufficient_evidence"
    return None
