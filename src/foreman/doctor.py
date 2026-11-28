"""Doctor: plain code that proves a target repository is ready for Foreman (target.yaml doctor).

At every start of a run, before any paid call, the runner runs the GitHub requirements before the graph. Only a new run
then runs the contract requirements, in triage.admit, and the full part whenever the contract file's digest at the base has
no green record in the host's run store. Only the full part starts a container. Each failure names the one missing
requirement and its exact fix. A read GitHub gives no usable answer to is a failed read, never not_ready.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import shlex
import sqlite3
import time
from typing import TypeVar

from typing_extensions import override

from foreman.contract import CONTRACT_PATH, Contract, ContractError, parse_contract, run_production_check
from foreman.github import READY_LABEL, AppInstallation, Forge, ForgeError, Permission
from foreman.state import StartSettings
from foreman.usd import BudgetUsd, usd_text
from foreman.workspace import CheckLog, ContainerUnavailable, RelatedServiceFailed, SetupFailed, Workspace, WorkspaceError, ended


@dataclass(frozen=True)
class Grant:
    """An App permission at read, by GitHub's key for it in an installation's permissions and by its name in the App's settings."""

    key: str
    name: str

    @override
    def __str__(self) -> str:
        return f"{self.name}: read"


# The events that start and wake a run, each with the App permission GitHub delivers it under (merge's invariants).
EVENTS = {"issues": Grant("issues", "Issues"), "issue_comment": Grant("issues", "Issues"),
          "pull_request": Grant("pull_requests", "Pull requests"), "pull_request_review": Grant("pull_requests", "Pull requests"),
          "check_suite": Grant("checks", "Checks"), "status": Grant("statuses", "Commit statuses")}


@dataclass(frozen=True)
class Requirement:
    """One requirement doctor checks, why Foreman needs it and how to supply it (doctor.setup_page)."""

    text: str
    why: str
    how: str

    @override
    def __str__(self) -> str:
        return self.text


APP, VARIABLES, WRITE_ACCESS, LABEL, MERGE_QUEUE, CONTRACT, VERIFY_SKILL = QUICK = (
    Requirement("the GitHub App is installed on the repository and GitHub lets it make doctor's reads",
                "Foreman reads and writes the repository only as its GitHub App, and GitHub delivers the events that start and "
                "wake runs to the App's installation.",
                "Install the Foreman GitHub App on the repository with every permission it requests, subscribed to "
                f"{', '.join(EVENTS)}. The Foreman host sets FOREMAN_APP_ID and the App's private key at FOREMAN_APP_KEY_PATH."),
    Requirement("FOREMAN_HUMAN_LOGIN is set and not blank, and each budget cap that is set parses. An unset cap uses its default.",
                "The human login's issue comments answer and stop runs, and the caps bound each run's work tokens and dollars.",
                "Set FOREMAN_HUMAN_LOGIN in the repository's Actions variables. FOREMAN_MAX_TOKENS (a positive whole number) and "
                "FOREMAN_MAX_BUDGET_USD (a positive dollar amount) are optional."),
    Requirement("the FOREMAN_HUMAN_LOGIN account can write to the repository",
                "Only a login that can write to the repository answers or stops its runs.",
                "Give the FOREMAN_HUMAN_LOGIN account write access to the repository."),
    Requirement(f"the {READY_LABEL} label exists",
                f"Adding {READY_LABEL} to an issue starts its run.",
                f"Create the {READY_LABEL} label in the repository."),
    Requirement("a merge queue ruleset protects the base branch",
                "Foreman merges only through GitHub's merge queue, which enforces the repository's required checks.",
                "Add a ruleset that targets the default branch with one merge queue rule and a merge method."),
    Requirement("the contract file parses strictly, so an unknown or duplicate key is named, and validates",
                "The contract file names the repository's check, verify skill, image, setup, secrets and app start, versioned "
                "with the code it checks.",
                f"Commit {CONTRACT_PATH} to the default branch with check, verify_skill, test_report, image, setup, secrets, start, production_check "
                "and mutation."),
    Requirement("the verify skill exists at the contract's path",
                "Verify follows the repository's own skill to prove a change works.",
                "Commit the skill as a regular file at the path the contract's verify_skill names."),
)
RELATED, IMAGE, SETUP, CHECK, MUTATION, START, PRODUCTION, PROBE = FULL = (
    Requirement("each related repository checks out at its default-branch head and its service starts",
                "Workers and the app's checks reach each related repository's real service, started in the attempt container "
                "from a checkout of that repository.",
                "Install the Foreman GitHub App on each repository related_repositories names, and make each one's start command "
                "start its service from a checkout of its default branch."),
    Requirement("the contract image builds",
                "Every check runs in a container from the contract's image, never on the Foreman host.",
                "Name an image docker on the Foreman host can pull in the contract's image field."),
    Requirement("the setup commands succeed",
                "Each container runs the setup commands in its checkout, with the test value of each named secret, before any check.",
                "List setup commands that succeed in a fresh checkout within thirty minutes, and set FOREMAN_TEST_<NAME> on the "
                "Foreman host for each secret the contract names."),
    Requirement("the check passes on the default branch and writes its test report",
                "Foreman proves each change with the repository's check, so the check passes before any change.",
                "Make the contract's check pass on the default branch within check_timeout_seconds, writing each test_report path "
                "the contract names."),
    Requirement("the mutation command runs on the default branch",
                "Before commit, Foreman flips sampled changed lines with the repository's own mutation tool, so a change no test "
                "notices goes back to the builder.",
                "Set mutation's command, which reads file:line entries on standard input and prints one JSON line per mutation "
                "with its line, id and killed or survived, and its sample_cap. It exits 0 for an empty list on the default branch."),
    Requirement("the app starts and its health command answers, when the contract names a start",
                "Verify drives the running app.",
                "Set start's command, a health command that exits 0 once the app answers, and timeout_seconds. A repository "
                "with no app leaves start out."),
    Requirement("the production check command runs against the default branch head",
                "After each merge Foreman runs the read-only production check against the merge commit, and a failed check "
                "files a revert of Foreman's merge.",
                "Set production_check's command, which reads the deployed commit from a source that needs no credentials and "
                "exits 0 when production runs the commit it is given and the check passes, its not_deployed_exit and its "
                "timeout_seconds. It runs on the Foreman host in the checkout, with no environment but PATH."),
    Requirement("the synthetic branch probe shows that neither an outside writer nor the App can move a PR's source branch while its arm is open",
                "Foreman arms the reviewed head with no human approval step, so no writer may move a pull request's branch once "
                "it is armed. Until the probe passes the repository is not admitted, so nothing is armed (merge_held).",
                "Protect pull request source branches so that GitHub refuses a push by the App or any other writer while the "
                "pull request's merge request is open. Doctor proves it with a synthetic pull request it arms and closes, "
                "acting as the App and as the Foreman host's own gh session."),
)
REQUIREMENTS = QUICK + FULL
# How long the full part allows a container read that is not a contract command, and how often it asks the health command.
READ_SECONDS, HEALTH_INTERVAL_SECONDS = 60, 1
HUMAN_LOGIN, MAX_TOKENS, MAX_BUDGET_USD = "FOREMAN_HUMAN_LOGIN", "FOREMAN_MAX_TOKENS", "FOREMAN_MAX_BUDGET_USD"
DEFAULT_TOKEN_CAP = 20_000_000
# Joe, 2026-11-23 on #26: "Cap 3". A repository raises it with FOREMAN_MAX_BUDGET_USD.
DEFAULT_BUDGET_USD = BudgetUsd(Decimal("3"))
T = TypeVar("T")


class NotReady(Exception):
    """The quick requirement that failed, what is missing and its exact fix."""

    def __init__(self, requirement: Requirement, missing: str, fix: str):
        super().__init__(f"Not ready: {missing}. Fix: {fix}.")
        self.requirement = requirement

    def halt_detail(self) -> str:
        """The not_ready report. The Ready label that asked for the start stays on the issue, for a new run and a resume alike."""
        return f"{self} Then remove and add the {READY_LABEL} label."


class ReadFailed(Exception):
    """A requirement's read that got no answer, a server error, a rate limit or another status that is no refusal. No setting is wrong."""

    def halt_detail(self) -> str:
        return f"{self} No setting needs to change. Remove and add the {READY_LABEL} label to try again."


READ_OR_ABOVE = ("read", "write", "admin")
REFUSALS = (401, 403, 404)
# A 422 to the App's token request means the installation does not cover the repository.
TOKEN_REFUSALS = (*REFUSALS, 422)


def answered(repo: str, requirement: Requirement, read: str, call: Callable[[], T], refusals: tuple[int, ...] = REFUSALS) -> T:
    """GitHub's answer to one read, made once. GitHub refusing the App's read is not_ready; any other failure is a failed read."""
    try:
        return call()
    except ForgeError as exc:
        if exc.status in refusals and not exc.rate_limited:
            fix = ("check FOREMAN_APP_ID and the App's private key at FOREMAN_APP_KEY_PATH on the Foreman host" if exc.status == 401 else
                   f"give the Foreman GitHub App's installation access to {repo} and every repository permission the App requests")
            raise NotReady(APP, f"GitHub answered HTTP {exc.status} to the read of {read}, refusing the App", fix) from exc
        raise ReadFailed(f'The read of {read}, for the requirement "{requirement.text.rstrip(".")}", failed because GitHub {exc.answer}.') from exc


def positive_tokens(raw: str) -> int:
    """A token cap: a positive whole number of work tokens. Raises ValueError otherwise."""
    if (value := int(raw)) <= 0:
        raise ValueError(f"{raw!r} is not positive")
    return value


def positive_budget(raw: str) -> BudgetUsd:
    """A dollar cap: a finite positive amount. Raises ValueError otherwise."""
    try:
        return BudgetUsd(Decimal(raw))
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a dollar amount") from None


def operator_variables(repo: str, variables: Mapping[str, str]) -> StartSettings:
    """FOREMAN_HUMAN_LOGIN is set and not blank, and each budget cap that is set parses. An unset budget cap takes its default."""
    def missing(detail: str, fix: str) -> NotReady:
        return NotReady(VARIABLES, detail, f"{fix} in {repo}'s Actions variables")

    human_login = variables.get(HUMAN_LOGIN, "").strip()
    if not human_login:
        raise missing(f"the {HUMAN_LOGIN} repository variable is not set",
                      f"set {HUMAN_LOGIN} to the GitHub login whose issue comments answer and stop runs")
    tokens, dollars = variables.get(MAX_TOKENS), variables.get(MAX_BUDGET_USD)
    try:
        max_tokens = positive_tokens(tokens) if tokens is not None else None
    except ValueError:
        raise missing(f"the {MAX_TOKENS} repository variable {tokens!r} is not a positive integer",
                      f"set {MAX_TOKENS} to a positive whole number of work tokens, or delete it to use the default") from None
    try:
        max_budget_usd = positive_budget(dollars) if dollars is not None else None
    except ValueError:
        raise missing(f"the {MAX_BUDGET_USD} repository variable {dollars!r} is not a positive dollar amount",
                      f"set {MAX_BUDGET_USD} to a positive amount such as 3 or 4.50, or delete it to use the default") from None
    return StartSettings(human_login=human_login, max_tokens=DEFAULT_TOKEN_CAP if max_tokens is None else max_tokens,
                         max_budget_usd=usd_text(max_budget_usd or DEFAULT_BUDGET_USD))


def repository(forge: Forge, repo: str) -> StartSettings:
    """The quick requirements GitHub answers: the App install, the operator variables, the login's write access, the Ready label
    and the merge queue.

    The one read of the operator variables at a start, so the run's login and caps are the values doctor checked.
    """
    installation: AppInstallation = answered(repo, APP, f"the App's token for {repo}", lambda: forge.app_installation(repo), TOKEN_REFUSALS)
    match installation:
        case "not_installed":
            raise NotReady(APP, f"the GitHub App is not installed on {repo}",
                           f"install the Foreman GitHub App on {repo}'s account with access to {repo}")
        case "unconfigured":
            raise NotReady(APP, f"this Foreman host has no FOREMAN_APP_ID, so it cannot act as the GitHub App on {repo}",
                           "set FOREMAN_APP_ID on the Foreman host, with the App's private key at FOREMAN_APP_KEY_PATH "
                           "(default ~/.config/bookout-foreman/app.pem)")
        case "installed":
            pass
    subscription = answered(repo, APP, f"the App's installation on {repo}", lambda: forge.app_subscription(repo), TOKEN_REFUSALS)
    if missing := [event for event in EVENTS if event not in subscription.events]:
        raise NotReady(APP, f"the GitHub App's installation on {repo} is not subscribed to {', '.join(missing)}",
                       "in the Foreman GitHub App's settings, subscribe to " + ", ".join(f"{event} (permission {EVENTS[event]})" for event in missing)
                       + f", then accept the App's new permissions on {repo}'s installation if GitHub asks")
    if unread := [event for event, grant in EVENTS.items() if subscription.permissions.get(grant.key) not in READ_OR_ABOVE]:
        raise NotReady(APP, f"the GitHub App's installation on {repo} cannot read {', '.join(unread)}",
                       "in the Foreman GitHub App's settings, grant " + ", ".join(f"{EVENTS[event]} for {event}" for event in unread)
                       + f", then accept the App's new permissions on {repo}'s installation")
    operator = operator_variables(repo, answered(repo, VARIABLES, f"{repo}'s Actions variables", lambda: forge.variables(repo)))
    login = operator.human_login
    permission: Permission | None = answered(repo, WRITE_ACCESS, f"{login}'s permission on {repo}", lambda: forge.permission(repo, login))
    match permission:
        case None:
            raise NotReady(WRITE_ACCESS, f"GitHub does not know the {HUMAN_LOGIN} login {login}",
                           f"correct the spelling of {HUMAN_LOGIN} in {repo}'s Actions variables, or name another login")
        case "read" | "none":
            raise NotReady(WRITE_ACCESS, f"the {HUMAN_LOGIN} account {login} cannot write to {repo}, where GitHub reports its permission as {permission}",
                           f"give {login} write access to {repo}, or set {HUMAN_LOGIN} in {repo}'s Actions variables to another login that has it")
        case "admin" | "write":
            pass
    if not answered(repo, LABEL, f"{repo}'s {READY_LABEL} label", lambda: forge.has_label(repo, READY_LABEL)):
        raise NotReady(LABEL, f"{repo} has no {READY_LABEL} label", f"create the {READY_LABEL} label in {repo}")
    branch = answered(repo, MERGE_QUEUE, f"{repo}'s default branch", lambda: forge.default_branch(repo))
    if answered(repo, MERGE_QUEUE, f"{repo}'s rules for {branch}", lambda: forge.merge_queue(repo, branch)) is None:
        raise NotReady(MERGE_QUEUE, f"no single merge queue rule applies to {repo}'s base branch {branch}",
                       f"add a ruleset that targets {branch} in {repo} with one merge queue rule and a merge method")
    return operator


def contract(workspace: Workspace, base: str) -> Contract:
    """The contract at the base commit, once the contract file validates and names a tracked verify skill."""
    text = workspace.tracked_file(base, CONTRACT_PATH)
    if text is None:
        raise NotReady(CONTRACT, f"the contract file {CONTRACT_PATH} is not a tracked file at the base commit {base}",
                       f"commit {CONTRACT_PATH} to the default branch with at least its check and verify_skill fields")
    try:
        parsed = parse_contract(text)
    except ContractError as exc:
        raise NotReady(CONTRACT, f"the contract file {CONTRACT_PATH} does not validate: {exc}",
                       f"correct each named key in {CONTRACT_PATH} on the default branch") from exc
    if workspace.tracked_path(base, parsed.verify_skill) != "file":
        raise NotReady(VERIFY_SKILL, f"the verify skill {parsed.verify_skill} that the contract names is not a tracked regular file at the base commit {base}",
                       f"commit the repository's verify skill at {parsed.verify_skill}, or set verify_skill to the tracked skill's path")
    return parsed


def contract_digest(workspace: Workspace, base: str) -> str:
    """The digest of the contract file at the base commit, which the quick part validated."""
    text = workspace.tracked_file(base, CONTRACT_PATH)
    assert text is not None
    return hashlib.sha256(text).hexdigest()


def full(workspace: Workspace, forge: Forge, repo: str, base: str, parsed: Contract, log: CheckLog | None) -> None:
    """The full part at the base: each related repository checked out at its default-branch head, the contract's image built
    with its setup run and each related service started, the check, the mutation command with an empty sample, and the app
    started, when the contract names a start, until its health command answers within start.timeout_seconds, then the
    production check against the base and the synthetic branch probe. The container is removed when the app's check ends."""
    for item in parsed.related_repositories:
        branch = forge.default_branch(item.repository)
        try:
            workspace.check_out_related(item.repository, branch)
        except WorkspaceError as exc:
            raise NotReady(RELATED, f"the related repository {item.repository} did not check out at its default-branch head {branch}: "
                                    f"{str(exc).rstrip().rstrip('.')}",
                           f"give the Foreman GitHub App's installation access to {item.repository}, or correct related_repositories "
                           f"in {CONTRACT_PATH}") from exc
    fixes = {IMAGE: f"make the image {parsed.image!r} available to docker on the Foreman host, or correct image in {CONTRACT_PATH}",
             CHECK: f"make {parsed.check!r} pass on the default branch",
             MUTATION: f"make {parsed.mutation.command!r} exit 0 for an empty list on the default branch"}
    requirement = IMAGE
    try:
        with workspace.container_at(base, base=base, log=log) as run:
            requirement = CHECK
            if (result := run(parsed.check, parsed.check_timeout_seconds)) != 0:
                raise NotReady(CHECK, f"the check {parsed.check!r} {ended(result)} on the default branch at {base}", fixes[CHECK])
            reports = [parsed.test_report] if isinstance(parsed.test_report, str) else parsed.test_report or []
            for report in reports:
                if run(f"test -f {shlex.quote(report)}", READ_SECONDS) != 0:
                    raise NotReady(CHECK, f"the check {parsed.check!r} wrote no test report at {report}",
                                   f"make the check write its JUnit XML report at {report}, or correct test_report in {CONTRACT_PATH}")
            # The default branch has no changed lines, so the tool gets an empty sample.
            requirement = MUTATION
            if (result := run(f"{{\n{parsed.mutation.command}\n}} </dev/null", parsed.check_timeout_seconds)) != 0:
                raise NotReady(MUTATION, f"the mutation command {parsed.mutation.command!r} {ended(result)} on the default branch "
                                         f"at {base}", fixes[MUTATION])
            if (start := parsed.start) is not None:
                requirement = START
                fixes[START] = f"make {start.command!r} start the app so {start.health!r} exits 0 within start.timeout_seconds"
                run(f"({start.command}) >/dev/null 2>&1 &", READ_SECONDS)
                deadline = time.monotonic() + start.timeout_seconds
                while run(start.health, max(1.0, deadline - time.monotonic())) != 0:
                    if time.monotonic() >= deadline:
                        raise NotReady(START, f"the health command {start.health!r} did not answer within "
                                              f"{start.timeout_seconds} seconds of {start.command!r}", fixes[START])
                    time.sleep(HEALTH_INTERVAL_SECONDS)
    except RelatedServiceFailed as exc:
        raise NotReady(RELATED, str(exc).rstrip().rstrip("."),
                       f"make the start command of {exc.repository} in {CONTRACT_PATH} start its service from its checkout") from exc
    except SetupFailed as exc:
        raise NotReady(SETUP, str(exc).rstrip().rstrip("."),
                       f"correct the setup commands in {CONTRACT_PATH}, and set each test value they need on the Foreman host") from exc
    except ContainerUnavailable as exc:
        raise NotReady(requirement, str(exc).rstrip().rstrip("."), fixes[requirement]) from exc
    check = parsed.production_check
    code, output = run_production_check(check, base, workspace.root)
    if code not in (0, check.not_deployed_exit):
        how = output if code is None else f"exited with {code}"
        raise NotReady(PRODUCTION, f"the production check {check.command!r} {how} against the default branch head {base}",
                       f"make {check.command!r} exit 0 or {check.not_deployed_exit} for the default branch head")
    probe = forge.branch_probe(repo, forge.default_branch(repo), base)
    if moved := [writer for writer, did in (("the App", probe.app_moved), ("an outside writer", probe.outside_moved)) if did]:
        raise NotReady(PROBE, f"{' and '.join(moved)} moved the source branch of {repo}'s armed synthetic pull request",
                       "protect pull request source branches so that GitHub refuses every push while a merge request is open")


class GreenChecks:
    """Each green full check, in the host's run store, keyed by repository and contract file digest (doctor.full)."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        connection.execute("CREATE TABLE IF NOT EXISTS doctor_green (repo TEXT, digest TEXT, PRIMARY KEY (repo, digest))")
        connection.commit()

    @classmethod
    def in_memory(cls) -> "GreenChecks":
        """The records of a run outside the host's run store, such as a dry run's, which end with it."""
        return cls(sqlite3.connect(":memory:", check_same_thread=False))

    def green(self, repo: str, digest: str) -> bool:
        return self.connection.execute("SELECT 1 FROM doctor_green WHERE repo = ? AND digest = ?", (repo.lower(), digest)).fetchone() is not None

    def record(self, repo: str, digest: str) -> None:
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO doctor_green VALUES (?, ?)", (repo.lower(), digest))


def admitted(workspace: Workspace, forge: Forge, repo: str, base: str, greens: GreenChecks, log: CheckLog | None) -> Contract:
    """The contract requirements at admission, then the full part once for a contract file digest with no green record."""
    parsed = contract(workspace, base)
    digest = contract_digest(workspace, base)
    if not greens.green(repo, digest):
        full(workspace, forge, repo, base, parsed, log)
        greens.record(repo, digest)
    return parsed
