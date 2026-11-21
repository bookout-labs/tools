"""Bounded live smoke against the public scratch repository."""

from decimal import Decimal
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import tempfile
import time

from foreman.credentials import CredentialFailure, credentials, git_auth
from foreman.delivery import LiveRun
from foreman.github import BOT_LOGIN, ForgeError, LiveForge
from foreman.redaction import redact
from foreman.readiness import MergeReceipt, MergeState
from foreman.runner import run_live_delivery
from foreman.smoke import SmokeVerdict, smoke_verdict
from foreman.usd import BudgetUsd
from foreman.worker import CliWorker
from foreman.workspace import LiveWorkspace

SMOKE_REPO = "bookout-labs/foreman-smoke"
SMOKE_TOKEN_CAP = 100_000
SMOKE_CLAUDE_USD = BudgetUsd(Decimal("2"))
SMOKE_SECONDS = 600

FIXTURE_CHECK = '''import pathlib, subprocess, sys
issue, base = sys.argv[1:]
path = f"smoke_{issue}.txt"
assert pathlib.Path(path).read_bytes() == f"smoke {issue}\\n".encode()
assert subprocess.check_output(["git", "diff", "--name-only", base, "HEAD"]).decode().splitlines() == [path]
assert subprocess.check_output(["git", "diff", "--numstat", base, "HEAD"]).decode() == f"1\\t0\\t{path}\\n"
'''


def run_smoke() -> int:
    """Provision first, then exercise the normal live runner and its watcher."""
    verdict = SmokeVerdict(78, "bootstrap", "prerequisite")
    try:
        env = credentials.environment(SMOKE_REPO)
        if env is None:
            raise CredentialFailure("Smoke requires Studio App credentials; personal credentials are not supported.")
        forge = LiveForge()
        forge.default_branch(SMOKE_REPO)
        directory = Path(tempfile.mkdtemp(prefix="foreman-smoke-"))
        root = directory / "checkout"
        subprocess.run(["git", *git_auth([f"https://github.com/{SMOKE_REPO}"]), "clone",
                        f"https://github.com/{SMOKE_REPO}", str(root)], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        subprocess.run(["python3.12", "-m", "venv", str(directory / "python")],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        os.environ["PATH"] = str(directory / "python/bin") + os.pathsep + os.environ.get("PATH", "")
        subprocess.run(["python", "-c", "import sys; assert sys.version_info[:2] == (3, 12)"],
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        script = directory / "acceptance.py"
        script.write_text(FIXTURE_CHECK)
        issue = forge.create_issue(SMOKE_REPO, "Smoke fixture", "Add smoke_<this issue number>.txt containing exactly "
                                   "smoke <this issue number> and a newline. Change no other files.", ["smoke"])
        request = f"Add smoke_{issue}.txt containing exactly 'smoke {issue}\\n'. Change no other files."
        check = "python -m unittest discover -s tests && " + shlex.join(["python", str(script), str(issue), base])
        print(f"smoke issue {issue}; tokens {SMOKE_TOKEN_CAP}; Claude ${SMOKE_CLAUDE_USD.amount}; deadline {SMOKE_SECONDS}s. "
              "Other providers have no dollar cap.", flush=True)
        verdict = SmokeVerdict(1, "live_run", "checkpoint")
        result = run_live_delivery(repo=SMOKE_REPO, issue=issue, request=request, check=check,
                                   checkpoint=directory / "checkpoint.sqlite3", dispatcher=CliWorker(root),
                                   run=LiveRun(forge, LiveWorkspace(root, SMOKE_REPO)),
                                   max_tokens=SMOKE_TOKEN_CAP, max_budget_usd=SMOKE_CLAUDE_USD,
                                   log=lambda message: print(redact(message), flush=True))
        verdict = smoke_verdict(result)
        if verdict.exit_code != 1 and result["checks"].verified_head and result.get("review_receipt") and result.get("merge", MergeState()).receipt:
            checkpoint_verdict = verdict
            receipt = MergeReceipt.model_validate(result.get("merge", MergeState()).receipt)
            review = result.get("review_receipt")
            assert review is not None
            verdict = SmokeVerdict(1, "identity.unavailable", "GitHub identity")
            condition = forge.smoke_identity(SMOKE_REPO, receipt.pr, result["checks"].verified_head or "", review["id"])
            verdict = SmokeVerdict(1, condition, "GitHub identity") if condition else checkpoint_verdict
            if not condition:
                print(f"smoke identity {BOT_LOGIN} PR {receipt.pr} commit {receipt.source_head} COMMENT review {review['id']}", flush=True)
        print(f"smoke stop {result['stop']}; head {result['checks'].verified_head}; merge {result.get('merge')}", flush=True)
    except (CredentialFailure, ForgeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        print(redact(str(exc)), file=sys.stderr, flush=True)
    print(f"smoke exit {verdict.exit_code} {verdict.condition} attempt {verdict.attempt}", flush=True)
    return verdict.exit_code


def smoke_command() -> int:
    """Own a process group so the total deadline also stops workers and checks."""
    sys.stdout.flush()
    sys.stderr.flush()
    started = time.monotonic()
    pid = os.fork()
    if pid == 0:
        os.setsid()
        code = 1
        try:
            code = run_smoke()
        finally:
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(code)
    try:
        while time.monotonic() - started < SMOKE_SECONDS:
            waited, status = os.waitpid(pid, os.WNOHANG)
            if waited:
                return os.waitstatus_to_exitcode(status) if os.WIFEXITED(status) else 1
            time.sleep(0.05)
        print("smoke exit 1 wall_clock_deadline attempt live run", flush=True)
        return 1
    finally:
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
