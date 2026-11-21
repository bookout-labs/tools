"""Run one Codex turn and read its JSONL result."""

from collections.abc import Mapping
from dataclasses import dataclass
import json
from subprocess import CompletedProcess
from typing import ClassVar

from pydantic import TypeAdapter, ValidationError

from foreman._raw import is_dict
from foreman.adapters import Call, Outcome, OutcomeKind, Usage, build_outcome
from foreman.config import ModelSpec

EVENT = TypeAdapter(dict[str, object])
QUOTA_PREFIXES = (
    "Quota exceeded. Check your plan and billing details.",
    "rate limit exceeded: ",
    "exceeded retry limit, last status: 429 Too Many Requests",
    "You’ve hit your usage limit.",
    "You’ve hit your usage limit for ",
    "Your workspace is out of credits.",
    "You hit your spend cap set in your workspace.",
    "You hit your spend cap set by the owner of your workspace.",
)


def read_usage(report: object) -> Usage | None:
    if not is_dict(report):
        return None
    try:
        usage = Usage.model_validate({
            "input_tokens": report.get("input_tokens"),
            "output_tokens": report.get("output_tokens"),
            "cache_read_input_tokens": report.get("cached_input_tokens"),
            "cache_creation_input_tokens": report.get("cache_write_input_tokens"),
        })
    except ValidationError:
        return None
    if usage.cache_read_input_tokens is None:
        return None
    cached = usage.cache_read_input_tokens
    if cached > usage.input_tokens:
        return None
    return usage.model_copy(update={
        "input_tokens": usage.input_tokens - cached,
        "cache_read_input_tokens": cached,
    })


@dataclass
class CodexTurn:
    session_id: str | None = None
    message: str | None = None
    completed: bool = False
    usage: Usage | None = None
    failure: str | None = None
    stream_error: str | None = None
    reroute: str | None = None

    def record(self, event: dict[str, object]) -> None:
        match event:
            case {"type": "thread.started", "thread_id": str(session_id)}:
                self.session_id = session_id
            case {"type": "item.completed", "item": {"type": "agent_message", "text": str(text)}}:
                self.message = text
            case {"type": "item.completed", "item": {"type": "error", "message": str(message)}}:
                if message.startswith("model rerouted: "):
                    self.reroute = message
            case {"type": "error", "message": str(message)}:
                self.stream_error = message
            case {"type": "turn.failed", "error": {"message": str(message)}}:
                self.failure = message or "Codex turn failed"
            case {"type": "turn.failed"}:
                self.failure = "Codex turn failed"
            case {"type": "turn.completed"}:
                self.completed = True
                self.usage = read_usage(event.get("usage"))
            case _:
                pass


@dataclass(frozen=True)
class CodexAdapter:
    model: ModelSpec
    env: ClassVar[Mapping[str, str]] = {}

    def command(self, call: Call) -> list[str]:
        schema_path = call.temp_dir / "output-schema.json"
        schema_path.write_text(call.schema, encoding="utf-8")
        filesystem = {str(root.resolve()): '"read"' for root in call.read_roots}
        if call.edit:
            filesystem[":workspace_roots"] = '{"."="write", ".git"="read", ".codex"="read"}'
            filesystem[str((call.temp_dir / "scratch").resolve())] = '"write"'
        entries = ",".join(f"{json.dumps(path, ensure_ascii=False)}={access}" for path, access in filesystem.items())
        profile = '{extends=":read-only",network={enabled=false},filesystem={' + entries + '}}'
        policy = ["--ignore-user-config", "--ignore-rules", "-c", "approval_policy=never",
                  "-c", "default_permissions=foreman", "-c", "permissions.foreman=" + profile]
        effort = ["-c", f"model_reasoning_effort={self.model.effort}"] if self.model.effort else []
        return [
            "codex", "exec", "-m", self.model.model_id, *effort, "--ephemeral",
            "--json", "--output-schema", str(schema_path), "--color", "never", *policy, call.prompt,
        ]

    def read(self, process: CompletedProcess[str], call: Call) -> Outcome:
        turn = CodexTurn()
        try:
            for line in process.stdout.split("\n"):
                if line.strip():
                    turn.record(EVENT.validate_json(line))
        except ValidationError:
            return Outcome("failed", session_id=turn.session_id, detail="Codex emitted invalid JSONL")

        kind, output, detail = self._result(process.returncode, turn)
        return build_outcome(
            kind, output=output, usage=turn.usage,
            session_id=turn.session_id, detail=detail,
        )

    def _result(self, returncode: int, turn: CodexTurn) -> tuple[OutcomeKind, dict[str, object] | None, str]:
        if turn.reroute:
            return "failed", None, turn.reroute
        if returncode or turn.failure or not turn.completed:
            error = turn.failure or (turn.stream_error if not turn.completed else None)
            if error and error.startswith(QUOTA_PREFIXES):
                return "quota", None, error
            detail = error or (f"Codex exited {returncode}" if returncode else "Codex did not complete the turn")
            return "failed", None, detail
        if turn.message is None:
            return "failed", None, "Codex did not return a final agent message"
        try:
            output = EVENT.validate_python(json.loads(turn.message))
        except json.JSONDecodeError:
            return "failed", None, "Codex did not return valid JSON"
        except ValidationError:
            return "failed", None, "Codex did not return a JSON object"
        return "ran", output, ""
