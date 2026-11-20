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
_COMMENT_SCHEMA = {
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


def stage_schema(node: str) -> dict:
    """Return a JSON Schema for a delivery graph stage output."""
    if node == "triage":
        return {
            "type": "object",
            "required": ["triage"],
            "properties": {
                "triage": {"type": "string", "enum": ["implement", "spec", "question", "needs_human"]},
                "slice": _SLICE_SCHEMA,
                "human_question": {"type": "string"},
            },
            "additionalProperties": False,
        }
    if node == "spec":
        return {
            "type": "object",
            "required": ["spec", "spec_gate"],
            "properties": {
                "spec": _DOC_SCHEMA,
                "spec_gate": {"type": "string", "enum": ["ready", "needs_human"]},
                "slice": _SLICE_SCHEMA,
                "slices": {"type": "array", "items": _SLICE_SCHEMA},
                "human_question": {"type": "string"},
            },
            "additionalProperties": False,
        }
    if node == "research":
        return {
            "type": "object",
            "required": ["research"],
            "properties": {"research": _DOC_SCHEMA},
            "additionalProperties": False,
        }
    if node == "implement":
        return {
            "type": "object",
            "required": ["implementation"],
            "properties": {
                "implementation": _DOC_SCHEMA,
                "head_sha": {"type": ["string", "null"]},
                "pr_summary": {"type": "string"},
                "browser_proof": {"type": ["object", "null"]},
                "formal_proof": {"type": ["object", "null"]},
            },
            "additionalProperties": False,
        }
    if node == "review":
        return {
            "type": "object",
            "required": ["review", "risk"],
            "properties": {
                "review": {"type": "string"},
                "review_note": {"type": "string"},
                "risk": _RISK_SCHEMA,
                "comments": {"type": "array", "items": _COMMENT_SCHEMA},
            },
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
    if text is None:
        return ""
    return " ".join(str(text).split())[:limit]


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


def _parse_codex_events(stdout: str, slug: str) -> tuple[str | None, dict]:
    """Extract final message text, session_id, and token usage from Codex JSONL output."""
    lines = [ln.strip() for ln in (stdout or "").splitlines() if ln.strip()]
    if len(lines) <= 1 and not (lines and '"type"' in lines[0]):
        return None, {"session_id": "", "model_usage": {}}
    session_id = ""
    usage: dict = {}
    last_message: str | None = None
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
        item = event.get("item") if isinstance(event.get("item"), dict) else {}
        if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
            last_message = item["text"]
        elif isinstance(event.get("last_message"), str):
            last_message = event["last_message"]
        ev_usage = (
            event.get("usage")
            or payload.get("usage")
            or (payload.get("info") or {}).get("total_token_usage")
        )
        if isinstance(ev_usage, dict) and ev_usage:
            usage = ev_usage
    if not parsed_any_event:
        return None, {"session_id": "", "model_usage": {}}
    return last_message, {"session_id": session_id, "model_usage": ({slug: usage} if usage else {})}


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
        schema = stage_schema(node)
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
                    args = ["codex", "exec", "resume", resume_id, "-m", slug, *effort_args, *common_tail, prompt]
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

            if process.returncode != 0:
                combined = f"{process.stderr}\n{process.stdout}"
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
                if reported != [slug]:
                    got = ", ".join(reported) if reported else "none"
                    raise InvalidOutput(f"codex reported model {got}, expected {slug}")
                file_body = out_file.read_text(encoding="utf-8") if out_file.exists() else ""
                event_body, codex_meta = _parse_codex_events(process.stdout, slug)
                body: object = file_body if file_body.strip() else (event_body if event_body is not None else process.stdout)
                self.last_meta = {
                    "provider": provider,
                    "model": model,
                    "slug": slug,
                    "session_id": codex_meta.get("session_id", ""),
                    "duration_ms": elapsed_ms,
                    "num_turns": 1,
                    "total_cost_usd": None,
                    "model_usage": codex_meta.get("model_usage", {}),
                }
                result = json_object(body)
            else:
                envelope = json_object(process.stdout)
                if envelope.get("is_error") or envelope.get("type") == "error":
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
                raw_usage = usage if isinstance(usage, dict) else (
                    {slug: envelope["usage"]} if isinstance(envelope.get("usage"), dict) else {}
                )
                self.last_meta = {
                    "provider": provider,
                    "model": model,
                    "slug": slug,
                    "session_id": envelope.get("session_id") or envelope.get("sessionId") or "",
                    "duration_ms": envelope.get("duration_ms") or elapsed_ms,
                    "num_turns": envelope.get("num_turns"),
                    "total_cost_usd": envelope.get("total_cost_usd") or envelope.get("cost_usd"),
                    "model_usage": raw_usage,
                }
                structured = envelope.get("structured_output")
                if isinstance(structured, dict):
                    result = structured
                elif isinstance(structured, str) and structured.strip():
                    result = json_object(structured)
                else:
                    body = envelope.get("result" if provider == "claude" else "text")
                    result = json_object(body)

        if not set(required) <= result.keys() <= set(required + optional):
            got = ", ".join(sorted(str(k) for k in result.keys())) or "none"
            raise InvalidOutput(f"{node} response keys do not match the stage: got {got}")
        return result
