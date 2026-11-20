"""Run a pinned worker and read its final stage result."""

import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory
import time
from typing import Callable

from foreman.config import ConfigError, family
from foreman.dispatch import CliMissing, InvalidOutput, QuotaExhausted
from foreman.redaction import redact_text
from foreman.transcripts import locate_session_transcript

CommandRunner = Callable[[list[str], Path], subprocess.CompletedProcess[str]]

REQUIRED = {
    "triage": ("triage",),
    "spec": ("spec", "spec_gate"),
    "research": ("research",),
    "implement": ("implementation",),
    "review": ("review", "risk"),
}
OPTIONAL = {
    "triage": ("slice", "human_question"),
    "spec": ("slice", "slices", "human_question"),
    "implement": ("head_sha", "pr_summary", "browser_proof", "formal_proof"),
    "review": ("review_note", "comments"),
}
SLICE_PROMPT = (
    "For a ready slice, include slice with title, acceptance, depends_on (empty list), "
    "screen (docs/screens/<file>.md or empty), and properties (list of name and constraint). "
    "Preserve the proof obligations in the request. For needs_human, include human_question."
)
PROOF_PROMPT = (
    "For a required screen, report browser_proof with screen, trace, and passed. "
    "For required properties, report formal_proof with ran and constraints, a list of "
    "constraint and rejected results. Report evidence you actually collected."
)
EFFORTS = {"low", "medium", "high", "xhigh", "max", "ultra"}

_DOC_SCHEMA = {"anyOf": [{"type": "string"}, {"type": "object", "additionalProperties": {"type": "string"}}]}
_STRICT_DOC_SCHEMA = {"type": "string"}
_BROWSER_PROOF_SCHEMA = {
    "type": "object",
    "required": ["screen", "trace", "passed"],
    "properties": {
        "screen": {"type": "string"},
        "trace": {"type": "string"},
        "passed": {"type": "boolean"},
    },
    "additionalProperties": False,
}
_FORMAL_CONSTRAINT_SCHEMA = {
    "type": "object",
    "required": ["constraint", "rejected"],
    "properties": {
        "constraint": {"type": "string"},
        "rejected": {"type": "boolean"},
    },
    "additionalProperties": False,
}
_FORMAL_PROOF_SCHEMA = {
    "type": "object",
    "required": ["ran", "constraints"],
    "properties": {
        "ran": {"type": "boolean"},
        "constraints": {"type": "array", "items": _FORMAL_CONSTRAINT_SCHEMA},
    },
    "additionalProperties": False,
}
_SLICE_PROP_SCHEMA = {
    "type": "object",
    "required": ["name", "constraint"],
    "properties": {"name": {"type": "string"}, "constraint": {"type": "string"}},
    "additionalProperties": False,
}
_SLICE_SCHEMA = {
    "type": "object",
    "required": ["title", "acceptance", "depends_on", "screen", "properties"],
    "properties": {
        "title": {"type": "string"},
        "acceptance": {"type": "string"},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "screen": {"type": "string"},
        "properties": {"type": "array", "items": _SLICE_PROP_SCHEMA},
    },
    "additionalProperties": False,
}
_RISK_SCHEMA = {
    "type": "object",
    "required": ["alignment", "impact", "verification", "rollback", "error_detection", "level"],
    "properties": {
        "alignment": {"type": "string"},
        "impact": {"type": "string"},
        "verification": {"type": "string"},
        "rollback": {"type": "string"},
        "error_detection": {"type": "string"},
        "level": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "additionalProperties": False,
}


def _comment_schema(*, strict: bool = False) -> dict:
    if strict:
        return {
            "type": "object",
            "required": ["path", "line", "side", "start_line", "start_side", "body"],
            "properties": {
                "path": {"type": "string"},
                "line": {"type": "integer", "minimum": 1},
                "side": {"type": "string", "enum": ["LEFT", "RIGHT"]},
                "start_line": {"type": ["integer", "null"], "minimum": 1},
                "start_side": {"anyOf": [{"type": "string", "enum": ["LEFT", "RIGHT"]}, {"type": "null"}]},
                "body": {"type": "string"},
            },
            "additionalProperties": False,
        }
    return {
        "type": "object",
        "required": ["path", "line", "side", "body"],
        "properties": {
            "path": {"type": "string"},
            "line": {"type": "integer", "minimum": 1},
            "side": {"type": "string", "enum": ["LEFT", "RIGHT"]},
            "start_line": {"type": "integer", "minimum": 1},
            "start_side": {"type": "string", "enum": ["LEFT", "RIGHT"]},
            "body": {"type": "string"},
        },
        "additionalProperties": False,
    }


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def stage_schema(node: str, *, strict: bool = False) -> dict:
    """Return a JSON Schema for a delivery graph stage output.

    When strict=True (OpenAI / Codex --output-schema), every property is listed
    in required and optional values become nullable so strict mode accepts them.
    """
    doc = _STRICT_DOC_SCHEMA if strict else _DOC_SCHEMA
    if node == "triage":
        props = {
            "triage": {"type": "string", "enum": ["implement", "spec", "question", "needs_human"]},
            "slice": _nullable(_SLICE_SCHEMA) if strict else _SLICE_SCHEMA,
            "human_question": {"type": ["string", "null"]} if strict else {"type": "string"},
        }
        return {
            "type": "object",
            "required": list(props.keys()) if strict else ["triage"],
            "properties": props,
            "additionalProperties": False,
        }
    if node == "spec":
        props = {
            "spec": doc,
            "spec_gate": {"type": "string", "enum": ["ready", "needs_human"]},
            "slice": _nullable(_SLICE_SCHEMA) if strict else _SLICE_SCHEMA,
            "slices": _nullable({"type": "array", "items": _SLICE_SCHEMA}) if strict else {"type": "array", "items": _SLICE_SCHEMA},
            "human_question": {"type": ["string", "null"]} if strict else {"type": "string"},
        }
        return {
            "type": "object",
            "required": list(props.keys()) if strict else ["spec", "spec_gate"],
            "properties": props,
            "additionalProperties": False,
        }
    if node == "research":
        return {
            "type": "object",
            "required": ["research"],
            "properties": {"research": doc},
            "additionalProperties": False,
        }
    if node == "implement":
        props = {
            "implementation": doc,
            "head_sha": {"type": ["string", "null"]},
            "pr_summary": {"type": ["string", "null"]} if strict else {"type": "string"},
            "browser_proof": _nullable(_BROWSER_PROOF_SCHEMA) if strict else {"type": ["object", "null"]},
            "formal_proof": _nullable(_FORMAL_PROOF_SCHEMA) if strict else {"type": ["object", "null"]},
        }
        return {
            "type": "object",
            "required": list(props.keys()) if strict else ["implementation"],
            "properties": props,
            "additionalProperties": False,
        }
    if node == "review":
        comments_schema = {"type": "array", "items": _comment_schema(strict=strict)}
        props = {
            "review": {"type": "string"},
            "review_note": {"type": ["string", "null"]} if strict else {"type": "string"},
            "risk": _RISK_SCHEMA,
            "comments": _nullable(comments_schema) if strict else comments_schema,
        }
        return {
            "type": "object",
            "required": list(props.keys()) if strict else ["review", "risk"],
            "properties": props,
            "additionalProperties": False,
        }
    raise ValueError(f"no schema defined for stage {node}")


def model_and_effort(model: str) -> tuple[str, str | None]:
    slug, sep, effort = model.rpartition("-")
    if sep and slug and effort in EFFORTS:
        return slug, effort
    return model, None


def _requested_model_ran(slug: str, usage: object) -> bool:
    """Stop unless the usage report confirms the requested model ran.

    No report is accepted. A CLI suffix such as grok-4.7-build is still the
    requested model. An extra model from the same provider stays only when the
    requested model is in the report. Another provider does not.
    """
    if usage is None or usage == {}:
        return True
    if not isinstance(usage, dict):
        return False
    try:
        expected = family(slug)
    except ConfigError:
        return False
    matched = False
    for key in usage:
        if not isinstance(key, str):
            return False
        try:
            reported = family(key)
        except ConfigError:
            return False
        if reported != expected:
            return False
        if key == slug or key.startswith(f"{slug}-"):
            matched = True
    return matched


QUOTA = re.compile(
    r"quota|rate.limit|usage limit|hit your limit|resource_exhausted|too many requests|\b429\b",
    re.IGNORECASE,
)


def _detail(text: object, limit: int = 200) -> str:
    return " ".join(redact_text(text, redact_pii=True).split())[:limit]


def run_worker_command(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True)


def json_object(text: str) -> dict:
    snippet = _detail(text)
    try:
        result = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise InvalidOutput(
            f"worker did not return a JSON object: {snippet}" if snippet else "worker did not return a JSON object"
        ) from exc
    if not isinstance(result, dict):
        raise InvalidOutput(
            f"worker did not return a JSON object: {snippet}" if snippet else "worker did not return a JSON object"
        )
    return result


def _counts_spend(usage: object) -> bool:
    # Codex emits an all-zero usage object when it captured no total; that is unknown spend, not zero.
    return isinstance(usage, dict) and any(isinstance(v, (int, float)) and v > 0 for v in usage.values())


def _parse_codex_events(stdout: str, slug: str) -> tuple[str | None, dict]:
    """Extract final message text, session_id, errors, and token usage from Codex JSONL output."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if len(lines) <= 1 and not (lines and '"type"' in lines[0]):
        return None, {"session_id": "", "model_usage": {}, "parsed_events": False, "failed_error": ""}
    session_id = ""
    usage: dict = {}
    last_message: str | None = None
    failed_error = ""
    parsed_any_event = False
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or "type" not in event:
            continue
        parsed_any_event = True
        if isinstance(event.get("thread_id"), str) and event["thread_id"]:
            session_id = event["thread_id"]
        if isinstance(event.get("session_id"), str) and event["session_id"]:
            session_id = event["session_id"]
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if not session_id and isinstance(payload.get("id"), str) and event["type"] == "session_meta":
            session_id = payload["id"]
        if event["type"] == "error" and isinstance(event.get("message"), str):
            failed_error = event["message"]
        elif event["type"] == "turn.failed":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            if isinstance(err.get("message"), str):
                failed_error = err["message"]
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            last_message = item["text"]
        elif isinstance(event.get("last_message"), str):
            last_message = event["last_message"]
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        ev_usage = event.get("usage") or payload.get("usage") or info.get("total_token_usage")
        if _counts_spend(ev_usage):
            usage = ev_usage
    if not parsed_any_event:
        return None, {"session_id": "", "model_usage": {}, "parsed_events": False, "failed_error": ""}
    return last_message, {
        "session_id": session_id,
        "model_usage": ({slug: usage} if usage else {}),
        "parsed_events": True,
        "failed_error": failed_error,
    }


CODEX_SESSION_BANNER = re.compile(r"^session id:[ \t]*([A-Za-z0-9_-]+)[ \t]*$", re.MULTILINE | re.IGNORECASE)
CODEX_USAGE_EVENTS = {"token_count", "token_usage_record"}


def _codex_rollout_events(session_id: str) -> list[dict] | None:
    """Parsed events of the one rollout file for this session, or None when missing or ambiguous."""
    path = locate_session_transcript(session_id, provider="codex") if session_id else None
    if path is None:
        return None
    events = []
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ev = json.loads(raw_line)
        except ValueError:
            continue
        if isinstance(ev, dict) and isinstance(ev.get("payload"), dict):
            events.append(ev)
    return events


def _codex_rollout_usage(session_id: str) -> dict:
    """Latest cumulative token usage a Codex rollout recorded, so failed attempts still count spend."""
    usage: dict = {}
    for ev in _codex_rollout_events(session_id) or []:
        payload = ev["payload"]
        if payload.get("type") not in CODEX_USAGE_EVENTS:
            continue
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        found = info.get("total_token_usage") or payload.get("usage")
        if _counts_spend(found):
            usage = found
    return usage


def _verify_codex_rollout_model(session_id: str, slug: str, *, required: bool) -> None:
    """Check the session rollout's model; without a stderr banner the rollout is the only evidence."""
    events = _codex_rollout_events(session_id)
    if events is None:
        if required:
            raise InvalidOutput(
                f"codex rollout for session {_detail(session_id) or 'none'} is missing or ambiguous, expected {slug}"
            )
        return
    seen_models: list[str] = []
    for ev in events:
        payload = ev["payload"]
        if ev.get("type") == "turn_context":
            model = payload.get("model")
        elif ev.get("type") == "world_state":
            state = payload.get("state")
            mode = state.get("collaboration_mode") if isinstance(state, dict) else None
            model = mode.get("model") if isinstance(mode, dict) else None
        else:
            continue
        if isinstance(model, str) and model:
            seen_models.append(model)
    if not seen_models:
        raise InvalidOutput(f"codex rollout has no model metadata, expected {slug}")
    for recorded_model in seen_models:
        if recorded_model != slug and not recorded_model.startswith(f"{slug}-"):
            raise InvalidOutput(f"codex rollout reported model {_detail(recorded_model)}, expected {slug}")


def _codex_resume_usage(usage: dict, slug: str, session_id: str, records: list[dict]) -> dict:
    prior_usage = [
        record["model_usage"][slug]
        for record in records
        if isinstance(record, dict)
        and record.get("session_id") == session_id
        and isinstance(record.get("model_usage"), dict)
        and isinstance(record["model_usage"].get(slug), dict)
    ]
    delta = dict(usage)
    for key in (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
        "output_tokens", "reasoning_output_tokens", "total_tokens",
    ):
        if isinstance(current := usage.get(key), int):
            already_recorded = sum(entry[key] for entry in prior_usage if isinstance(entry.get(key), int))
            delta[key] = max(0, current - already_recorded)
    return delta


def _envelope_meta(envelope: dict, provider: str, model: str, slug: str, elapsed_ms: int) -> dict:
    usage = envelope.get("modelUsage")
    return {
        "provider": provider,
        "model": model,
        "slug": slug,
        "session_id": envelope.get("session_id") or envelope.get("sessionId") or "",
        "duration_ms": envelope.get("duration_ms") or elapsed_ms,
        "num_turns": envelope.get("num_turns"),
        "total_cost_usd": envelope.get("total_cost_usd") or envelope.get("cost_usd"),
        "model_usage": usage if isinstance(usage, dict) else (
            {slug: envelope["usage"]} if isinstance(envelope.get("usage"), dict) else {}
        ),
    }


class CliWorker:
    def __init__(self, cwd: Path, run: CommandRunner = run_worker_command):
        self.cwd = cwd
        self.run = run
        self.last_meta: dict | None = None

    def __call__(self, node: str, model: str | None, state: dict, instruction: str) -> dict:
        self.last_meta = None
        if model is None:
            raise QuotaExhausted(model)
        required = REQUIRED[node]
        optional = OPTIONAL.get(node, ())
        prompt = (
            f"{instruction}\n\nReturn exactly one JSON object without surrounding text. "
            f"Required keys: {', '.join(required)}. Optional keys: {', '.join(optional) or 'none'}. "
            "Write stage decisions as values of their corresponding keys. "
            "Do not arm a merge."
        )
        if node in {"spec", "implement", "research"}:
            label = {"spec": "spec", "implement": "implementation", "research": "research"}[node]
            prompt += (
                f" The {label} value is one document: a string, or an object whose values are"
                " the sections of that document."
            )
        if node in {"triage", "spec"} and state.get("issue_kind") == "slice" and "slice" not in state:
            prompt += f" Use needs_human if you cannot determine the issue requirements. {SLICE_PROMPT}"
        if node == "implement":
            prompt += f" {PROOF_PROMPT}"
        prompt += f"\n\nDelivery state:\n{json.dumps(state)}"

        provider = family(model)
        slug, effort = model_and_effort(model)
        edit = node == "implement"
        schema = stage_schema(node, strict=(provider == "gpt"))
        prior_sessions = state.get("sessions") if isinstance(state.get("sessions"), dict) else {}
        prior_models = state.get("models_used") if isinstance(state.get("models_used"), dict) else {}
        resume_id = (
            prior_sessions.get("implement")
            if node == "implement" and prior_models.get("implement") == model
            else None
        )
        if not isinstance(resume_id, str) or not resume_id.strip():
            resume_id = None

        started = time.monotonic()
        with TemporaryDirectory(prefix="foreman_") as tmpdir:
            tmp = Path(tmpdir)
            schema_file = tmp / "schema.json"
            out_file = tmp / "last_message.json"
            if provider == "gpt":
                schema_file.write_text(json.dumps(schema), encoding="utf-8")
                effort_args = ["-c", f"model_reasoning_effort={effort}"] if effort else []
                common_tail = [
                    "--json",
                    "--output-schema",
                    str(schema_file),
                    "-o",
                    str(out_file),
                ]
                if resume_id:
                    args = ["codex", "exec", "resume", "-m", slug, *effort_args, *common_tail, resume_id, prompt]
                else:
                    sandbox = "workspace-write" if edit else "read-only"
                    args = [
                        "codex", "exec", "-m", slug, *effort_args,
                        "--color", "never", "-s", sandbox, *common_tail, prompt,
                    ]
            else:
                mode = "acceptEdits" if edit else "plan"
                options = ["--output-format", "json", "--json-schema", json.dumps(schema), "--permission-mode", mode]
                resume_args = ["--resume", resume_id] if resume_id else []
                if provider == "claude":
                    effort_args = ["--effort", effort] if effort else []
                    args = ["claude", "--model", slug, *effort_args, *options, *resume_args, "-p", prompt]
                else:
                    effort_args = ["--reasoning-effort", effort] if effort else []
                    args = ["grok", "-m", slug, *effort_args, *options, "--no-subagents", *resume_args, "-p", prompt]

            try:
                process = self.run(args, self.cwd)
            except FileNotFoundError as exc:
                raise CliMissing(model, f"{args[0]} CLI is not installed") from exc
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if provider == "gpt":
                event_body, codex_meta = _parse_codex_events(process.stdout, slug)
                banner = CODEX_SESSION_BANNER.search(f"{process.stderr}\n{process.stdout}")
                session_id = codex_meta["session_id"] or (banner.group(1) if banner else "") or resume_id or ""
                usage = codex_meta["model_usage"]
                # Any exit can leave spend only in the rollout, including a zero exit with malformed output.
                if not usage and (rollout_usage := _codex_rollout_usage(session_id)):
                    usage = {slug: rollout_usage}
                if resume_id and usage:
                    usage = {slug: _codex_resume_usage(usage[slug], slug, session_id, state.get("usage_records") or [])}
                if process.returncode == 0 or usage:
                    self.last_meta = {
                        "provider": provider,
                        "model": model,
                        "slug": slug,
                        "session_id": session_id,
                        "duration_ms": elapsed_ms,
                        "num_turns": 1,
                        "total_cost_usd": None,
                        "model_usage": usage,
                    }

            if process.returncode != 0:
                combined = f"{process.stderr}\n{process.stdout}"
                if provider != "gpt":
                    try:
                        envelope = json.loads(process.stdout)
                    except ValueError:
                        envelope = None
                    if isinstance(envelope, dict):
                        self.last_meta = _envelope_meta(envelope, provider, model, slug, elapsed_ms)
                if QUOTA.search(combined):
                    raise QuotaExhausted(model, _detail(combined))
                detail = _detail(combined)
                raise InvalidOutput(
                    f"{provider} worker exited {process.returncode}: {detail}" if detail
                    else f"{provider} worker exited {process.returncode}"
                )

            if provider == "gpt":
                reroute = re.search(r"^model rerouted:[ \t]*(.+)$", process.stderr, re.MULTILINE)
                if reroute:
                    raise InvalidOutput(f"codex rerouted {slug}: {reroute.group(1).strip()}")
                reported = re.findall(r"^model:[ \t]*(\S+)[ \t]*$", process.stderr, re.MULTILINE)
                if err_msg := codex_meta.get("failed_error"):
                    if QUOTA.search(err_msg):
                        raise QuotaExhausted(model, _detail(err_msg))
                    raise InvalidOutput(f"codex worker failed: {_detail(err_msg)}")
                if reported:
                    if reported != [slug]:
                        got = ", ".join(reported)
                        raise InvalidOutput(f"codex reported model {got}, expected {slug}")
                elif not codex_meta.get("parsed_events"):
                    raise InvalidOutput(f"codex reported model none, expected {slug}")
                _verify_codex_rollout_model(session_id, slug, required=not reported)
                file_body = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
                body: object = file_body if file_body.strip() else (event_body if event_body is not None else process.stdout)
                result = json_object(body)
            else:
                envelope = json_object(process.stdout)
                if envelope.get("is_error") or envelope.get("type") == "error":
                    self.last_meta = _envelope_meta(envelope, provider, model, slug, elapsed_ms)
                    if QUOTA.search(process.stdout):
                        raise QuotaExhausted(model, _detail(process.stdout))
                    detail = _detail(
                        envelope.get("result") or envelope.get("error") or envelope.get("message") or process.stdout
                    )
                    raise InvalidOutput(
                        f"{provider} worker reported an error: {detail}" if detail
                        else f"{provider} worker reported an error"
                    )
                usage = envelope.get("modelUsage")
                if not _requested_model_ran(slug, usage):
                    reported = ", ".join(str(k) for k in usage) if isinstance(usage, dict) else _detail(usage)
                    raise InvalidOutput(f"{provider} usage reported {reported}, expected {slug}")
                self.last_meta = _envelope_meta(envelope, provider, model, slug, elapsed_ms)
                structured = (
                    envelope.get("structured_output")
                    if envelope.get("structured_output") is not None
                    else envelope.get("structuredOutput")
                )
                if isinstance(structured, dict):
                    result = structured
                elif isinstance(structured, str) and structured.strip():
                    result = json_object(structured)
                else:
                    body = envelope.get("result" if provider == "claude" else "text")
                    result = json_object(body)

        for opt_key in optional:
            if opt_key in result and result[opt_key] is None and opt_key not in {"head_sha", "browser_proof", "formal_proof"}:
                result.pop(opt_key)
        if isinstance(result.get("comments"), list):
            cleaned_comments = []
            for item in result["comments"]:
                if isinstance(item, dict):
                    cleaned_item = dict(item)
                    if cleaned_item.get("start_line") is None:
                        cleaned_item.pop("start_line", None)
                    if cleaned_item.get("start_side") is None:
                        cleaned_item.pop("start_side", None)
                    cleaned_comments.append(cleaned_item)
                else:
                    cleaned_comments.append(item)
            result["comments"] = cleaned_comments

        if not set(required) <= result.keys() <= set(required + optional):
            got = ", ".join(sorted(str(k) for k in result.keys())) or "none"
            raise InvalidOutput(f"{node} response keys do not match the stage: got {got}")
        return result
