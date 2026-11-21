"""Replace detected credentials in outgoing text."""

from collections.abc import Iterator
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


def _private_key_span(text: str, start: int, header: str) -> tuple[int, int]:
    end = start + len(header)
    if text[max(0, start - 5):start] == "-----" and text.startswith("-----", end):
        start -= 5
        footer = "END " + header.removeprefix("BEGIN ")
        close = text.find(footer, end)
        if close != -1:
            end = close + len(footer)
            if text.startswith("-----", end):
                end += 5
            return start, end
    elif not (header == "PuTTY-User-Key-File-2" and text.startswith(":", end)):
        return start, end
    for line in text[end:].splitlines(keepends=True):
        if not line.strip():
            return start, len(text[:end].rstrip("\r\n"))
        end += len(line)
    return start, end


def _matches(text: str, plugins: list[BasePlugin]) -> Iterator[_Match]:
    for match in re.finditer(r"\b(?:ghs_\d+_[A-Za-z0-9_.-]+|gh[pousr]_[A-Za-z0-9]+)", text):
        yield _Match(*match.span(), "GitHub Token", 0)
    for pattern in (r"(?i)authorization\s*:\s*(?:bearer|basic)\s+[^\s\"']+",
                    r"https://[^/\s]*@", r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"):
        for match in re.finditer(pattern, text):
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
            "GitHub Token", "JSON Web Token", "Credential", "Private Key", "Basic Auth Credentials",
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
