"""Bounded, sanitized verifier diagnostics. Raw output is never retained."""

import os
import re

from foreman.credentials import credentials
from foreman.redaction import PrivateKeyBlock, redact
from foreman.container import CheckLog


_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[ -/]*[@-~])")
_MARKER = "[... output omitted ...]\n"


def _window(text: str, size: int, lines: int, *, tail: bool = False) -> str:
    data = text.encode("utf-8")
    text = (data[-size:] if tail else data[:size]).decode("utf-8", errors="ignore") if size else ""
    parts = text.splitlines(keepends=True)
    return "".join(parts[-lines:] if tail else parts[:lines]) if lines else ""


class CheckOutput:
    def __init__(self, log: CheckLog | None = None):
        self.log = log
        self.head = self.tail = ""
        self.omitted: bool = False
        self.key_block = PrivateKeyBlock()
        self.values = tuple(sorted({*credentials.known_values(), *(os.environ.get(name, "") for name in
                                   ("GH_TOKEN", "GITHUB_TOKEN"))} - {""}, key=len, reverse=True))

    def __call__(self, output: str) -> None:
        if self.log:
            self.log(output)
        output = _ESCAPE.sub("", output)
        output = re.sub(r"\r\n|[\r\v\f\x1c-\x1e\x85\u2028\u2029]", "\n", output)
        output = output.encode("utf-8", errors="replace").decode("utf-8")
        for line in output.splitlines(keepends=True):
            line = self.key_block(line)
            for value in self.values:
                line = line.replace(value, "[redacted:Credential]")
            clean = redact(line)
            rest = clean
            if not self.tail and not self.omitted:
                combined = self.head + clean
                self.head = _window(combined, 2048, 32)
                rest = combined[len(self.head):]
            pending = self.tail + rest
            self.tail = _window(pending, 6144, 96, tail=True)
            self.omitted = self.omitted or self.tail != pending

    @property
    def text(self) -> str:
        if not self.head and not self.tail:
            return "[no check output]"
        if not self.omitted:
            return self.head + self.tail
        head = _window(self.head, 8191 - len((self.tail + _MARKER).encode("utf-8")),
                       127 - len(self.tail.splitlines()))
        return head.rstrip("\n") + "\n" + _MARKER + self.tail
