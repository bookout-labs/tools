from pathlib import Path
from subprocess import CompletedProcess

from pydantic import Field

from foreman.adapters import Call, Envelope, Outcome, read_envelope


class ClaudeEnvelope(Envelope):
    output: dict[str, object] | None = Field(default=None, alias="structured_output")
    message: str = Field(default="", alias="result")


class ClaudeAdapter:
    def __init__(self, model: str):
        self.model = model

    def command(self, call: Call) -> list[str]:
        mode = "acceptEdits" if call.edit else "plan"
        return ["claude", "--model", self.model, "--permission-mode", mode,
                "--output-format", "json", "--json-schema", call.schema, "-p", call.prompt]

    def read(self, process: CompletedProcess[str], out_path: Path | None = None) -> Outcome:
        return read_envelope(process, ClaudeEnvelope, self.model)
