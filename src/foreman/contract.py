"""The target repository's contract file, parsed strictly (target.yaml target_contract)."""

from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError
import yaml

CONTRACT_PATH = ".foreman/contract.yaml"


class ContractError(ValueError):
    """The contract file does not parse or validate. The message names each problem."""


def _command(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value


def _relative_path(value: str) -> str:
    # rules.paths: a relative path inside the checkout, with no traversal.
    if not value or value.startswith("/") or "\\" in value or "\0" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ValueError("must be a relative path inside the checkout, with no '.', '..' or empty parts")
    return value


Command = Annotated[str, AfterValidator(_command)]
RelativePath = Annotated[str, AfterValidator(_relative_path)]
Seconds = Annotated[int, Field(gt=0)]


class _Strict(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class Start(_Strict):
    command: Command
    health: Command
    timeout_seconds: Seconds


class RelatedRepository(_Strict):
    # The contract pins no commit: admission pins each one, so a commit or sha key is an unknown key.
    repository: Annotated[str, StringConstraints(pattern=r"^[\w.-]+/[\w.-]+$")]
    start: Command


class Mutation(_Strict):
    command: Command
    sample_cap: Annotated[int, Field(gt=0)]


class ProductionCheck(_Strict):
    command: Command
    not_deployed_exit: Annotated[int, Field(ge=1, le=255)]
    timeout_seconds: Seconds


class Contract(_Strict):
    """Only check and verify_skill are required. Each other field becomes required in the slice that first uses it."""

    check: Command
    verify_skill: RelativePath
    test_report: RelativePath | list[RelativePath] | None = None
    image: Command | None = None
    setup: list[Command] = []
    network: Literal["setup", "run"] = "setup"
    start: Start | None = None
    related_repositories: list[RelatedRepository] = []
    secrets: list[Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]] = []
    mutation: Mutation | None = None
    production_check: ProductionCheck | None = None


def _duplicate_key(node: yaml.Node | None) -> str | None:
    """The first key a mapping repeats. YAML keeps the last of two equal keys, so the parse names it instead."""
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key, value in node.value:
            if isinstance(key, yaml.ScalarNode):
                if key.value in seen:
                    return f"duplicate key {key.value!r} at line {key.start_mark.line + 1}"
                seen.add(key.value)
            if found := _duplicate_key(value):
                return found
    if isinstance(node, yaml.SequenceNode):
        return next((found for item in node.value if (found := _duplicate_key(item))), None)
    return None


def parse_contract(text: bytes) -> Contract:
    try:
        source = text.decode("utf-8")
        if duplicate := _duplicate_key(yaml.SafeLoader(source).get_single_node()):
            raise ContractError(duplicate)
        raw: object = yaml.safe_load(source)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractError(f"is not valid UTF-8 YAML: {exc}") from exc
    try:
        return Contract.model_validate(raw)
    except ValidationError as exc:
        raise ContractError("; ".join(f"{'.'.join(map(str, error['loc'])) or 'the file'}: {error['msg']}"
                                      for error in exc.errors())) from exc
