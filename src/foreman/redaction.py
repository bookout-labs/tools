"""Replace detected credentials in outgoing text."""

from collections.abc import Iterator
from dataclasses import dataclass
from string import ascii_letters, digits
from threading import Lock

from detect_secrets.plugins.artifactory import ArtifactoryDetector
from detect_secrets.plugins.base import BasePlugin
from detect_secrets.plugins.github_token import GitHubTokenDetector
from detect_secrets.plugins.gitlab_token import GitLabTokenDetector
from detect_secrets.plugins.high_entropy_strings import HighEntropyStringsPlugin
from detect_secrets.plugins.jwt import JwtTokenDetector
from detect_secrets.plugins.keyword import KeywordDetector, QUOTES_REQUIRED_DENYLIST_REGEX_TO_GROUP
from detect_secrets.plugins.npm import NpmDetector
from detect_secrets.plugins.private_key import PrivateKeyDetector
from detect_secrets.settings import default_settings, get_plugins


_SETTINGS_LOCK = Lock()
_VERIFY_FILTER = "detect_secrets.filters.common.is_ignored_due_to_verification_policies"
_JWT_SIGNATURE_CHARS = frozenset(ascii_letters + digits + "-_+/=")


@dataclass(frozen=True)
class _Match:
    start: int
    end: int
    kind: str
    priority: int


def _values(plugin: BasePlugin, line: str) -> Iterator[str]:
    if isinstance(plugin, HighEntropyStringsPlugin):
        with plugin.non_quoted_string_regex(is_exact_match=False):
            for candidate in plugin.analyze_line("outgoing-text", line):
                if candidate.secret_value:
                    yield candidate.secret_value
    elif isinstance(plugin, (GitHubTokenDetector, GitLabTokenDetector)):
        for pattern in plugin.denylist:
            for match in pattern.finditer(line):
                yield match.group()
    elif isinstance(plugin, ArtifactoryDetector):
        for secret in plugin.analyze_string(line):
            yield secret.strip().strip('=:"').rstrip()
    elif isinstance(plugin, KeywordDetector):
        for pattern, group in QUOTES_REQUIRED_DENYLIST_REGEX_TO_GROUP.items():
            for match in pattern.finditer(line):
                yield match.group(group)
    elif isinstance(plugin, NpmDetector):
        for secret in plugin.analyze_string(line):
            yield secret.split(maxsplit=1)[0].partition('"')[0].partition("'")[0].rstrip("`")
    else:
        yield from plugin.analyze_string(line)


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
    # Without a footer, stop at the end of the quoted value or Markdown block.
    boundaries = (text.find(delimiter, end) for delimiter in ('"', "'", "\n```"))
    return start, min((position for position in boundaries if position != -1), default=len(text))


def _matches(text: str, plugins: list[BasePlugin]) -> Iterator[_Match]:
    offset = 0
    for line in text.splitlines(keepends=True):
        for plugin in plugins:
            priority = 0
            if isinstance(plugin, KeywordDetector):
                priority = 1
            elif isinstance(plugin, HighEntropyStringsPlugin):
                priority = 2
            for value in set(_values(plugin, line)) - {""}:
                start = line.find(value)
                while start != -1:
                    first, end = offset + start, offset + start + len(value)
                    if isinstance(plugin, PrivateKeyDetector):
                        first, end = _private_key_span(text, first, value)
                    elif isinstance(plugin, JwtTokenDetector):
                        if text.startswith(".", end):
                            end += 1
                        if text[end - 1] == ".":
                            while end < len(text) and text[end] in _JWT_SIGNATURE_CHARS:
                                end += 1
                    yield _Match(first, end, plugin.secret_type, priority)
                    start = line.find(value, start + len(value))
        offset += len(line)


def redact(text: str) -> str:
    """Replace overlapping detections with one marker, preferring provider-specific kinds."""
    with _SETTINGS_LOCK, default_settings() as settings:
        settings.disable_filters(_VERIFY_FILTER)
        matches = sorted(_matches(text, get_plugins()), key=lambda match: (match.start, match.end, match.kind))
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
