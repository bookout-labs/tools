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


def _alert_dedupe_key(event: dict) -> str:
    # Hash the raw seed before redaction so the key never echoes secrets or collapses on placeholders.
    seed = next(
        (
            str(event[key]).strip()
            for key in ("event_id", "alert_id", "fingerprint", "dedupe_key")
            if event.get(key) is not None and str(event[key]).strip()
        ),
        None,
    )
    if seed is None:
        seed = f"{str(event.get('service') or 'service').strip()}:{str(event.get('title')).strip()}"
    return f"alert:{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:16]}"


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
        return {
            "start": True,
            "source": "alert_webhook",
            "draft_only": True,
            "dedupe_key": _alert_dedupe_key(event),
            "request": redact_text(raw_req, redact_pii=True),
        }
    raise AcceptError(f"unknown event source {source}")
