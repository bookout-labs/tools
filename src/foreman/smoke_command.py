"""Bounded live smoke against the public scratch repositories. Each target's run starts and continues as a real run does:
the smoke files its issue, adds Ready under the human login, and GitHub's deliveries start and wake the run through the host's
receiver and accept. The smoke reads the saved run each time the run saves it and never moves it on itself."""

from decimal import Decimal
import http.client
import json
import re
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from watchfiles._rust_notify import RustNotify

from foreman.budget import claude_spend, work_tokens
from foreman.credentials import CredentialFailure, credentials, git_auth
from foreman.doctor import HUMAN_LOGIN
from foreman.github import BOT_LOGIN, DONE_LABEL, STOPPED_LABEL, ForgeError, LiveForge
from foreman.process_group import owned_group, signals_end_foreman
from foreman.receiver import PORT
from foreman.redaction import redact
from foreman.readiness import MergeReceipt
from foreman.release import release
from foreman.runner import SavedRun, issue_thread, saved_run
from foreman.state import CheckpointVersionError, checkpoint_store
from foreman.status import shows
from foreman.smoke import SmokeVerdict, smoke_verdict
from foreman.smoke_ownership import HumanSessionMissing, add_ready, cleanup_smoke, create_smoke_issue, human_session, smoke_lock
from foreman.usd import BudgetUsd

SMOKE_REPO = "bookout-labs/foreman-smoke"
SMOKE_TARGETS = (SMOKE_REPO, "bookout-labs/foreman-smoke-npm")
# The most both runs may spend together. Each target's repository variables cap one run at 1,000,000 tokens and $1.25,
# the measured two-round worst case plus a margin, so the two caps add up to more than these totals.
SMOKE_TOKEN_CAP = 1_500_000  # Two review rounds measured 709,054 and 827,368 tokens.
SMOKE_CLAUDE_USD = BudgetUsd(Decimal("2"))  # Two review rounds measured $0.93 and $0.63 Claude.
# Two review rounds took 260 s and 241 s to arm, and the queue merged the smoke's pull requests 129 s to 181 s after they opened.
SMOKE_SECONDS = 1200
# How long past the deadline the smoke's own process has to say which delivery did not come before its group is ended.
GRACE_SECONDS = 5


def published_file(root: Path, issue: int, base: str, head: str) -> None:
    """The published PR adds exactly the issue's file with its requested line. The smoke repository's own contract holds the check."""
    path = f"smoke_{issue}.txt"
    assert subprocess.check_output(["git", "diff", "--numstat", base, head], cwd=root, text=True) == f"1\t0\t{path}\n", \
        f"the pull request changes more than {path}"
    assert subprocess.check_output(["git", "show", f"{head}:{path}"], cwd=root, text=True) == f"smoke {issue}\n", \
        f"{path} does not hold the requested line"


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
                code, used, spent = _run_smoke(target, deadline, repo)
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


def receiving() -> bool:
    """Whether a listener answers on the host's receiver port. The receiver answers a GET with 501 and decides nothing."""
    connection = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    try:
        connection.request("GET", "/")
        connection.getresponse()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return True


def run_end(notify: RustNotify, store: Path, thread: str, deadline: float, commit: str) -> SavedRun | None:
    """The saved run once only a person could move it on, ended or asking a question, or as soon as it shows it saved a release
    other than the smoke's commit. The smoke reads it again each time the store changes, and the deadline is a ceiling. A run
    that is missing or still waiting then is what the deadline found."""
    # A save writes the store or its write-ahead log. A read writes the shared-memory file, and the write-ahead log only when no
    # writer has it open, so the smoke's own reads cause at most one more read and never a loop.
    saves = {store.name, f"{store.name}-wal"}
    saved = saved_run(store, thread)
    while saved is None or saved.waiting and saved.result["run"].release == commit:
        left = deadline - time.monotonic()
        changes = notify.watch(1_600, 50, max(1, int(left * 1000)), None) if left > 0 else "timeout"
        if isinstance(changes, str):
            break
        if any(Path(path).name in saves for _, path in changes):
            saved = saved_run(store, thread)
    return saved


def _run_smoke(directory: Path, deadline: float, repo: str) -> tuple[int, int | None, Decimal | None]:
    """Provision, file the issue and add Ready under the repository's human login, then read the run the deliveries start.
    The run reads its login and caps from the repository's variables."""
    verdict = SmokeVerdict(78, "bootstrap", "prerequisite")
    used: int | None = 0
    spent: Decimal | None = Decimal(0)
    started = time.monotonic()
    try:
        env = credentials.environment(repo)
        if env is None:
            raise CredentialFailure("Smoke requires Studio App credentials; personal credentials are not supported.")
        if not receiving():
            raise OSError(f"No receiver answers on 127.0.0.1:{PORT}, so no delivery can start or wake the run.")
        # The smoke proves only the commit it runs from, so it needs one before it files anything.
        if not (commit := release()):
            raise ValueError("The smoke's own code runs no commit: its checkout has tracked changes, or its install recorded none.")
        forge = LiveForge()
        # The App must read the repository before the smoke files anything.
        forge.default_branch(repo)
        human_session(forge.variables(repo).get(HUMAN_LOGIN, "").strip())
        verdict = SmokeVerdict(78, "cleanup", "prerequisite")
        forge.ensure_label(repo, "smoke", "ededed")
        cleanup_smoke(forge, repo)
        verdict = SmokeVerdict(78, "bootstrap", "prerequisite")
        root = directory / "checkout"
        auth = git_auth([f"https://github.com/{repo}"])
        subprocess.run(["git", *auth, "clone", f"https://github.com/{repo}", str(root)], env=env,
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        base_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
        print(f"smoke {repo} base {base_sha}", flush=True)
        if repo.endswith("-npm"):
            verdict = SmokeVerdict(1, "target.contract", "npm fixture")
            npm_contract(root)
            verdict = SmokeVerdict(78, "bootstrap", "npm prerequisite")
            package = json.loads((root / "package.json").read_text())
            for name in ("node", "npm"):
                actual = subprocess.check_output([name, "--version"], text=True).strip().removeprefix("v")
                if actual != package["engines"][name]:
                    raise ValueError(f"{name} requires {package['engines'][name]}, found {actual}")
        # The receiver's runs save where foreman start saves them by default.
        store = checkpoint_store()
        store.parent.mkdir(parents=True, exist_ok=True)
        issue = create_smoke_issue(forge, repo)
        print(f"smoke issue {issue}; deadline {deadline - time.monotonic():.0f}s. Other providers have no dollar cap.", flush=True)
        # The store is watched before Ready is added, so no save the run makes comes before the watch.
        with RustNotify([str(store.parent)], False, False, 0, False, False) as notify:
            verdict = SmokeVerdict(78, "delivery.ready", "prerequisite")
            add_ready(repo, issue)
            verdict = SmokeVerdict(1, "live_run", "checkpoint")
            try:
                saved = run_end(notify, store, issue_thread(repo, issue), deadline, commit)
            except CheckpointVersionError as exc:
                # Another schema is another release's, and the run's release cannot be read from it.
                verdict = SmokeVerdict(78, "release", "receiver release")
                raise ValueError(f"The receiver's run was saved under another schema, so its release is unknown, and the smoke runs {commit}.") from exc
        if saved is not None and (receiver := saved.result["run"].release) != commit:
            # Another release's run proves nothing about this commit, and is not a Foreman failure.
            used, spent = work_tokens(saved.result.get("attempts", [])), claude_spend(saved.result.get("attempts", []))
            verdict = SmokeVerdict(78, "release", "receiver release")
            raise ValueError(f"The receiver's run saved release {receiver or 'no commit'}, and the smoke runs {commit}.")
        if saved is None or saved.waiting:
            where = "the Ready delivery" if saved is None else "a GitHub delivery" if saved.result["stop"] == "merge_pending" else "the run"
            if saved is not None:
                used, spent = work_tokens(saved.result.get("attempts", [])), claude_spend(saved.result.get("attempts", []))
            print(f"smoke exit 1 wall_clock_deadline attempt {where}", flush=True)
            return 1, used, spent
        result = saved.result
        attempts = result.get("attempts", [])
        # The run's own output is the receiver's, so the smoke's log shows each attempt from the saved run.
        for index, attempt in enumerate(attempts, 1):
            print(f"attempt {index} {attempt['stage']} {attempt['model'] or 'no model'} {attempt['outcome']}", flush=True)
        used, spent = work_tokens(attempts), claude_spend(attempts)
        verdict = smoke_verdict(result)
        if verdict.exit_code == 0 and not saved.woken:
            # A run that never waited for a delivery proves nothing about wakes.
            verdict = SmokeVerdict(1, "delivery.wake", "never woken")
        elif verdict.exit_code != 1 and result["checks"].verified_head and result["review"].review_receipt and (merge := result.get("merge")) and merge.receipt:
            checkpoint_verdict = verdict
            receipt = MergeReceipt.model_validate(merge.receipt)
            verdict = SmokeVerdict(1, "pull_request.file", "published pull request")
            subprocess.run(["git", *auth, "fetch", "origin", f"refs/pull/{receipt.pr}/head"], cwd=root, env=env,
                           stdin=subprocess.DEVNULL, capture_output=True, check=True)
            published_file(root, issue, result["run"].base_sha, result["checks"].verified_head)
            review = result["review"].review_receipt
            verdict = SmokeVerdict(1, "identity.unavailable", "GitHub identity")
            observed = forge.smoke_identity(repo, receipt.pr, result["checks"].verified_head or "", review["id"])
            condition = next((name for name, valid in zip(("identity.pr", "identity.commit", "identity.review"), observed) if not valid), "")
            if condition:
                verdict = SmokeVerdict(1, condition, "GitHub identity")
            else:
                print(f"smoke identity {BOT_LOGIN} PR {receipt.pr} commit {receipt.source_head} COMMENT review {review['id']}", flush=True)
                # The fixture's issue mirrors its saved run: Stopped after a halt or Done once merged, and one status comment.
                verdict = SmokeVerdict(1, "status.unavailable", "GitHub issue status")
                shown = shows(forge, repo, issue, STOPPED_LABEL if result.get("halt") else DONE_LABEL)
                verdict = checkpoint_verdict if shown else SmokeVerdict(1, "status.issue", "GitHub issue status")
        print(f"smoke stop {result['stop']}; head {result['checks'].verified_head}; merge {result.get('merge')}", flush=True)
    except (AssertionError, KeyError, CredentialFailure, ForgeError, HumanSessionMissing, OSError, sqlite3.Error,
            subprocess.SubprocessError, ValueError) as exc:
        detail = str(exc)
        if isinstance(exc, subprocess.CalledProcessError) and exc.stderr:
            detail += "\n" + os.fsdecode(exc.stderr)
        print(redact(detail), file=sys.stderr, flush=True)
    print(f"smoke {repo} elapsed {time.monotonic() - started:.2f}s", flush=True)
    print(f"smoke exit {verdict.exit_code} {verdict.condition} attempt {verdict.attempt} target {repo}", flush=True)
    return verdict.exit_code, used, spent


def smoke_command() -> int:
    """Own the scratch directory and the smoke's own process group across every exit. The run's processes are the receiver's."""
    deadline = time.monotonic() + SMOKE_SECONDS
    with (tempfile.TemporaryDirectory(prefix="foreman-smoke-") as directory, signals_end_foreman(),
          owned_group([sys.executable, "-m", "foreman.smoke_command", directory, str(deadline)], grace=1) as process):
        try:
            code = process.wait(timeout=max(0, deadline - time.monotonic()) + GRACE_SECONDS)
            return code if code >= 0 else 1
        except subprocess.TimeoutExpired:
            print("smoke exit 1 wall_clock_deadline attempt live run", flush=True)
            return 1


if __name__ == "__main__":
    with signals_end_foreman():
        sys.exit(run_smoke(Path(sys.argv[1]), float(sys.argv[2])))
