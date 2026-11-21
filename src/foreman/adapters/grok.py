from pathlib import Path
from subprocess import CompletedProcess

from pydantic import AliasChoices, Field

from foreman.adapters import Call, Envelope, Outcome, read_envelope


class GrokEnvelope(Envelope):
    output: dict[str, object] | None = Field(default=None, alias="structuredOutput")
    session_id: str | None = Field(default=None, alias="sessionId")
    message: str = Field(default="", validation_alias=AliasChoices("message", "structuredOutputError"))


class GrokAdapter:
    def __init__(self, model: str):
        self.model = model

    def command(self, call: Call) -> list[str]:
        mode = "acceptEdits" if call.edit else "plan"
        return ["grok", "-m", self.model, "--permission-mode", mode, "--no-subagents",
                "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]

    def read(self, process: CompletedProcess[str], out_path: Path | None = None) -> Outcome:
        return read_envelope(process, GrokEnvelope, self.model)
