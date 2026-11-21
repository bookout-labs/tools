from subprocess import CompletedProcess
from typing import Literal

from pydantic import Field

from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, Usage, build_outcome


class ClaudeEnvelope(NativeEnvelope):
    type: Literal["result"]
    subtype: str
    is_error: bool
    api_error_status: int | None = None
    result: str = ""
    errors: list[str] = Field(default_factory=list)
    structured_output: dict[str, object] | None = None
    session_id: str | None = None
    model_usage: dict[str, object] | None = Field(default=None, alias="modelUsage")
    usage: Usage | None = None
    total_cost_usd: float | None = None

    def outcome(self, process: CompletedProcess[str], model_id: str) -> Outcome:
        kind: OutcomeKind = "ran"
        detail = "\n".join(self.errors) or self.result or process.stderr.strip()
        if self.is_error and self.api_error_status == 429:
            kind = "quota"
            detail = detail or "Claude API status 429"
        elif process.returncode or self.is_error or self.subtype != "success":
            kind = "failed"
            detail = detail or f"Claude {self.subtype} (exit {process.returncode})"
        elif self.model_usage and model_id not in self.model_usage:
            kind = "failed"
            detail = f"requested {model_id}, modelUsage reported {', '.join(self.model_usage)}"
        elif self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=self.usage, total_cost_usd=self.total_cost_usd,
            session_id=self.session_id, detail=detail,
        )


class ClaudeAdapter(NativeAdapter):
    envelope = ClaudeEnvelope

    def command(self, call: Call) -> list[str]:
        effort = ["--effort", self.model.effort] if self.model.effort else []
        return ["claude", "--model", self.model.model_id, *effort, "--permission-mode", self.permission_mode(call),
                "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
