"""Per-command App authentication for local and SSH-connected factory workers."""

import argparse
import os
import re
import subprocess
import sys

from foreman.credentials import COMMAND_TIMEOUT, CredentialFailure, GIT_AUTH, UNAVAILABLE, credentials
from foreman.redaction import redact

ALLOWED_REPOS = frozenset({"bookout-labs/tools"})


def environment(repo: str, studio: str | None) -> dict[str, str]:
    if repo not in ALLOWED_REPOS:
        raise CredentialFailure("Worker repository is not allowed.")
    if studio is None:
        env = credentials.environment(repo)
        if env is None:
            raise CredentialFailure(UNAVAILABLE)
        return env
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", studio):
        raise CredentialFailure("Use a configured SSH host alias for Studio.")
    result = subprocess.run(
        ["ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oClearAllForwardings=yes",
         "-oConnectTimeout=10", studio, repo], capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
    )
    if result.returncode or not re.fullmatch(r"password=[^\s]+\n\n", result.stdout):
        raise CredentialFailure(UNAVAILABLE)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_TRACE", "GIT_CURL_VERBOSE", "GH_DEBUG"))}
    env.update(GH_TOKEN=result.stdout.removeprefix("password=").strip(), GH_HOST="github.com", GH_PROMPT_DISABLED="1")
    return env


def git_command(repo: str, args: list[str], env: dict[str, str]) -> list[str]:
    if not args:
        raise CredentialFailure("Specify a git command.")
    url = f"https://github.com/{repo}"
    command = args[0]
    if command in {"fetch", "push", "ls-remote", "clone"}:
        if len(args) < 2 or any(value.startswith("-") for value in args[2:]):
            raise CredentialFailure("Use git COMMAND origin REF, or git clone HTTPS_URL DIRECTORY, without options.")
        if command == "clone":
            urls = [args[1]]
        else:
            if args[1] != "origin":
                raise CredentialFailure("Worker network commands require origin.")
            urls: list[str] = []
            for options in ([], ["--push"]):
                result = subprocess.run(["git", "remote", "get-url", *options, "--all", "origin"],
                                        env=env, capture_output=True, text=True, timeout=10)
                if result.returncode:
                    raise CredentialFailure("Cannot inspect origin. Configure the allowed HTTPS repository.")
                urls.extend(result.stdout.splitlines())
        for candidate in urls:
            result = subprocess.run(["git", "ls-remote", "--get-url", candidate], env=env,
                                    capture_output=True, text=True, timeout=10)
            if result.returncode or result.stdout.strip().removesuffix(".git") != url:
                raise CredentialFailure("Worker transport requires the allowed token-free HTTPS repository.")
        if not urls:
            raise CredentialFailure("Missing origin URL.")
        auth = [*GIT_AUTH, *(part for value in set(urls) for part in ("-c", f"http.{value}.extraHeader="))]
    elif command in {"status", "diff", "log", "show", "add", "commit", "switch", "branch", "rev-parse", "merge", "cherry-pick"}:
        auth = list(GIT_AUTH)
    else:
        raise CredentialFailure("Unsupported worker git command.")
    return ["git", *auth, *args]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", choices=sorted(ALLOWED_REPOS), default="bookout-labs/tools")
    parser.add_argument("--studio", help="SSH host alias for the restricted Studio credential endpoint")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    try:
        if options.serve:
            if options.command or options.studio or os.environ.get("SSH_ORIGINAL_COMMAND") != options.repo:
                raise CredentialFailure("Restricted credential request denied.")
            env = environment(options.repo, None)
            sys.stdout.write(f"password={env['GH_TOKEN']}\n\n")
            return 0
        if not options.command or options.command[0] not in {"gh", "git"}:
            raise CredentialFailure("Worker launcher requires gh or git.")
        env = environment(options.repo, options.studio)
        env = {k: v for k, v in env.items() if not k.startswith("GIT_CONFIG")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   GIT_ALLOW_PROTOCOL="https", GH_REPO=options.repo, GH_PAGER="cat", PAGER="cat")
        command = options.command
        if command[0] == "git":
            command = git_command(options.repo, command[1:], env)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
        for stream, value in ((sys.stdout, result.stdout), (sys.stderr, result.stderr)):
            stream.write(redact(value.replace(env['GH_TOKEN'], "[redacted:Credential]"), credentials_only=True))
        return result.returncode
    except (CredentialFailure, OSError, subprocess.SubprocessError, UnicodeError):
        sys.stderr.write(UNAVAILABLE + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
