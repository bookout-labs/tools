"""Model assignment. This is data. A node does not get to rewrite it."""

from dataclasses import dataclass
from pathlib import Path
import tomllib

DEFAULT_PATH = Path(__file__).with_name("models.toml")
ROLES = {"coordinator", "builder", "verifier", "check"}
FAMILIES = {"claude", "gpt", "grok"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class NodeConfig:
    role: str
    models: tuple[str, ...]
    other_family_from: str | None = None


@dataclass(frozen=True)
class Config:
    max_review_rounds: int
    nodes: dict[str, NodeConfig]


def family(model: str) -> str:
    prefix = model.split("-", 1)[0]
    if prefix not in FAMILIES:
        raise ConfigError(f"unknown model family: {model}")
    return prefix


def parse_config(data: dict) -> Config:
    if not isinstance(data, dict):
        raise ConfigError("config must be a table")
    rounds = data.get("max_review_rounds")
    if not isinstance(rounds, int) or rounds < 1:
        raise ConfigError("max_review_rounds must be an integer of at least 1")
    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, dict) or not raw_nodes:
        raise ConfigError("nodes must be a non-empty table")
    nodes: dict[str, NodeConfig] = {}
    for name, raw in raw_nodes.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ConfigError(f"bad node entry: {name}")
        role = raw.get("role")
        models = raw.get("models")
        other = raw.get("other_family_from")
        if role not in ROLES:
            raise ConfigError(f"{name} has an unknown role")
        if not isinstance(models, list) or not all(isinstance(item, str) and item for item in models):
            raise ConfigError(f"{name} models must be a list of names")
        if role == "check" and models:
            raise ConfigError(f"{name} is a check and takes no model")
        if role != "check" and not models:
            raise ConfigError(f"{name} needs at least one model")
        for model in models:
            family(model)
        if other is not None and not isinstance(other, str):
            raise ConfigError(f"{name} other_family_from must be a node name")
        nodes[name] = NodeConfig(role=role, models=tuple(models), other_family_from=other)
    for name, node in nodes.items():
        if node.other_family_from and node.other_family_from not in nodes:
            raise ConfigError(f"{name} refers to a missing node {node.other_family_from}")
    return Config(max_review_rounds=rounds, nodes=nodes)


def load_config(path: Path | None = None) -> Config:
    source = path or DEFAULT_PATH
    with source.open("rb") as handle:
        return parse_config(tomllib.load(handle))


def order_for(node: str, config: Config, models_used: dict[str, str]) -> list[str]:
    spec = config.nodes[node]
    models = list(spec.models)
    source = "implement" if node == "review" else spec.other_family_from
    if not source:
        return models
    used = models_used.get(source)
    if not used:
        raise ConfigError(f"{node} needs the {source} model")
    same = family(used)
    others = [model for model in models if family(model) != same]
    if not others:
        raise ConfigError(f"no {node} model outside the {same} family")
    return others
