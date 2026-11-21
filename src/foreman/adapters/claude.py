from collections.abc import Mapping
from subprocess import CompletedProcess
from typing import ClassVar, Literal

from pydantic import Field
from typing_extensions import override

from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, build_outcome


QUOTA_ERRORS = {"usage_limit_reached", "model_requires_usage_credits"}


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
    total_cost_usd: float | None = None

    @override
    def outcome(self, process: CompletedProcess[str], model_id: str) -> Outcome:
        kind: OutcomeKind = "ran"
        detail = "\n".join(self.errors) or self.result or process.stderr.strip()
        if self.is_error and (self.api_error_status == 429 or self.api_error in QUOTA_ERRORS):
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
        elif self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=self.read_usage(), total_cost_usd=self.total_cost_usd,
            session_id=self.session_id, detail=detail,
        )


class ClaudeAdapter(NativeAdapter):
    envelope = ClaudeEnvelope
    env: ClassVar[Mapping[str, str]] = {"CLAUDE_CODE_NO_MODEL_FALLBACK": "1"}

    def command(self, call: Call) -> list[str]:
        effort = ["--effort", self.model.effort] if self.model.effort else []
        return ["claude", "--model", self.model.model_id, *effort, "--permission-mode", self.permission_mode(call),
                "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
