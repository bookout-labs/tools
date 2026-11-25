"""Doctor: plain code that proves a target repository is ready for Foreman (target.yaml doctor).

The quick part runs at every start of a run before any paid call. A new run runs all of it in triage.admit. A resume runs
only its GitHub requirements, since the contract already passed at the pinned base. It starts no container. Each failure
names the one missing requirement and its exact fix.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from foreman.contract import CONTRACT_PATH, Contract, ContractError, parse_contract
from foreman.github import READY_LABEL, Forge
from foreman.usd import BudgetUsd
from foreman.workspace import Workspace

APP, VARIABLES, LABEL, MERGE_QUEUE, CONTRACT, VERIFY_SKILL = QUICK = (
    "the GitHub App is installed on the repository",
    "FOREMAN_HUMAN_LOGIN is set and not blank, and each budget cap that is set parses. An unset cap uses its default.",
    f"the {READY_LABEL} label exists",
    "a merge queue ruleset protects the base branch",
    "the contract file parses strictly, so an unknown or duplicate key is named, and validates",
    "the verify skill exists at the contract's path",
)
HUMAN_LOGIN, MAX_TOKENS, MAX_BUDGET_USD = "FOREMAN_HUMAN_LOGIN", "FOREMAN_MAX_TOKENS", "FOREMAN_MAX_BUDGET_USD"


class NotReady(Exception):
    """The quick requirement that failed, what is missing and its exact fix."""

    def __init__(self, requirement: str, missing: str, fix: str):
        super().__init__(f"Not ready: {missing}. Fix: {fix}.")
        self.requirement = requirement

    def halt_detail(self, *, resumed: bool = False) -> str:
        """The not_ready report. A new run's issue keeps Ready. A refused resume stays halted under its Stopped label."""
        if resumed:
            return f"{self} The run stays halted at its step. Then add the {READY_LABEL} label again to resume it."
        return f"{self} Then remove and add the {READY_LABEL} label."


@dataclass(frozen=True)
class OperatorVariables:
    """The repository variables the operator controls. An unset budget cap is None, and its default applies."""

    human_login: str
    max_tokens: int | None
    max_budget_usd: BudgetUsd | None


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


def operator_variables(repo: str, variables: Mapping[str, str]) -> OperatorVariables:
    """FOREMAN_HUMAN_LOGIN is set and not blank, and each budget cap that is set parses."""
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
    return OperatorVariables(human_login, max_tokens, max_budget_usd)


def repository(forge: Forge, repo: str) -> None:
    """The quick requirements GitHub answers: the App install, the operator variables, the Ready label and the merge queue."""
    match forge.app_installation(repo):
        case "not_installed":
            raise NotReady(APP, f"the GitHub App is not installed on {repo}",
                           f"install the Foreman GitHub App on {repo}'s account with access to {repo}")
        case "unconfigured":
            raise NotReady(APP, f"this Foreman host has no FOREMAN_APP_ID, so it cannot act as the GitHub App on {repo}",
                           "set FOREMAN_APP_ID on the Foreman host, with the App's private key at FOREMAN_APP_KEY_PATH "
                           "(default ~/.config/bookout-foreman/app.pem)")
        case "installed":
            pass
    operator_variables(repo, forge.variables(repo))
    if not forge.has_label(repo, READY_LABEL):
        raise NotReady(LABEL, f"{repo} has no {READY_LABEL} label", f"create the {READY_LABEL} label in {repo}")
    branch = forge.default_branch(repo)
    if forge.merge_queue(repo, branch) is None:
        raise NotReady(MERGE_QUEUE, f"no single merge queue rule applies to {repo}'s base branch {branch}",
                       f"add a ruleset that targets {branch} in {repo} with one merge queue rule and a merge method")


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
    if not workspace.has_tracked_file(base, parsed.verify_skill):
        raise NotReady(VERIFY_SKILL, f"the verify skill {parsed.verify_skill} that the contract names is not a tracked regular file at the base commit {base}",
                       f"commit the repository's verify skill at {parsed.verify_skill}, or set verify_skill to the tracked skill's path")
    return parsed
