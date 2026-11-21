from subprocess import CompletedProcess
from typing import Literal

from pydantic import Field

from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, Usage, build_outcome


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
        quota = self.is_error and self.api_error_status == 429
        failed = process.returncode != 0 or self.is_error or self.subtype != "success"
        detail = "\n".join(self.errors) or self.result or process.stderr.strip()
        if failed and not detail:
            detail = (
                f"Claude API status {self.api_error_status}" if self.api_error_status is not None
                else f"Claude {self.subtype} (exit {process.returncode})"
            )
        return build_outcome(
            model_id, output=self.structured_output, model_usage=self.model_usage,
            usage=self.usage, total_cost_usd=self.total_cost_usd, session_id=self.session_id,
            detail=detail, failed=failed, quota=quota,
        )


class ClaudeAdapter(NativeAdapter):
    envelope = ClaudeEnvelope

    def command(self, call: Call) -> list[str]:
        effort = ["--effort", self.model.effort] if self.model.effort else []
        return ["claude", "--model", self.model.model_id, *effort, "--permission-mode", call.permission_mode,
                "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
