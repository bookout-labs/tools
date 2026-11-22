"""Run one Codex turn and read its JSONL result."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import shlex
from subprocess import CompletedProcess
from typing import ClassVar

from pydantic import TypeAdapter, ValidationError

from foreman._raw import is_dict
from foreman.adapters import Call, NativeUsage, NativeUsageTokens, Outcome, OutcomeKind, Usage, build_outcome
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


def _shell_command(command: str) -> str:
    # Codex can render exec commands with the login shell it used to run them.
    if command.startswith(("/bin/bash ", "/bin/sh ", "/bin/zsh ")):
        try:
            shell = shlex.split(command)
        except ValueError:
            return command
        if len(shell) == 3 and shell[1] in {"-c", "-lc"}:
            return shell[2].strip()
    return command.strip()


def _search_found_no_match(command: str, output: object) -> bool:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        args = list(lexer)
    except ValueError:
        return False
    if "\n" in command or any(arg and not arg.strip(lexer.punctuation_chars) for arg in args):
        return False
    match args:
        case ["grep" | "rg", *_]:
            return True
        case ["git", "grep", *_] | ["git", "--no-replace-objects", "grep", *_]:
            # Git can also return 1 after reporting a read error.
            return output == ""
        case _:
            return False


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
    unread: set[str]
    session_id: str | None = None
    message: str | None = None
    completed: bool = False
    usage: Usage | None = None
    failure: str | None = None
    stream_error: str | None = None
    reroute: str | None = None
    command_failure: str | None = None

    def record(self, event: dict[str, object]) -> None:
        match event:
            case {"type": "thread.started", "thread_id": str(session_id)}:
                self.session_id = session_id
            case {"type": "item.completed", "item": {"type": "agent_message", "text": str(text)}}:
                self.message = text
            case {"type": "item.completed", "item": item} if (
                self.command_failure is None and is_dict(item) and item.get("type") == "command_execution"
            ):
                self.record_command(item)
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

    def record_command(self, result: Mapping[object, object]) -> None:
        raw = result.get("command")
        command = _shell_command(raw) if isinstance(raw, str) else ""
        code, status = result.get("exit_code"), result.get("status")
        if (status == "completed" and code == 0) or (
            status in ("completed", "failed") and code == 1
            and _search_found_no_match(command, result.get("aggregated_output"))
        ):
            self.unread.discard(command)
        else:
            suffix = f" (exit {code})" if isinstance(code, int) else ""
            self.command_failure = f"Codex reviewer command failed{suffix}"


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
        turn = CodexTurn(set(call.checks) if call.review else set())
        try:
            for line in process.stdout.split("\n"):
                if line.strip():
                    turn.record(EVENT.validate_json(line))
        except ValidationError:
            return Outcome("failed", session_id=turn.session_id, detail="Codex emitted invalid JSONL")

        kind, output, detail = self._result(process.returncode, turn, review=call.review)
        outcome = build_outcome(
            kind, output=output, usage=turn.usage,
            session_id=turn.session_id, detail=detail,
        )

        if outcome.usage is None:
            return outcome
        return replace(outcome, native_usage=NativeUsage(
            family=self.model.family, aggregation="turn_total", entries={
                "turn": NativeUsageTokens(
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                    cache_read_input_tokens=outcome.usage.cache_read_input_tokens,
                    cache_creation_input_tokens=outcome.usage.cache_creation_input_tokens,
                    total_cost_usd=outcome.usage.total_cost_usd,
                ),
            },
        ))

    def _result(self, returncode: int, turn: CodexTurn, *, review: bool) -> tuple[OutcomeKind, dict[str, object] | None, str]:
        if turn.reroute:
            return "failed", None, turn.reroute
        if returncode or turn.failure or not turn.completed:
            error = turn.failure or (turn.stream_error if not turn.completed else None)
            if error and error.startswith(QUOTA_PREFIXES):
                return "quota", None, error
            detail = error or (f"Codex exited {returncode}" if returncode else "Codex did not complete the turn")
            return "failed", None, detail
        if review and turn.command_failure:
            return "failed", None, turn.command_failure
        if review and turn.unread:
            return "failed", None, f"Codex reviewer did not complete required read: {min(turn.unread)}"
        if turn.message is None:
            return "failed", None, "Codex did not return a final agent message"
        try:
            output = EVENT.validate_python(json.loads(turn.message))
        except json.JSONDecodeError:
            return "failed", None, "Codex did not return valid JSON"
        except ValidationError:
            return "failed", None, "Codex did not return a JSON object"
        return "ran", output, ""
