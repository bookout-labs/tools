"""Studio-owned, repository-scoped GitHub App credentials."""

from datetime import datetime
import json
import os
from pathlib import Path
import re
from threading import Lock
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import jwt
from pydantic import BaseModel, SecretStr


class CredentialFailure(RuntimeError):
    """Authentication stopped before any personal-account fallback."""


def failure(status: int, rate_limited: bool = False) -> CredentialFailure:
    kind = "rate limit" if rate_limited or status == 429 else "unauthorized" if status == 401 else "permission denied"
    return CredentialFailure(f"App credentials: {kind}. Restore App access, then resume the same run.")


class _Token(BaseModel):
    token: SecretStr
    expires_at: datetime


class AppCredentials:
    def __init__(self) -> None:
        self._lock = Lock()
        self._tokens: dict[tuple[str, str, str, str], _Token] = {}

    def invalidate(self) -> None:
        with self._lock:
            self._tokens.clear()

    def environment(self, repo: str) -> dict[str, str] | None:
        app = os.environ.get("FOREMAN_APP_ID", "")
        installation = os.environ.get("FOREMAN_INSTALLATION_ID", "")
        if not app and not installation:
            return None
        if not app.isdecimal() or not installation.isdecimal() or not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise CredentialFailure("Configure FOREMAN_APP_ID and FOREMAN_INSTALLATION_ID, then resume.")
        key_path = os.environ.get("FOREMAN_APP_KEY_PATH", "~/.config/bookout-foreman/app.pem")
        cache_key = (app, installation, key_path, repo.lower())
        with self._lock:
            token = self._tokens.get(cache_key)
            if token is None or token.expires_at.timestamp() <= time.time() + 60:
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
                    if token.expires_at.timestamp() <= time.time() + 60:
                        raise CredentialFailure("App returned an expired credential. Restore App access and resume.")
                    self._tokens[cache_key] = token
                except CredentialFailure:
                    raise
                except HTTPError as exc:
                    raise failure(exc.code, exc.headers.get("X-RateLimit-Remaining") == "0" or bool(exc.headers.get("Retry-After"))) from None
                except Exception:
                    raise CredentialFailure("App credential unavailable. Check Studio connectivity and key configuration, then resume.") from None
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_TRACE", "GIT_CURL_VERBOSE", "GH_DEBUG"))}
        env.update(GH_TOKEN=token.token.get_secret_value(), GH_HOST="github.com", GH_PROMPT_DISABLED="1")
        return env


credentials = AppCredentials()


def github_repo(url: str) -> str:
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?", url)
    if match is None:
        raise CredentialFailure("App git transport requires a token-free https://github.com/owner/repo origin.")
    return match[1]


GIT_AUTH = ("-c", "credential.helper=", "-c", 'credential.helper=!f() { if [ "$1" = get ]; then printf "username=x-access-token\\npassword=%s\\n" "$GH_TOKEN"; fi; }; f',
            "-c", "http.extraHeader=", "-c", "http.https://github.com/.extraHeader=", "-c", "http.followRedirects=false", "-c", "core.askPass=")
