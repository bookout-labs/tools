"""Pure admission gates for browser, constraint, and scoring evidence."""


class ProofError(ValueError):
    pass


def browser_required(slice: dict) -> bool:
    screen = slice.get("screen", "")
    if not isinstance(screen, str):
        raise ProofError("screen must be text")
    return bool(screen.strip())


def formal_required(slice: dict) -> bool:
    properties = slice.get("properties", [])
    if not isinstance(properties, list):
        raise ProofError("properties must be a list")
    for prop in properties:
        if not isinstance(prop, dict):
            raise ProofError("property must be a mapping")
        for field in ("name", "constraint"):
            value = prop.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ProofError(f"property needs {field}")
    return bool(properties)


def accept_browser_proof(slice: dict, artifact: dict) -> bool:
    """Accept {screen, trace}, or raise when required evidence is missing."""
    if not browser_required(slice):
        return True
    if not isinstance(artifact, dict):
        raise ProofError("browser proof must be a mapping")
    if artifact.get("screen") != slice["screen"]:
        raise ProofError("browser proof screen does not match the slice")
    trace = artifact.get("trace")
    if not isinstance(trace, str) or not trace.strip():
        raise ProofError("browser proof needs a trace path")
    return True


def accept_formal_proof(slice: dict, artifact: dict) -> bool:
    """Accept {constraints: [{constraint, rejected: true}]} for every property."""
    if not formal_required(slice):
        return True
    if not isinstance(artifact, dict):
        raise ProofError("formal proof must be a mapping")
    evidence = artifact.get("constraints")
    if not isinstance(evidence, list):
        raise ProofError("formal proof needs constraint rejection results")
    rejected = set()
    for result in evidence:
        if not isinstance(result, dict):
            raise ProofError("constraint result must be a mapping")
        constraint = result.get("constraint")
        if not isinstance(constraint, str) or not constraint.strip():
            raise ProofError("constraint result needs a constraint name")
        if result.get("rejected") is True:
            rejected.add(constraint)
    for prop in slice["properties"]:
        if prop["constraint"] not in rejected:
            raise ProofError(f"missing rejection for constraint {prop['constraint']}")
    return True


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
        matches = [result for result in results if isinstance(result, dict)
                   and result.get("constraint") == prop["constraint"]]
        if any(result.get("rejected") is False for result in matches):
            return "block"
        if not any(result.get("rejected") is True for result in matches):
            missing = True
    if missing:
        return "block" if artifact.get("typecheck") is not None else "insufficient_evidence"
    return None
