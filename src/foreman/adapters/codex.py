"""Run one Codex turn and read its JSONL result."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
from subprocess import CompletedProcess
from typing import ClassVar

from pydantic import TypeAdapter, ValidationError

from foreman._raw import is_dict
from foreman.adapters import Call, CapabilityUnavailable, NativeUsage, NativeUsageTokens, Outcome, OutcomeKind, Usage, build_outcome
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


def rollout_usage(session_id: str) -> Usage | None:
    """The usage of a session's last turn, from the rollout Codex writes after each model response.

    A turn that stops on quota never reports usage in its stream, but its rollout keeps the cumulative count up to the
    last response. The turn's usage is that count minus the count when the turn started.
    """
    if not re.fullmatch(r"[0-9a-f-]+", session_id):
        return None
    # The worker's own Codex home: the one CodexAdapter.env hands it, or the default under its HOME.
    sessions = Path(CodexAdapter.env.get("CODEX_HOME") or Path.home() / ".codex") / "sessions"
    rollouts = list(sessions.glob(f"*/*/*/rollout-*-{session_id}.jsonl"))
    if len(rollouts) != 1:
        return None
    fields = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")
    total: dict[str, int] = dict.fromkeys(fields, 0)
    start: dict[str, int] | None = None
    counted = False
    try:
        with rollouts[0].open(encoding="utf-8") as lines:
            for line in lines:
                payload = EVENT.validate_json(line).get("payload")
                if not is_dict(payload):
                    continue
                if payload.get("type") == "task_started":
                    start, counted = total, False
                elif payload.get("type") == "token_count" and is_dict(info := payload.get("info")):
                    counts = info.get("total_token_usage")
                    if not is_dict(counts) or not all(isinstance(counts.get(field), int) for field in fields):
                        return None
                    total, counted = {field: int(str(counts.get(field))) for field in fields}, True
    except (OSError, ValidationError):
        return None
    # Model items prove paid work, so a turn whose usage the rollout doesn't show stays unknown, not zero.
    if start is None or not counted:
        return None
    return read_usage({field: total[field] - start[field] for field in fields})


@dataclass
class CodexTurn:
    session_id: str | None = None
    message: str | None = None
    completed: bool = False
    usage: Usage | None = None
    failure: str | None = None
    stream_error: str | None = None
    reroute: str | None = None
    worked: bool = False

    def record(self, event: dict[str, object]) -> None:
        if str(event.get("type", "")).startswith("item."):
            self.worked = True
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
    # Foreman's CODEX_HOME, by name, so the worker writes its rollouts where rollout_usage reads them.
    env: ClassVar[Mapping[str, str]] = {name: os.environ[name] for name in ("CODEX_HOME",) if name in os.environ}

    def command(self, call: Call) -> list[str]:
        schema_path = call.temp_dir / "output-schema.json"
        schema_path.write_text(call.schema, encoding="utf-8")
        filesystem = {str(root.resolve()): '"read"' for root in call.read_roots}
        if call.edit:
            filesystem[":workspace_roots"] = '{"."="write", ".git"="read", ".codex"="read"}'
            filesystem[str(call.scratch.resolve())] = '"write"'
        entries = ",".join(f"{json.dumps(path, ensure_ascii=False)}={access}" for path, access in filesystem.items())
        profile = '{extends=":read-only",network={enabled=false},filesystem={' + entries + '}}'
        policy = ["--ignore-user-config", "--ignore-rules", "-c", "approval_policy=never",
                  "-c", "default_permissions=foreman", "-c", "permissions.foreman=" + profile]
        if call.shell:
            # Codex's own shell is off, for the builder, the acceptance test writer, the verifier and a reviewer granted a check. Every command it runs goes through Foreman's exec tool to the run's attempt container,
            # which mounts the checkout and the attempt scratch at their host paths.
            if call.route is None:
                raise CapabilityUnavailable("Codex: the call has no attempt container to run its commands in")
            server = call.route.server()
            policy += ["--disable", "shell_tool", "--disable", "unified_exec",
                       "-c", f"mcp_servers.foreman.command={json.dumps(server['command'])}",
                       "-c", f"mcp_servers.foreman.args={json.dumps(server['args'])}",
                       "-c", 'mcp_servers.foreman.tools.exec.approval_mode="approve"']
        effort = ["-c", f"model_reasoning_effort={self.model.effort}"] if self.model.effort else []
        # Every call keeps its rollout so a quota stop can be charged what it used. Dispatch names the session a call
        # resumes; `exec resume` has no --color.
        session = [call.resume["id"]] if call.resume else []
        return ["codex", "exec", *(["resume"] if session else []), "-m", self.model.model_id, *effort,
                "--json", "--output-schema", str(schema_path),
                *([] if session else ["--color", "never"]), *policy, *session, call.prompt]

    def read(self, process: CompletedProcess[str], call: Call) -> Outcome:
        turn = CodexTurn()
        try:
            for line in process.stdout.split("\n"):
                if line.strip():
                    turn.record(EVENT.validate_json(line))
        except ValidationError:
            return Outcome("failed", session_id=turn.session_id, detail="Codex emitted invalid JSONL")

        kind, output, detail = self._result(process.returncode, turn)
        outcome = build_outcome(
            kind, output=output, usage=turn.usage,
            session_id=turn.session_id, detail=detail,
        )

        if outcome.usage is None and kind == "quota":
            # A quota stop before any model item did no paid work. A later one is charged what the session's rollout
            # logged for the turn, and stays unknown without it.
            if not turn.worked:
                usage = Usage(input_tokens=0, output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0)
            else:
                usage = rollout_usage(turn.session_id) if turn.session_id else None
            outcome = replace(outcome, usage=usage)
        if outcome.usage is None:
            return outcome
        report = NativeUsage(
            family=self.model.family, aggregation="turn_total", entries={
                "turn": NativeUsageTokens(
                    input_tokens=outcome.usage.input_tokens,
                    output_tokens=outcome.usage.output_tokens,
                    cache_read_input_tokens=outcome.usage.cache_read_input_tokens,
                    cache_creation_input_tokens=outcome.usage.cache_creation_input_tokens,
                    total_cost_usd=outcome.usage.total_cost_usd,
                ),
            },
        )
        return replace(outcome, native_usage=report, totals=report)

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
