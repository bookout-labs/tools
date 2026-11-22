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
from foreman.outcomes import Halt
from foreman.readiness import MergeReceipt, MergeState
from foreman.runner import run_live_delivery
from foreman.smoke import SmokeVerdict, smoke_verdict
from foreman.usd import BudgetUsd
from foreman.worker import CliWorker
from foreman.workspace import LiveWorkspace

SMOKE_REPO = "bookout-labs/foreman-smoke"
SMOKE_TOKEN_CAP = 1_500_000  # Two review rounds measured 709,054 and 827,368 tokens.
SMOKE_CLAUDE_USD = BudgetUsd(Decimal("2"))  # Two review rounds measured $0.93 and $0.63 Claude.
SMOKE_SECONDS = 900  # Two review rounds took 260 s and 241 s; allow CI observation too.

FIXTURE_CHECK = '''import pathlib, subprocess, sys
issue, base = sys.argv[1:]
path = f"smoke_{issue}.txt"
assert pathlib.Path(path).read_bytes() == f"smoke {issue}\\n".encode()
status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"]).decode()
head = subprocess.check_output(["git", "rev-parse", "HEAD"]).decode().strip()
if head == base:
    assert status == f"?? {path}\\n"
else:
    assert not status
    assert subprocess.check_output(["git", "diff", "--name-only", base, "HEAD"]).decode().splitlines() == [path]
    assert subprocess.check_output(["git", "diff", "--numstat", base, "HEAD"]).decode() == f"1\\t0\\t{path}\\n"
'''


def run_smoke(directory: Path, deadline: float) -> int:
    """Provision first, then exercise the normal live runner and its watcher."""
    verdict = SmokeVerdict(78, "bootstrap", "prerequisite")
    try:
        env = credentials.environment(SMOKE_REPO)
        if env is None:
            raise CredentialFailure("Smoke requires Studio App credentials; personal credentials are not supported.")
        forge = LiveForge()
        forge.default_branch(SMOKE_REPO)
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
        while True:
            result = run_live_delivery(repo=SMOKE_REPO, issue=issue, request=request, check=check,
                                       checkpoint=directory / "checkpoint.sqlite3", dispatcher=CliWorker(root),
                                       run=LiveRun(forge, LiveWorkspace(root, SMOKE_REPO)),
                                       max_tokens=SMOKE_TOKEN_CAP, max_budget_usd=SMOKE_CLAUDE_USD,
                                       log=lambda message: print(redact(message), flush=True))
            if result.get("halt") != Halt.ci_pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("smoke exit 1 wall_clock_deadline attempt CI observation", flush=True)
                return 1
            time.sleep(min(5, remaining))
        verdict = smoke_verdict(result)
        if verdict.exit_code != 1 and result["checks"].verified_head and result["review"].review_receipt and result.get("merge", MergeState()).receipt:
            checkpoint_verdict = verdict
            receipt = MergeReceipt.model_validate(result.get("merge", MergeState()).receipt)
            review = result["review"].review_receipt
            assert review is not None
            verdict = SmokeVerdict(1, "identity.unavailable", "GitHub identity")
            observed = forge.smoke_identity(SMOKE_REPO, receipt.pr, result["checks"].verified_head or "", review["id"])
            condition = next((name for name, valid in zip(("identity.pr", "identity.commit", "identity.review"), observed) if not valid), "")
            verdict = SmokeVerdict(1, condition, "GitHub identity") if condition else checkpoint_verdict
            if not condition:
                print(f"smoke identity {BOT_LOGIN} PR {receipt.pr} commit {receipt.source_head} COMMENT review {review['id']}", flush=True)
        print(f"smoke stop {result['stop']}; head {result['checks'].verified_head}; merge {result.get('merge')}", flush=True)
    except (CredentialFailure, ForgeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += "\n" + os.fsdecode(exc.stderr)
        print(redact(detail), file=sys.stderr, flush=True)
    print(f"smoke exit {verdict.exit_code} {verdict.condition} attempt {verdict.attempt}", flush=True)
    return verdict.exit_code


def smoke_command() -> int:
    """Own the scratch directory and worker process group across every exit."""
    deadline = time.monotonic() + SMOKE_SECONDS
    with tempfile.TemporaryDirectory(prefix="foreman-smoke-") as directory:
        def stop(signum: int, frame: object) -> None:
            raise SystemExit(128 + signum)
        previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "foreman.smoke_command", directory, str(deadline)],
                start_new_session=True,
            )
            try:
                code = process.wait(timeout=max(0, deadline - time.monotonic()))
                return code if code >= 0 else 1
            except subprocess.TimeoutExpired:
                print("smoke exit 1 wall_clock_deadline attempt live run", flush=True)
                return 1
        finally:
            if process is not None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                    # Workers can ignore TERM even after their parent exits.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                except ProcessLookupError:
                    pass
                process.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)


if __name__ == "__main__":
    sys.exit(run_smoke(Path(sys.argv[1]), float(sys.argv[2])))
