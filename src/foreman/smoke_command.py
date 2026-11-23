"""Bounded live smoke against the public scratch repository."""

from contextlib import redirect_stderr
from decimal import Decimal
import io
import json
import re
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time

from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from foreman.budget import claude_spend, work_tokens
from foreman.cli import main
from foreman.state import SCHEMA_VERSION, checkpoint_serializer
from foreman.credentials import CredentialFailure, credentials, git_auth
from foreman.delivery import LiveRun, read_delivery_state
from foreman.github import BOT_LOGIN, ForgeError, LiveForge
from foreman.process_group import owned_group, signals_end_foreman
from foreman.redaction import redact
from foreman.readiness import MergeReceipt, MergeState
from foreman.runner import run_live_delivery
from foreman.smoke import SmokeVerdict, smoke_verdict
from foreman.smoke_ownership import cleanup_smoke, create_smoke_issue, smoke_lock
from foreman.usd import BudgetUsd
from foreman.worker import CliWorker
from foreman.workspace import LiveWorkspace

SMOKE_REPO = "bookout-labs/foreman-smoke"
SMOKE_TARGETS = (SMOKE_REPO, "bookout-labs/foreman-smoke-npm")
SMOKE_TOKEN_CAP = 1_500_000  # Two review rounds measured 709,054 and 827,368 tokens.
SMOKE_CLAUDE_USD = BudgetUsd(Decimal("2"))  # Two review rounds measured $0.93 and $0.63 Claude.
SMOKE_SECONDS = 900  # Two review rounds took 260 s and 241 s; allow CI observation too.

FIXTURE_CHECK = '''import pathlib, subprocess, sys
issue, base = sys.argv[1:]
base = subprocess.check_output(["git", "merge-base", "HEAD", base]).decode().strip()
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
    """Serialize cleanup and delivery for each target, including after a crash.

    Every target runs unless one fails: an unavailable target (78) still lets the next one run, and the smoke
    returns the worst exit code.
    """
    tokens, dollars = SMOKE_TOKEN_CAP, SMOKE_CLAUDE_USD.amount
    started, worst = time.monotonic(), 0
    for repo in SMOKE_TARGETS:
        target = directory if repo == SMOKE_REPO else directory / repo.split("/")[-1]
        target.mkdir(exist_ok=True)
        try:
            with smoke_lock(repo):
                code, used, spent = _run_smoke(target, deadline, repo, tokens, BudgetUsd(dollars))
        except (ForgeError, OSError) as exc:
            print(redact(str(exc)), file=sys.stderr, flush=True)
            print(f"smoke {repo} exit 78 lock attempt prerequisite", flush=True)
            code, used, spent = 78, 0, Decimal(0)
        else:
            print(f"smoke {repo} exit {code}; work tokens {used}; Claude ${spent}", flush=True)
        if code == 1:
            return 1
        worst = max(worst, code)
        if used is None or spent is None:
            print(f"smoke {repo} exit 1 aggregate usage unknown", flush=True)
            return 1
        tokens, dollars = tokens - used, dollars - spent
        if tokens < 0 or dollars < 0 or ((tokens == 0 or dollars == 0) and repo != SMOKE_TARGETS[-1]):
            print(f"smoke {repo} exit 1 aggregate budget exhausted", flush=True)
            return 1
    print(f"smoke aggregate work tokens {SMOKE_TOKEN_CAP - tokens}; Claude ${SMOKE_CLAUDE_USD.amount - dollars}; "
          f"elapsed {time.monotonic() - started:.2f}s; exit {worst}", flush=True)
    return worst


def seed_old_state(home: Path, repo: str, *issues: int) -> Path:
    path = home / "foreman/checkpoints.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        saver.serde = checkpoint_serializer()
        for issue in issues:
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"schema_version": SCHEMA_VERSION - 1}
            checkpoint["channel_versions"] = {"schema_version": "1"}
            saver.put({"configurable": {"thread_id": f"{repo.lower()}#{issue}", "checkpoint_ns": ""}}, checkpoint, {"source": "input", "step": -1, "parents": {}}, {})
    return path


def npm_contract(root: Path) -> None:
    """Fail closed if maintenance erases the unlike-target regression probes."""
    package = json.loads((root / "package.json").read_text())
    lock = json.loads((root / "package-lock.json").read_text())
    assert package["type"] == "module" and package["scripts"]["check"] == "node --test", "npm suite command drift"
    assert lock["lockfileVersion"] == 3 and not package.get("dependencies"), "npm dependency contract drift"
    for name in ("node", "npm"):
        assert re.fullmatch(r"\d+\.\d+\.\d+", package["engines"][name]), f"{name} must be pinned"
    assert (root / ".nvmrc").read_text().strip() == package["engines"]["node"], "Node pins differ"
    suite = "\n".join(p.read_text() for p in (root / "tests").glob("*.test.js"))
    assert suite.count("test(") >= 18 and "assert." in suite and package["name"] in suite, "production suite missing"
    assert list((root / "src").glob("*.js")), "production library missing"


def _run_smoke(directory: Path, deadline: float, repo: str, tokens: int, dollars: BudgetUsd) -> tuple[int, int | None, Decimal | None]:
    """Provision first, then exercise the normal live runner and its watcher."""
    verdict = SmokeVerdict(78, "bootstrap", "prerequisite")
    used: int | None = 0
    spent: Decimal | None = Decimal(0)
    started = time.monotonic()
    old_path = os.environ.get("PATH", "")
    old_home = os.environ.get("XDG_STATE_HOME")
    try:
        env = credentials.environment(repo)
        if env is None:
            raise CredentialFailure("Smoke requires Studio App credentials; personal credentials are not supported.")
        forge = LiveForge()
        base_branch = forge.default_branch(repo)
        verdict = SmokeVerdict(78, "cleanup", "prerequisite")
        forge.ensure_smoke_label(repo)
        cleanup_smoke(forge, repo)
        verdict = SmokeVerdict(78, "bootstrap", "prerequisite")
        root = directory / "checkout"
        subprocess.run(["git", *git_auth([f"https://github.com/{repo}"]), "clone",
                        f"https://github.com/{repo}", str(root)], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        npm = repo.endswith("-npm")
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        print(f"smoke {repo} base {base_sha}", flush=True)
        if npm:
            verdict = SmokeVerdict(1, "target.contract", "npm fixture")
            npm_contract(root)
            verdict = SmokeVerdict(78, "bootstrap", "npm prerequisite")
            package = json.loads((root / "package.json").read_text())
            for name in ("node", "npm"):
                actual = subprocess.check_output([name, "--version"], text=True).strip().removeprefix("v")
                if actual != package["engines"][name]:
                    raise ValueError(f"{name} requires {package['engines'][name]}, found {actual}")
            subprocess.run(["npm", "ci"], cwd=root, capture_output=True, check=True)
            verdict = SmokeVerdict(1, "target.contract", "npm fixture")
            dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root, text=True)
            if dirty:
                raise ValueError(f"npm ci changed tracked checkout:\n{dirty}")
        else:
            subprocess.run(["python3.12", "-m", "venv", str(directory / "python")], capture_output=True, check=True)
            os.environ["PATH"] = str(directory / "python/bin") + os.pathsep + old_path
            subprocess.run(["python", "-c", "import sys; assert sys.version_info[:2] == (3, 12)"], capture_output=True, check=True)
        script = directory / "acceptance.py"
        script.write_text(FIXTURE_CHECK)
        issue = create_smoke_issue(forge, repo)
        request = f"Add smoke_{issue}.txt containing exactly 'smoke {issue}\\n'. Change no other files."
        check = ("npm run check" if npm else "python -m unittest discover -s tests") + " && " + shlex.join([sys.executable if npm else "python", str(script), str(issue), f"refs/remotes/origin/{base_branch}"])
        print(f"smoke issue {issue}; tokens {tokens}; Claude ${dollars.amount}; deadline {SMOKE_SECONDS}s. "
              "Other providers have no dollar cap.", flush=True)
        live = LiveRun(forge, LiveWorkspace(root, repo))
        command = ["run", "delivery", "--repo", repo, "--issue", str(issue), "--request", request,
                   "--check", check, "--preflight", "npm ci" if npm else "",
                   "--max-tokens", str(tokens), "--max-budget-usd", str(dollars.amount)]
        checkpoint = directory / "checkpoint.sqlite3"
        if npm:
            verdict = SmokeVerdict(1, "checkpoint.refusal_guidance", "old exact issue")
            home = directory / "old-state"
            checkpoint = seed_old_state(home, repo, issue, issue + 1)
            before = checkpoint.read_bytes()
            os.environ["XDG_STATE_HOME"] = str(home)
            errors = io.StringIO()
            def forbidden(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
                raise ValueError("old checkpoint dispatched a worker")
            with redirect_stderr(errors):
                code = main(command, live_run=live, worker_runner=forbidden)
            message = errors.getvalue()
            required = (redact(str(checkpoint)), f"{repo.lower()}#{issue}", f"schema_version {SCHEMA_VERSION - 1}",
                        "without --answer", "XDG_STATE_HOME", "new directory")
            if code != 1 or not all(word in message for word in required) or checkpoint.read_bytes() != before:
                raise ValueError(f"old checkpoint refusal lost actionable guidance or changed state: {message}")
            os.environ["XDG_STATE_HOME"] = str(directory / "fresh-state")
            checkpoint = seed_old_state(directory / "fresh-state", repo, issue + 1)
        verdict = SmokeVerdict(1, "live_run", "checkpoint")
        used, spent = None, None
        while True:
            if npm:
                main(command, live_run=live)
                with SqliteSaver.from_conn_string(str(checkpoint)) as saver:
                    saver.serde = checkpoint_serializer()
                    saved = saver.get({"configurable": {"thread_id": f"{repo.lower()}#{issue}"}})
                    if saved is None:
                        raise ValueError("CLI produced no delivery checkpoint")
                    result = read_delivery_state(saved["channel_values"])
            else:
                result = run_live_delivery(repo=repo, issue=issue, request=request, check=check,
                                           checkpoint=directory / "checkpoint.sqlite3", dispatcher=CliWorker(root, scratch_root=directory),
                                           run=live,
                                           max_tokens=tokens, max_budget_usd=dollars,
                                           log=lambda message: print(redact(message), flush=True))
            # Arm waits for the checks. Once armed, the queued pull request is the smoke's end point.
            if result["stop"] != "merge_pending" or result.get("merge", MergeState()).status == "armed":
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("smoke exit 1 wall_clock_deadline attempt CI observation", flush=True)
                return 1, used, spent
            time.sleep(min(5, remaining))
        used, spent = work_tokens(result.get("attempts", [])), claude_spend(result.get("attempts", []))
        verdict = smoke_verdict(result)
        if verdict.exit_code != 1 and result["checks"].verified_head and result["review"].review_receipt and result.get("merge", MergeState()).receipt:
            checkpoint_verdict = verdict
            receipt = MergeReceipt.model_validate(result.get("merge", MergeState()).receipt)
            review = result["review"].review_receipt
            assert review is not None
            verdict = SmokeVerdict(1, "identity.unavailable", "GitHub identity")
            observed = forge.smoke_identity(repo, receipt.pr, result["checks"].verified_head or "", review["id"])
            condition = next((name for name, valid in zip(("identity.pr", "identity.commit", "identity.review"), observed) if not valid), "")
            verdict = SmokeVerdict(1, condition, "GitHub identity") if condition else checkpoint_verdict
            if not condition:
                print(f"smoke identity {BOT_LOGIN} PR {receipt.pr} commit {receipt.source_head} COMMENT review {review['id']}", flush=True)
        print(f"smoke stop {result['stop']}; head {result['checks'].verified_head}; merge {result.get('merge')}", flush=True)
    except (AssertionError, KeyError, CredentialFailure, ForgeError, OSError, subprocess.SubprocessError, ValueError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += "\n" + os.fsdecode(exc.stderr)
        print(redact(detail), file=sys.stderr, flush=True)
    finally:
        os.environ["PATH"] = old_path
        if old_home is None:
            os.environ.pop("XDG_STATE_HOME", None)
        else:
            os.environ["XDG_STATE_HOME"] = old_home
    print(f"smoke {repo} elapsed {time.monotonic() - started:.2f}s", flush=True)
    print(f"smoke exit {verdict.exit_code} {verdict.condition} attempt {verdict.attempt} target {repo}", flush=True)
    return verdict.exit_code, used, spent


def smoke_command() -> int:
    """Own the scratch directory and worker process group across every exit."""
    deadline = time.monotonic() + SMOKE_SECONDS
    with (tempfile.TemporaryDirectory(prefix="foreman-smoke-") as directory, signals_end_foreman(),
          owned_group([sys.executable, "-m", "foreman.smoke_command", directory, str(deadline)], grace=1) as process):
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()))
            return code if code >= 0 else 1
        except subprocess.TimeoutExpired:
            print("smoke exit 1 wall_clock_deadline attempt live run", flush=True)
            return 1


if __name__ == "__main__":
    with signals_end_foreman():
        sys.exit(run_smoke(Path(sys.argv[1]), float(sys.argv[2])))
