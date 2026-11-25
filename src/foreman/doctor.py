"""Doctor: plain code that proves a target repository is ready for Foreman (target.yaml doctor).

The quick part runs at admission before any paid call. This module holds its two contract requirements.
"""

from foreman.contract import CONTRACT_PATH, Contract, ContractError, parse_contract
from foreman.workspace import Workspace


class NotReady(Exception):
    """One missing requirement and its exact fix."""

    def __init__(self, requirement: str, fix: str):
        super().__init__(f"Not ready: {requirement}. Fix: {fix}.")


def quick(workspace: Workspace, base: str) -> Contract:
    """The contract at the base commit, once the contract file validates and names a tracked verify skill."""
    text = workspace.tracked_file(base, CONTRACT_PATH)
    if text is None:
        raise NotReady(f"the contract file {CONTRACT_PATH} is not a tracked file at the base commit {base}",
                       f"commit {CONTRACT_PATH} to the default branch with at least its check and verify_skill fields")
    try:
        contract = parse_contract(text)
    except ContractError as exc:
        raise NotReady(f"the contract file {CONTRACT_PATH} does not validate: {exc}",
                       f"correct each named key in {CONTRACT_PATH} on the default branch") from exc
    if not workspace.has_tracked_file(base, contract.verify_skill):
        raise NotReady(f"the verify skill {contract.verify_skill} that the contract names is not a tracked regular file at the base commit {base}",
                       f"commit the repository's verify skill at {contract.verify_skill}, or set verify_skill to the tracked skill's path")
    return contract
