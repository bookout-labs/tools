from subprocess import CompletedProcess

from pydantic import Field

from foreman.adapters import Call, NativeAdapter, NativeEnvelope, Outcome, Usage, build_outcome

# Grok Build 1.0.89 flattens ACP rate-limit errors to the headless JSON message.
# These are the three fallback messages in sampling/error.rs and the vendor's free-usage code.
QUOTA_MESSAGES = {
    "You’ve hit the rate limit for your plan. Upgrade your account or try again later.",
    "You’ve hit your team’s API rate limit. Ask a team admin to purchase more credits for higher limits, "
    "or try again later. See https://docs.x.ai/developers/rate-limits#rate-limit-tiers",
    "You’ve reached your free Grok Build usage limit for now. Get SuperGrok for much higher limits, "
    "or try again later: https://grok.com/supergrok?referrer=grok-build",
}
FREE_USAGE_CODE = "subscription:free-usage-exhausted"


class GrokEnvelope(NativeEnvelope):
    type: str | None = None
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
        quota = self.type == "error" and (self.message in QUOTA_MESSAGES or FREE_USAGE_CODE in self.message)
        failed = process.returncode != 0 or self.type == "error" or bool(self.error)
        detail = self.message or self.error or self.structured_output_error or process.stderr.strip()
        return build_outcome(
            model_id, output=self.structured_output, model_usage=self.model_usage,
            usage=None if self.usage_is_incomplete else self.usage, total_cost_usd=self.total_cost_usd,
            session_id=self.session_id, detail=detail or (f"Grok exited {process.returncode}" if failed else ""),
            failed=failed, quota=quota,
        )


class GrokAdapter(NativeAdapter):
    envelope = GrokEnvelope

    def command(self, call: Call) -> list[str]:
        effort = ["--reasoning-effort", self.model.effort] if self.model.effort else []
        return ["grok", "-m", self.model.model_id, *effort, "--permission-mode", call.permission_mode,
                "--no-subagents", "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]
