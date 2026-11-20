"""Decide whether an outside event enters the graph. This does not call a model."""

import hashlib

from foreman.redaction import redact_text

FACTORY_LABEL = "factory"


class AcceptError(ValueError):
    pass


def _request(title: object, body: object) -> str:
    if not isinstance(title, str) or not title.strip():
        raise AcceptError("event needs a title")
    if body is None:
        body = ""
    if not isinstance(body, str):
        raise AcceptError("body must be text")
    if not body.strip():
        return title.strip()
    return f"{title.strip()}\n\n{body.strip()}"


def accept_event(event: dict) -> dict:
    if not isinstance(event, dict):
        raise AcceptError("event must be a mapping")
    source = event.get("source")
    if source == "github":
        if event.get("event") != "issue_labeled":
            return {"start": False}
        labels = event.get("labels") or []
        if not isinstance(labels, list) or FACTORY_LABEL not in labels:
            return {"start": False}
        return {"start": True, "source": "github", "request": _request(event.get("title"), event.get("body"))}
    if source == "alert":
        return {"start": False}
    if source == "alert_webhook":
        severity = str(event.get("severity") or "error").strip().lower()
        if severity in {"info", "debug"}:
            return {"start": False}
        raw_req = _request(event.get("title"), event.get("body"))
        scrubbed_req = redact_text(raw_req, redact_pii=True)
        service = redact_text(str(event.get("service") or "service"), redact_pii=True).strip()
        raw_fp = event.get("dedupe_key") or event.get("fingerprint") or scrubbed_req.splitlines()[0]
        scrubbed_fp = redact_text(str(raw_fp), redact_pii=True).strip()
        if event.get("dedupe_key"):
            dedupe_key = scrubbed_fp
        else:
            digest = hashlib.sha256(f"{service}:{scrubbed_fp}".encode("utf-8")).hexdigest()[:16]
            dedupe_key = f"alert:{digest}"
        return {
            "start": True,
            "source": "alert_webhook",
            "draft_only": True,
            "dedupe_key": dedupe_key,
            "request": scrubbed_req,
        }
    raise AcceptError(f"unknown event source {source}")
