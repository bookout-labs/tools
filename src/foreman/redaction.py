"""Secret and PII redaction for public GitHub outputs, logs, and transcripts."""

from __future__ import annotations

import json
import os
import re
from typing import Iterable, Mapping
from urllib.parse import quote, quote_plus

MAX_SECRET_BYTES = 1024 * 1024
MAX_SECRET_COUNT = 4096
MIN_SECRET_LENGTH = 6

SENSITIVE_ENV_MARKERS = (
    "KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "AUTH",
    "CREDENTIAL",
    "PRIVATE",
    "DSN",
    "DATABASE_URL",
    "CONNECTION_STRING",
)

SAFE_ENV_NAMES = {
    "DISPLAY",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOGNAME",
    "OLDPWD",
    "PATH",
    "PWD",
    "SHELL",
    "TASK_ID",
    "TERM",
    "TMPDIR",
    "USER",
    "VIRTUAL_ENV",
}

TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED]"),
    (re.compile(r"\bsk-(?:proj-|ant-|live-|test-)?[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bxai-[A-Za-z0-9_-]{16,}\b"), "[REDACTED]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[REDACTED]"),
    (re.compile(r"(?i)(\bAuthorization:\s*Bearer\s+|Bearer\s+)[A-Za-z0-9._~+/=-]{12,}"), r"\1[REDACTED]"),
    (
        re.compile(r"\b(postgres(?:ql)?|mysql|redis|amqps?|mongodb(?:\+srv)?)://[^:\s/@]+:[^@\s/]+@"),
        r"\1://[REDACTED]@",
    ),
)

EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?<![0-9a-fA-F-])(?:\+1[-.\s]?)?(?:\(\d{3}\)[-.\s]?|\b\d{3}[-.\s])\d{3}[-.\s]\d{4}\b(?![0-9a-fA-F-])"
)


def _is_sensitive_env_name(name: str) -> bool:
    upper = name.upper()
    if upper in SAFE_ENV_NAMES or upper.startswith("XDG_"):
        return False
    return any(marker in upper for marker in SENSITIVE_ENV_MARKERS)


class SecretRedactor:
    """Scrubs registered secret variants, API tokens, DB credentials, and optional PII."""

    def __init__(self) -> None:
        self._secrets: set[str] = set()
        self._bytes = 0
        self._overflow = False
        self._compiled: re.Pattern[str] | None = None

    def add(self, value: str | None) -> None:
        if not isinstance(value, str) or len(value) < MIN_SECRET_LENGTH:
            return
        if len(value) > MAX_SECRET_BYTES:
            self._overflow = True
            self._compiled = None
            return
        variants = {
            value,
            quote_plus(value),
            quote(value, safe=""),
            json.dumps(value)[1:-1],
        }
        for line in value.splitlines():
            stripped = line.strip()
            if len(stripped) >= MIN_SECRET_LENGTH:
                variants.add(stripped)
        added = False
        for item in variants:
            if len(item) < MIN_SECRET_LENGTH or item in self._secrets:
                continue
            if len(self._secrets) >= MAX_SECRET_COUNT or self._bytes + len(item) > MAX_SECRET_BYTES:
                self._overflow = True
                self._compiled = None
                return
            self._secrets.add(item)
            self._bytes += len(item)
            added = True
        if added:
            self._compiled = None

    def add_env(self, name: str, value: str | None) -> None:
        if not isinstance(value, str):
            return
        stripped = value.strip()
        if not stripped or stripped.lower() in {"true", "false"}:
            return
        try:
            float(stripped)
        except ValueError:
            pass
        else:
            return
        if _is_sensitive_env_name(name):
            self.add(stripped)

    @classmethod
    def from_environ(
        cls,
        env: Mapping[str, str] | None = None,
        extra_secrets: Iterable[str] = (),
    ) -> "SecretRedactor":
        redactor = cls()
        for key, value in (os.environ if env is None else env).items():
            redactor.add_env(key, value)
        for secret in extra_secrets:
            redactor.add(secret)
        return redactor

    def _pattern(self) -> re.Pattern[str] | None:
        if self._compiled is None and self._secrets:
            ordered = sorted(self._secrets, key=len, reverse=True)
            self._compiled = re.compile("|".join(re.escape(item) for item in ordered))
        return self._compiled

    def redact(self, text: object, *, redact_pii: bool = False) -> str:
        if text is None:
            return ""
        value = str(text)
        if not value:
            return ""
        if self._overflow:
            return "[REDACTED]"
        pattern = self._pattern()
        if pattern is not None:
            value = pattern.sub("[REDACTED]", value)
        for token_re, replacement in TOKEN_PATTERNS:
            value = token_re.sub(replacement, value)
        if redact_pii:
            value = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", value)
            value = PHONE_PATTERN.sub("[REDACTED_PHONE]", value)
        return value


def redact_text(
    text: object,
    *,
    extra_secrets: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    redact_pii: bool = False,
) -> str:
    """Convenience one-shot redaction using environment secrets and standard patterns."""
    return SecretRedactor.from_environ(env=env, extra_secrets=extra_secrets).redact(
        text, redact_pii=redact_pii
    )


def redact_value(
    value: object,
    *,
    extra_secrets: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    redact_pii: bool = False,
) -> object:
    """Scrub nested strings, including string dictionary keys, while preserving container types."""
    redactor = SecretRedactor.from_environ(env=env, extra_secrets=extra_secrets)

    def scrub(item: object) -> object:
        if isinstance(item, str):
            return redactor.redact(item, redact_pii=redact_pii)
        if isinstance(item, list):
            return [scrub(element) for element in item]
        if isinstance(item, tuple):
            return tuple(scrub(element) for element in item)
        if isinstance(item, dict):
            return {scrub(key): scrub(element) for key, element in item.items()}
        return item

    return scrub(value)
