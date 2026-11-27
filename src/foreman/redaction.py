"""Replace detected credentials in outgoing text."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from string import ascii_letters, digits
from threading import Lock
import re

from detect_secrets.plugins.base import BasePlugin, RegexBasedDetector
from detect_secrets.plugins.high_entropy_strings import HighEntropyStringsPlugin
from detect_secrets.plugins.jwt import JwtTokenDetector
from detect_secrets.plugins.keyword import KeywordDetector, QUOTES_REQUIRED_DENYLIST_REGEX_TO_GROUP
from detect_secrets.plugins.private_key import PrivateKeyDetector
from detect_secrets.settings import default_settings, get_plugins


_SETTINGS_LOCK = Lock()
# The exact test values this process supplied to an attempt container (contract secrets). Each line of a value is one entry,
# because output is redacted line by line as it streams.
_TEST_VALUES: set[str] = set()
_JWT_SIGNATURE_CHARS = frozenset(ascii_letters + digits + "-_+/=")


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    kind: str
    priority: int


def _keyword_spans(plugin: KeywordDetector, line: str) -> Iterator[tuple[int, int]]:
    if plugin.keyword_exclude and plugin.keyword_exclude.search(line):
        return
    for pattern, group in QUOTES_REQUIRED_DENYLIST_REGEX_TO_GROUP.items():
        for match in pattern.finditer(line):
            if match.group(group).strip():
                yield match.span(group)


def _spans(plugin: BasePlugin, line: str) -> Iterator[tuple[int, int]]:
    if isinstance(plugin, RegexBasedDetector):
        for pattern in plugin.denylist:
            for match in pattern.finditer(line):
                yield match.span()
    elif isinstance(plugin, HighEntropyStringsPlugin):
        with plugin.non_quoted_string_regex(is_exact_match=False):
            for match in plugin.regex.finditer(line):
                if plugin.calculate_shannon_entropy(match.group()) > plugin.entropy_limit:
                    yield match.span()
    elif isinstance(plugin, KeywordDetector):
        yield from _keyword_spans(plugin, line)
    else:
        raise TypeError(f"No redaction matcher for {type(plugin).__name__}")


class PrivateKeyBlock:
    """Frame streamed key blocks without retaining their contents."""

    def __init__(self) -> None:
        self.footer: str | None = None

    def __call__(self, line: str) -> str:
        if self.footer is not None:
            if not self.footer and not line.strip():
                self.footer = None
                return line
            if self.footer and self.footer in line:
                self.footer = None
            return ""
        for start, end in _spans(PrivateKeyDetector(), line):
            header = line[start:end]
            if line[max(0, start - 5):start] == "-----" and line.startswith("-----", end):
                start -= 5
                footer = "-----END " + header.removeprefix("BEGIN ") + "-----"
                self.footer = None if footer in line[end:] else footer
            elif header == "PuTTY-User-Key-File-2" and line.startswith(":", end):
                self.footer = ""
            else:
                continue
            return line[:start] + "[redacted:Private Key]" + ("\n" if line.endswith("\n") else "")
        return line


def _private_key_span(text: str, start: int, header: str) -> tuple[int, int]:
    block = PrivateKeyBlock()
    first = max(0, start - 5) if text[max(0, start - 5):start] == "-----" else start
    end = first
    for line in text[first:].splitlines(keepends=True):
        clean = block(line)
        if end == first and clean == line:
            return start, start + len(header)
        if end != first and clean:
            break
        end += len(line)
        if end == first + len(line) and block.footer and block.footer not in text[end:]:
            block.footer = ""  # Complete text can recover after an unterminated key at a blank line.
        if block.footer is None:
            break
    return first, len(text[:end].rstrip("\r\n"))


def register_test_values(values: Iterable[str]) -> None:
    """Redact each of these values, exactly, from every text this process writes from now on."""
    with _SETTINGS_LOCK:
        _TEST_VALUES.update(line for value in values for line in value.splitlines() if line.strip())


def _matches(text: str, plugins: list[BasePlugin]) -> Iterator[_Match]:
    for value in _TEST_VALUES:
        start = text.find(value)
        while start != -1:
            yield _Match(start, start + len(value), "Test Value", 0)
            start = text.find(value, start + len(value))
    for match in re.finditer(r"\b(?:ghs_\d+_[A-Za-z0-9_.-]+|gh[pousr]_[A-Za-z0-9]+)", text):
        yield _Match(*match.span(), "GitHub Token", 0)
    for match in re.finditer(r"(?i)authorization\s*:\s*(?:bearer|basic)\s+[^\s\"']+", text):
        yield _Match(*match.span(), "Credential", 1)
    offset = 0
    for raw_line in text.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        for plugin in plugins:
            priority = 0
            if isinstance(plugin, KeywordDetector):
                priority = 1
            elif isinstance(plugin, HighEntropyStringsPlugin):
                priority = 2
            for start, stop in _spans(plugin, line):
                first, end = offset + start, offset + stop
                if isinstance(plugin, PrivateKeyDetector):
                    first, end = _private_key_span(text, first, line[start:stop])
                elif isinstance(plugin, JwtTokenDetector):
                    if text.startswith(".", end):
                        end += 1
                    if text[end - 1] == ".":
                        while end < len(text) and text[end] in _JWT_SIGNATURE_CHARS:
                            end += 1
                yield _Match(first, end, plugin.secret_type, priority)
        offset += len(raw_line)


def redact(text: str, *, credentials_only: bool = False) -> str:
    """Replace overlapping detections with one marker, preferring provider-specific kinds."""
    with _SETTINGS_LOCK, default_settings():
        matches = sorted(_matches(text, get_plugins()), key=lambda match: (match.start, match.end, match.kind))
    if credentials_only:
        matches = [match for match in matches if match.kind in {
            "GitHub Token", "JSON Web Token", "Credential", "Private Key", "Basic Auth Credentials", "Test Value",
        }]
    parts: list[str] = []
    cursor = 0
    index = 0
    while index < len(matches):
        match = matches[index]
        start, end = match.start, match.end
        index += 1
        while index < len(matches) and matches[index].start < end:
            other = matches[index]
            end = max(end, other.end)
            if (other.priority, other.start - other.end) < (match.priority, match.start - match.end):
                match = other
            index += 1
        parts.extend((text[cursor:start], f"[redacted:{match.kind}]"))
        cursor = end
    parts.append(text[cursor:])
    return "".join(parts)
