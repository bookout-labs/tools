"""Pure admission gates for browser, constraint, and scoring evidence."""


from foreman.stage_output import BrowserProofRecord, FormalProofRecord, SliceRecord


class ProofError(ValueError):
    pass


def browser_required(slice: SliceRecord) -> bool:
    return bool(slice["screen"])


def formal_required(slice: SliceRecord) -> bool:
    return bool(slice["properties"])


def browser_proof_label(slice: SliceRecord, artifact: BrowserProofRecord | None) -> str | None:
    """Return a failure label, or None when the gate accepts."""
    if not browser_required(slice):
        return None
    if artifact is None:
        return "insufficient_evidence"
    trace = artifact["trace"]
    if trace is None or not trace.strip():
        return "insufficient_evidence"
    if artifact["screen"] != slice["screen"] or artifact["passed"] is False:
        return "block"
    return None


def formal_proof_label(slice: SliceRecord, artifact: FormalProofRecord | None) -> str | None:
    """Return a failure label, or None when the gate accepts."""
    if not formal_required(slice):
        return None
    if artifact is None or artifact["ran"] is False:
        return "insufficient_evidence"
    results = artifact["constraints"]
    if results is None:
        return "block" if artifact["typecheck"] is not None else "insufficient_evidence"
    missing = False
    for prop in slice["properties"]:
        rejections = [result["rejected"] for result in results if result["constraint"] == prop["constraint"]]
        if not all(rejections):
            return "block"
        if not rejections:
            missing = True
    if missing:
        return "block" if artifact["typecheck"] is not None else "insufficient_evidence"
    return None
