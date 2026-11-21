"""Studio-owned, repository-scoped GitHub App credentials."""

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jwt
from pydantic import BaseModel, SecretStr


class CredentialFailure(RuntimeError):
    """Authentication stopped before any personal-account fallback."""


COMMAND_TIMEOUT = 30
TOKEN_MARGIN = COMMAND_TIMEOUT + 30
UNAVAILABLE = "App credential unavailable. Check Studio connectivity and key configuration, then resume."


def failure(status: int, rate_limited: bool = False) -> str:
    kinds = {401: "unauthorized", 403: "permission denied", 404: "installation or repository scope denied",
             422: "installation or repository scope denied", 429: "rate limit"}
    kind = "rate limit" if rate_limited and status in {403, 429} else kinds.get(status)
    return UNAVAILABLE if kind is None else f"App credentials: {kind}. Restore App access, then resume the same run."


class _Token(BaseModel):
    token: SecretStr
    expires_at: datetime


@dataclass
class _Entry:
    lock: Lock = field(default_factory=Lock)
    token: _Token | None = None


class AppCredentials:
    def __init__(self) -> None:
        self._lock = Lock()
        self._entries: dict[tuple[str, str, str, str], _Entry] = {}

    @property
    def configured(self) -> bool:
        return bool(os.environ.get("FOREMAN_APP_ID") or os.environ.get("FOREMAN_INSTALLATION_ID"))

    def invalidate(self) -> None:
        with self._lock:
            self._entries.clear()

    def environment(self, repo: str) -> dict[str, str] | None:
        app = os.environ.get("FOREMAN_APP_ID", "")
        installation = os.environ.get("FOREMAN_INSTALLATION_ID", "")
        if not self.configured:
            return None
        if not app.isdecimal() or not installation.isdecimal():
            raise CredentialFailure("Configure FOREMAN_APP_ID and FOREMAN_INSTALLATION_ID, then resume.")
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise CredentialFailure("App credentials require an authorized OWNER/NAME repository.")
        key_path = os.environ.get("FOREMAN_APP_KEY_PATH", "~/.config/bookout-foreman/app.pem")
        cache_key = (app, installation, key_path, repo.lower())
        with self._lock:
            entry = self._entries.setdefault(cache_key, _Entry())
        with entry.lock:
            token = entry.token
            if token is None or token.expires_at.timestamp() <= time.time() + TOKEN_MARGIN:
                try:
                    key = Path(key_path).expanduser()
                    if key.stat().st_mode & 0o077:
                        raise CredentialFailure("App key must have owner-only permissions. Repair and resume.")
                    now = int(time.time())
                    signed = jwt.encode({"iat": now - 60, "exp": now + 540, "iss": app}, key.read_bytes(), algorithm="RS256")
                    request = Request(
                        f"https://api.github.com/app/installations/{installation}/access_tokens",
                        data=json.dumps({"repositories": [repo.split('/')[1]]}).encode(),
                        headers={"Authorization": f"Bearer {signed}", "Accept": "application/vnd.github+json"},
                    )
                    with urlopen(request, timeout=15) as response:
                        token = _Token.model_validate_json(response.read())
                    if token.expires_at.timestamp() <= time.time() + TOKEN_MARGIN:
                        raise CredentialFailure("App returned an expired credential. Restore App access and resume.")
                    entry.token = token
                except CredentialFailure:
                    raise
                except HTTPError as exc:
                    raise CredentialFailure(failure(exc.code, exc.headers.get("X-RateLimit-Remaining") == "0" or bool(exc.headers.get("Retry-After")))) from None
                except Exception:
                    raise CredentialFailure(UNAVAILABLE) from None
        return command_environment(token.token.get_secret_value())


credentials = AppCredentials()


GIT_AUTH = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "credential.helper=", "-c", 'credential.https://github.com.helper=!f() { if [ "$1" = get ]; then printf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"; fi; }; f',
            "-c", "http.extraHeader=", "-c", "http.https://github.com/.extraHeader=", "-c", "http.followRedirects=false", "-c", "core.askPass=")


def command_environment(token: str | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k in {"PATH", "HOME", "LANG", "TMPDIR"} or k.startswith("LC_")}
    env.update(GH_HOST="github.com", GH_PROMPT_DISABLED="1",
               GH_PAGER="cat", PAGER="cat", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="https", GIT_ATTR_NOSYSTEM="1")
    if token is not None:
        env["GH_TOKEN"] = token
    return env


def validate_remote(repo: str, cwd: Path | None = None, *, app: bool = True,
                    urls: list[str] | None = None) -> list[str]:
    env = command_environment() if app else None
    expected = {f"https://github.com/{repo}"}
    if not app:
        expected.update({f"git@github.com:{repo}", f"ssh://git@github.com/{repo}"})
    if urls is None:
        urls = []
        for options in ([], ["--push"]):
            result = subprocess.run(["git", "remote", "get-url", *options, "--all", "origin"],
                                    cwd=cwd, env=env, capture_output=True, text=True, timeout=10)
            candidates = os.fsdecode(result.stdout).splitlines()
            if result.returncode or not candidates:
                raise CredentialFailure("Cannot inspect origin. Configure the allowed GitHub repository.")
            urls.extend(candidates)
    for candidate in urls:
        resolved = subprocess.run(["git", "ls-remote", "--get-url", candidate], cwd=cwd,
                                  env=env, capture_output=True, text=True, timeout=10)
        if resolved.returncode or os.fsdecode(resolved.stdout).strip().removesuffix(".git") not in expected:
            raise CredentialFailure("Transport requires the selected token-free GitHub repository.")
    return urls


def git_auth(urls: list[str]) -> tuple[str, ...]:
    return (*GIT_AUTH, *(part for url in sorted(set(urls)) for part in ("-c", f"http.{url}.extraHeader=")))
