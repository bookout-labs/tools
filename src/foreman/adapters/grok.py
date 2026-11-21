from subprocess import CompletedProcess
from typing import Literal

from pydantic import Field

from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, Usage, build_outcome

# Grok Build 1.0.89 emits these exact quota messages from sampling/error.rs.
# Other server text has no rate-limit code in headless JSON and is treated as a failure.
QUOTA_MESSAGES = {
    "You’ve hit the rate limit for your plan. Upgrade your account or try again later.",
    "You’ve hit your team’s API rate limit. Ask a team admin to purchase more credits for higher limits, "
    "or try again later. See https://docs.x.ai/developers/rate-limits#rate-limit-tiers",
    "You’ve reached your free Grok Build usage limit for now. Get SuperGrok for much higher limits, "
    "or try again later: https://grok.com/supergrok?referrer=grok-build",
}
# The installed CLI resolves the configured shortcut to this catalog ID in modelUsage.
REPORTED_MODEL_IDS = {"grok-4.7": "grok-4.7-build"}


class GrokEnvelope(NativeEnvelope):
    type: Literal["error"] | None = None
    stop_reason: str | None = Field(default=None, alias="stopReason")
    message: str = ""
    error: str = ""
    structured_output: dict[str, object] | None = Field(default=None, alias="structuredOutput")
    structured_output_error: str = Field(default="", alias="structuredOutputError")
    session_id: str | None = Field(default=None, alias="sessionId")
    model_usage: dict[str, object] | None = Field(default=None, alias="modelUsage")
    usage: Usage | None = None
    usage_is_incomplete: bool = False
    total_cost_usd: float | None = None

    def outcome(self, process: CompletedProcess[str], model_id: str) -> Outcome:
        kind: OutcomeKind = "ran"
        detail = self.message or self.error or self.structured_output_error or process.stderr.strip()
        reported_id = REPORTED_MODEL_IDS.get(model_id, model_id)
        if self.type == "error" and self.message in QUOTA_MESSAGES:
            kind = "quota"
        elif process.returncode or self.type == "error" or self.error:
            kind = "failed"
            detail = detail or f"Grok exited {process.returncode}"
        elif self.stop_reason != "end_turn":
            kind = "failed"
            detail = detail or f"Grok stopReason: {self.stop_reason or 'missing'}"
        elif self.model_usage and self.model_usage.keys() != {reported_id}:
            kind = "failed"
            detail = f"requested {reported_id}, modelUsage reported {', '.join(self.model_usage)}"
        elif self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=None if self.usage_is_incomplete else self.usage,
            total_cost_usd=self.total_cost_usd, session_id=self.session_id, detail=detail,
        )


class GrokAdapter(NativeAdapter):
    envelope = GrokEnvelope

    def command(self, call: Call) -> list[str]:
        effort = ["--reasoning-effort", self.model.effort] if self.model.effort else []
        return ["grok", "-m", self.model.model_id, *effort, "--permission-mode", self.permission_mode(call),
                "--no-subagents", "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
