from subprocess import CompletedProcess
from typing import Literal

from pydantic import Field
from typing_extensions import override

from foreman.grant import Editing
from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, OutcomeKind, build_outcome

# Grok Build 1.0.89 emits these exact quota messages from sampling/error.rs.
# Other server text has no rate-limit code in headless JSON and is treated as a failure.
QUOTA_MESSAGES = {
    "You’ve hit the rate limit for your plan. Upgrade your account or try again later.",
    "You’ve hit your team’s API rate limit. Ask a team admin to purchase more credits for higher limits, "
    "or try again later. See https://docs.x.ai/developers/rate-limits#rate-limit-tiers",
    "You’ve reached your free Grok Build usage limit for now. Get SuperGrok for much higher limits, "
    "or try again later: https://grok.com/supergrok?referrer=grok-build",
}


class GrokEnvelope(NativeEnvelope):
    type: Literal["error"] | None = None
    stop_reason: str | None = Field(default=None, alias="stopReason")
    message: str = ""
    error: str = ""
    structured_output: dict[str, object] | None = Field(default=None, alias="structuredOutput")
    structured_output_error: str = Field(default="", alias="structuredOutputError")
    session_id: str | None = Field(default=None, alias="sessionId")
    usage_is_incomplete: bool = False

    @override
    def outcome(self, process: CompletedProcess[str], model_id: str) -> Outcome:
        kind: OutcomeKind = "ran"
        detail = self.message or self.error or self.structured_output_error or process.stderr.strip()
        if self.type == "error" and self.message in QUOTA_MESSAGES:
            kind = "quota"
        elif process.returncode or self.type == "error" or self.error:
            kind = "failed"
            detail = detail or f"Grok exited {process.returncode}"
        elif self.stop_reason != "end_turn":
            kind = "failed"
            detail = detail or f"Grok stopReason: {self.stop_reason or 'missing'}"
        # Grok validates -m. Its catalog and usage ledger can use different IDs.
        elif not self.model_usage:
            kind = "failed"
            detail = "modelUsage missing; cannot confirm a model ran"
        elif self.structured_output is None:
            kind = "failed"
            detail = detail or "missing structured output"
        return build_outcome(
            kind, output=self.structured_output, usage=None if self.usage_is_incomplete else self.read_usage(),
            session_id=self.session_id, detail=detail,
        )


class GrokAdapter(NativeAdapter):
    envelope = GrokEnvelope

    def command(self, call: Call) -> list[str]:
        effort = ["--reasoning-effort", self.model.effort] if self.model.effort else []
        return ["grok", "-m", self.model.model_id, *effort, "--permission-mode", ("acceptEdits" if isinstance(call.grant, Editing) else "plan"),
                "--no-subagents", "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
