"""Per-command App authentication for factory network commands."""

import argparse
import os
import re
import subprocess
import sys

from foreman.credentials import (COMMAND_TIMEOUT, CredentialFailure, UNAVAILABLE, command_environment,
                                 credentials, git_auth, normalize_remote_url, validate_remote)
from foreman.redaction import redact

REPO = "bookout-labs/tools"


def environment(studio: str | None) -> dict[str, str]:
    if studio is None:
        env = credentials.environment(REPO)
        if env is None:
            raise CredentialFailure("Configure the Studio App IDs and key before using the launcher.")
        return env
    try:
        result = subprocess.run(
            ["ssh", "-T", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", "-oClearAllForwardings=yes",
             "-oConnectTimeout=10", studio, REPO], capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CredentialFailure(UNAVAILABLE) from None
    if result.returncode:
        if result.returncode == 255:
            raise CredentialFailure(UNAVAILABLE)
        raise CredentialFailure(redact(result.stderr.strip(), credentials_only=True) or "Studio credential endpoint failed.")
    if not re.fullmatch(r"password=[^\s]+\n\n", result.stdout):
        raise CredentialFailure("Studio credential endpoint returned an invalid response.")
    return command_environment(result.stdout.removeprefix("password=").strip())


def validate_config(url: str) -> None:
    config = subprocess.run(["git", "config", "--list", "--includes", "--show-scope", "--null"],
                            env=command_environment(), capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
    if config.returncode:
        raise CredentialFailure("Cannot inspect repository Git config.")
    entries = config.stdout.split("\0")
    for scope, entry in zip(entries[::2], entries[1::2]):
        key, _, value = entry.partition("\n")
        if scope in {"local", "worktree"} and not (
                re.fullmatch(r"core\.(repositoryformatversion|filemode|bare|logallrefupdates|ignorecase|precomposeunicode)"
                             r"|remote\.origin\.fetch|branch\..*\.(remote|merge|gh-merge-base|t3-merge-base|t3-merge-base-ref)", key.lower()) or
                (key.lower() == "remote.origin.url" and normalize_remote_url(value) == url)):
            raise CredentialFailure(f"Worker Git refuses repository config key {redact(key, credentials_only=True)}.")


def git_command(args: list[str]) -> list[str]:
    if not args or args[0] not in {"fetch", "push", "ls-remote", "clone"}:
        raise argparse.ArgumentTypeError("Use the launcher only for git fetch, push, ls-remote or clone. Run local git directly.")
    if len(args) < 2 or any(value.startswith("-") for value in args[1:]):
        raise argparse.ArgumentTypeError("Worker git commands require a destination and accept no options.")
    url = f"https://github.com/{REPO}"
    if args[0] == "clone":
        if len(args) not in {2, 3} or args[1] != url:
            raise argparse.ArgumentTypeError(f"Use git clone {url} [DIRECTORY].")
    else:
        if args[1] != "origin":
            raise argparse.ArgumentTypeError("Worker network commands require origin.")
        if args[0] == "push":
            if len(args) != 3:
                raise argparse.ArgumentTypeError("Use git push origin HEAD:BRANCH or BRANCH.")
            branch = args[2].removeprefix("HEAD:").removeprefix("refs/heads/")
            if (branch == "main" or branch.startswith("refs/") or
                    not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", branch) or
                    any(not part or part.startswith(".") or part.endswith((".", ".lock")) for part in branch.split("/")) or
                    ".." in branch or "//" in branch):
                raise argparse.ArgumentTypeError("Push requires a non-main branch under refs/heads, without force, deletion or tags.")
            # Fully qualify both ends so a same-named tag cannot supply the source.
            args = ["push", "origin", f"{'HEAD' if args[2].startswith('HEAD:') else 'refs/heads/' + branch}:refs/heads/{branch}"]
    validate_config(url)
    urls = validate_remote(REPO, urls=[url] if args[0] == "clone" else None)
    return ["git", *git_auth(urls), "-c", "push.followTags=false", "-c", "core.hooksPath=/dev/null",
            "-c", "http.proxy=", "-c", "http.sslVerify=true", "-c", "core.fsmonitor=false",
            "-c", "fetch.recurseSubmodules=false", "-c", "push.recurseSubmodules=no", "-c", "submodule.recurse=false", *args]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--studio", help="SSH host alias for the restricted Studio credential endpoint")
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    operation = "worker command"
    try:
        if options.serve:
            if options.command or options.studio or os.environ.get("SSH_ORIGINAL_COMMAND") != REPO:
                parser.error("Restricted credential request denied.")
            env = environment(None)
            sys.stdout.write(f"password={env['GH_TOKEN']}\n\n")
            return 0
        if options.studio and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", options.studio):
            parser.error("Use a configured SSH host alias for Studio.")
        command = options.command
        if not command or command[0] not in {"gh", "git"}:
            parser.error("Worker launcher requires gh or git.")
        operation = " ".join(command[:2])
        if command[0] == "git":
            command = git_command(command[1:])
        elif len(command) < 2 or command[1] not in {"pr", "issue", "run", "repo", "label"}:
            parser.error("Unsupported gh command. Use pr, issue, run, repo or label.")
        env = environment(options.studio)
        env["GH_REPO"] = REPO
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=COMMAND_TIMEOUT)
        for stream, value in ((sys.stdout, result.stdout), (sys.stderr, result.stderr)):
            stream.write(redact(value.replace(env['GH_TOKEN'], "[redacted:Credential]"), credentials_only=True))
        return result.returncode
    except argparse.ArgumentTypeError as exc:
        parser.error(str(exc))
    except CredentialFailure as exc:
        sys.stderr.write(str(exc) + "\n")
    except subprocess.TimeoutExpired as exc:
        sys.stderr.write(f"{operation} timed out after {exc.timeout} seconds; inspect receipts before retrying.\n")
    except (OSError, subprocess.SubprocessError, UnicodeError):
        sys.stderr.write(f"{operation} could not execute. Check the command installation and output encoding.\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
