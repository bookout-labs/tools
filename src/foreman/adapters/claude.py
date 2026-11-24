from collections.abc import Mapping
from dataclasses import replace
from math import isclose
from pathlib import Path
import json
import platform
import shutil
from subprocess import CompletedProcess
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from typing_extensions import override

from foreman._raw import is_dict, is_list
from foreman.adapters import Call, CapabilityUnavailable, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, Usage, build_outcome
from foreman.usd import CostUsd, usd_text


EVENT = TypeAdapter(dict[str, object])
QUOTA_ERRORS = {"usage_limit_reached", "model_requires_usage_credits"}
# Claude Code 2.1.329 prints this and exits at startup when failIfUnavailable meets a sandbox that cannot start.
SANDBOX_UNAVAILABLE = "sandbox required but unavailable"


class PermissionDenial(BaseModel):
    tool_name: str
    tool_input: dict[str, object] = Field(default_factory=dict)


class ClaudeEnvelope(NativeEnvelope):
    type: Literal["result"]
    subtype: str
    is_error: bool
    api_error_status: int | None = None
    api_error: str | None = None
    result: str = ""
    errors: list[str] = Field(default_factory=list)
    structured_output: dict[str, object] | None = None
    session_id: str | None = None
    total_cost_usd: object = None
    permission_denials: object = Field(default_factory=list)

    @override
    def read_usage(self) -> Usage | None:
        usage = super().read_usage()
        if usage is None or usage.total_cost_usd is None:
            return usage
        try:
            total = TypeAdapter(CostUsd).validate_python(self.total_cost_usd, strict=True)
        except ValidationError:
            total = None
        # The envelope total corroborates the ledger; it is not an extra charge.
        if total is None or not isclose(usage.total_cost_usd, total, rel_tol=1e-6, abs_tol=1e-6):
            return usage.model_copy(update={"total_cost_usd": None})
        return usage

    @override
    def outcome(self, process: CompletedProcess[str], model_id: str, call: Call) -> Outcome:
        kind: OutcomeKind = "ran"
        detail = "\n".join(self.errors) or self.result or process.stderr.strip()
        if self.subtype == "error_max_budget_usd":
            kind = "budget_exhausted"
            detail = detail or "Claude exhausted its dollar budget"
        elif self.is_error and (self.api_error_status == 429 or self.api_error in QUOTA_ERRORS):
            kind = "quota"
            detail = detail or self.api_error or "Claude API status 429"
        elif process.returncode or self.is_error or self.api_error or self.subtype != "success":
            kind = "failed"
            detail = detail or self.api_error or f"Claude {self.subtype} (exit {process.returncode})"
        elif not self.model_usage:
            kind = "failed"
            detail = f"modelUsage missing; cannot confirm {model_id}"
        elif model_id not in self.model_usage:
            kind = "failed"
            detail = f"requested {model_id}, modelUsage reported {', '.join(self.model_usage)}"
        if kind == "ran" and call.edit and call.checks:
            try:
                denials = TypeAdapter(list[PermissionDenial]).validate_python(self.permission_denials, strict=True)
            except ValidationError:
                kind, detail = "failed", f"Claude denied-check evidence malformed: {self.permission_denials!r}; {call.checks}"
            else:
                for denial in denials:
                    command = denial.tool_input.get("command")
                    if denial.tool_name == "Bash" and (
                        not isinstance(command, str) or not command.strip()
                        or any(check in command or command in check for check in call.checks)
                    ):
                        kind = "failed"
                        detail = f"Claude denied the granted check: {call.checks}; denied command: {command!r}"
                        break
        if kind == "ran" and self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=self.read_usage(),
            session_id=self.session_id, detail=detail,
        )


def completed_reads(stream: str) -> set[str]:
    """Top-level Bash commands whose tool result came back without an error, joined by tool-use id.

    This proves a read ran, even across compaction. It doesn't prove the output was untruncated, or complete when the
    reviewer split it into several reads.
    """
    commands: dict[str, str] = {}
    completed: set[str] = set()
    for line in stream.splitlines():
        event = EVENT.validate_json(line) if line.strip() else {}
        message = event.get("message")
        if event.get("parent_tool_use_id") is not None or not is_dict(message) or not is_list(blocks := message.get("content")):
            continue
        for block in filter(is_dict, blocks):
            match event.get("type"), block:
                case "assistant", {"type": "tool_use", "name": "Bash", "id": str(use), "input": {"command": str(command)}}:
                    commands[use] = command.strip()
                case "user", {"type": "tool_result", "tool_use_id": str(use)} if use in commands and block.get("is_error") is not True:
                    completed.add(commands[use])
                case _:
                    pass
    return completed


def both(paths: tuple[Path, ...]) -> list[str]:
    """Each path as given and as resolved, so a denial holds whether a tool names the link or its target."""
    return list(dict.fromkeys(str(form) for path in paths for form in (path.absolute(), path.resolve())))


def sandboxed(system: str) -> None:
    """Claude Code sandboxes Bash with Seatbelt on macOS, and with bubblewrap and socat on Linux."""
    missing = [tool for tool in ("bwrap", "socat") if shutil.which(tool) is None] if system == "Linux" else []
    if system not in ("Darwin", "Linux") or missing:
        reason = f"{', '.join(missing)} missing" if missing else "the Bash sandbox is unsupported"
        raise CapabilityUnavailable(f"Claude on {system}: {reason}, so a worker's Bash cannot be confined")


class ClaudeAdapter(NativeAdapter):
    envelope = ClaudeEnvelope
    env: ClassVar[Mapping[str, str]] = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"}

    @override
    def read(self, process: CompletedProcess[str], call: Call) -> Outcome:
        # A sandbox that cannot start on this host ends the call before any model work, so the next model runs.
        if (call.edit or call.drive) and process.returncode and SANDBOX_UNAVAILABLE in process.stderr.lower():
            return Outcome("cli_missing", detail=process.stderr.strip())
        if not call.review:
            return super().read(process, call)
        # A review streams its events, so its tool calls prove which reads ran; the last line is the result envelope.
        result = process.stdout.rstrip().rpartition("\n")[2]
        outcome = self.read_envelope(result, process, call)
        if outcome.kind != "ran":
            return outcome
        try:
            completed = completed_reads(process.stdout)
        except ValidationError:
            return replace(outcome, kind="failed", output={}, detail="Claude emitted invalid stream JSON")
        if missing := [read for read in call.required if read not in completed]:
            return replace(outcome, kind="failed", output={}, detail=f"Claude reviewer did not complete required read: {missing[0]}")
        return outcome

    def command(self, call: Call) -> list[str]:
        effort = ["--effort", self.model.effort] if self.model.effort else []
        budget = ["--max-budget-usd", usd_text(call.budget_usd)] if call.budget_usd is not None else []
        allow = [f"Bash({command})" for command in call.checks]
        if call.edit:
            allow += ["Edit", "Write"]
        deny = [f"Edit(/{root}/**)" for root in both(call.read_roots)]
        permissions: dict[str, object] = {"allow": allow, "deny": deny}
        settings: dict[str, object] = {"permissions": permissions}
        directories = [str(root) for root in call.read_roots]
        if call.edit:
            # Edit and Write stay inside the working directories and off the git directories.
            permissions["blockWritesOutsideWorkingDirectories"] = True
            deny += [rule for path in both(call.git_dirs) for rule in (f"Edit(/{path})", f"Edit(/{path}/**)")]
        if call.edit or call.drive:
            sandboxed(platform.system())
            # Bash runs in Claude Code's OS sandbox, which fails closed: it writes the checkout and the attempt scratch,
            # never the method roots --add-dir exposes or the git directories, and reaches no network.
            settings["sandbox"] = {
                "enabled": True, "autoAllowBashIfSandboxed": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
                "network": {"allowedDomains": [], "strictAllowlist": True},
                "filesystem": {"denyWrite": both((*call.read_roots, *call.git_dirs))},
            }
            directories.append(str(call.scratch))
        settings_path = call.temp_dir / "claude-settings.json"
        settings_path.write_text(json.dumps(settings))
        scope = [arg for directory in directories for arg in ("--add-dir", directory)]
        resume = ["--resume", call.resume_session, "--fork-session"] if call.edit and call.resume_session else []
        return ["claude", "--model", self.model.model_id, *effort, *resume, "--restricted",
                "--strict-mcp-config", "--permission-mode", "dontAsk",
                "--tools", "Read,Glob,Grep,Bash,Edit,Write" if call.edit else
                "Read,Glob,Grep,Bash" if call.checks or call.drive else "Read,Glob,Grep",
                *scope, "--settings", str(settings_path),
                *budget, *(["--output-format", "stream-json", "--verbose"] if call.review else ["--output-format", "json"]),
                "--json-schema", call.schema, "-p", call.prompt]
