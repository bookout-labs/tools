from collections.abc import Mapping
from math import isclose
import json
from subprocess import CompletedProcess
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from typing_extensions import override

from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, Usage, build_outcome
from foreman.usd import CostUsd, usd_text


QUOTA_ERRORS = {"usage_limit_reached", "model_requires_usage_credits"}


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
        if kind == "ran" and (call.checks or call.review):
            try:
                denials = TypeAdapter(list[PermissionDenial]).validate_python(self.permission_denials, strict=True)
            except ValidationError:
                kind, detail = "failed", f"Claude denied-check evidence malformed: {self.permission_denials!r}; {call.checks}"
            else:
                for denial in denials:
                    command = denial.tool_input.get("command")
                    if call.review or denial.tool_name == "Bash" and (
                        not isinstance(command, str) or not command.strip()
                        or any(check in command or command in check for check in call.checks)
                    ):
                        kind = "failed"
                        detail = (f"Claude reviewer permission denied: {self.permission_denials!r}" if call.review else
                                  f"Claude denied the granted check: {call.checks}; denied command: {command!r}")
                        break
        if kind == "ran" and self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=self.read_usage(),
            session_id=self.session_id, detail=detail,
        )


class ClaudeAdapter(NativeAdapter):
    envelope = ClaudeEnvelope
    env: ClassVar[Mapping[str, str]] = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"}

    def command(self, call: Call) -> list[str]:
        effort = ["--effort", self.model.effort] if self.model.effort else []
        budget = ["--max-budget-usd", usd_text(call.budget_usd)] if call.budget_usd is not None else []
        allow = [f"Bash({command})" for command in call.checks]
        if call.edit:
            allow += ["Edit", "Write"]
        settings = {"permissions": {"allow": allow, "deny": [
            f"Edit(/{root.resolve()}/**)" for root in call.read_roots
        ]}}
        settings_path = call.temp_dir / "claude-settings.json"
        settings_path.write_text(json.dumps(settings))
        directories = [str(root) for root in call.read_roots]
        if call.edit:
            directories.append(str(call.temp_dir))
        scope = [arg for directory in directories for arg in ("--add-dir", directory)]
        return ["claude", "--model", self.model.model_id, *effort, "--restricted",
                "--strict-mcp-config", "--permission-mode", "dontAsk",
                "--tools", "Read,Glob,Grep,Bash,Edit,Write" if call.edit else
                "Read,Glob,Grep,Bash" if call.checks else "Read,Glob,Grep",
                *scope, "--settings", str(settings_path),
                *budget, "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
