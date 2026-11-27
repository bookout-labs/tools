"""The target repository's contract file, parsed strictly (target.yaml target_contract)."""

from collections.abc import Hashable
import re
from typing import Annotated, Any, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from typing_extensions import override
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
    if (not value.strip() or value.startswith("/") or "\\" in value or "\0" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))):
        raise ValueError("must be a relative path inside the checkout, with no blank, '.', '..' or empty parts")
    return value


def _repository(value: str) -> str:
    # OWNER/NAME in GitHub's ASCII name characters. A '.' or '..' segment would leave the checkout it names.
    if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", value) or any(part in {".", ".."} for part in value.split("/")):
        raise ValueError("must be OWNER/NAME in letters, digits, '.', '_' and '-', with no '.' or '..' part")
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
    repository: Annotated[str, AfterValidator(_repository)]
    start: Command


class Mutation(_Strict):
    command: Command
    sample_cap: Annotated[int, Field(gt=0)]


class ProductionCheck(_Strict):
    command: Command
    not_deployed_exit: Annotated[int, Field(ge=1, le=255)]
    timeout_seconds: Seconds


class Contract(_Strict):
    """A field without a default becomes required in the slice that first uses it. A field with one stays optional."""

    check: Command
    check_timeout_seconds: Seconds = 1800
    verify_skill: RelativePath
    test_report: RelativePath | Annotated[list[RelativePath], Field(min_length=1)]
    image: Command
    setup: list[Command]
    network: Literal["setup", "run"] = "setup"
    start: Start | None = None
    related_repositories: list[RelatedRepository] = []
    secrets: list[Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]*$")]]
    mutation: Mutation | None = None
    production_check: ProductionCheck | None = None

    @property
    def reports(self) -> list[str]:
        """The JUnit XML reports the check writes, as a list."""
        return [self.test_report] if isinstance(self.test_report, str) else self.test_report


class _Constructor(Protocol):
    # PyYAML's stubs leave construct_object's node untyped, so the loader names the signature it calls.
    def construct_object(self, node: yaml.Node, deep: bool = False) -> object: ...


def _construct(constructor: _Constructor, node: yaml.Node, deep: bool) -> object:
    return constructor.construct_object(node, deep)


class _ContractLoader(yaml.SafeLoader):
    """Loads each mapping key as written. YAML keeps the last of two equal keys and folds a merge key's mapping in,
    so the load names a repeated key and refuses a merge key, and every key reaches the strict model."""

    @override
    def construct_mapping(self, node: yaml.Node, deep: bool = False) -> dict[Hashable, Any]:
        # A !!map tag on a scalar or sequence reaches here too, so the node's kind is checked first.
        if not isinstance(node, yaml.MappingNode):
            raise ContractError(f"a {node.tag} value at line {node.start_mark.line + 1} is not a mapping")
        mapping: dict[Hashable, Any] = {}
        for key_node, value_node in node.value:
            line = key_node.start_mark.line + 1
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise ContractError(f"merge key '<<' at line {line} is not allowed")
            key = _construct(self, key_node, deep)
            if not isinstance(key, Hashable):
                raise ContractError(f"key at line {line} is not a scalar")
            if key in mapping:
                raise ContractError(f"duplicate key {key!r} at line {line}")
            mapping[key] = _construct(self, value_node, deep)
        return mapping


def parse_contract(text: bytes) -> Contract:
    try:
        raw: object = yaml.load(text.decode("utf-8"), Loader=_ContractLoader)
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ContractError(f"is not valid UTF-8 YAML: {exc}") from exc
    try:
        return Contract.model_validate(raw)
    except ValidationError as exc:
        raise ContractError("; ".join(f"{'.'.join(map(str, error['loc'])) or 'the file'}: {error['msg']}"
                                      for error in exc.errors())) from exc
