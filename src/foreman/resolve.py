"""Resolve a route name to the newest model the installed CLI will run."""

from dataclasses import dataclass
import json
import re
import subprocess

from foreman.config import EFFORTS, ConfigError

GROK_SLUG = re.compile(r"(?<![\w.-])grok-\d+(?:\.\d+)*(?![\w.-])")
VERSION = re.compile(r"^\d+(?:\.\d+)*$")


@dataclass(frozen=True)
class Cmd:
    code: int
    stdout: str
    stderr: str = ""


def run_cmd(args: list[str]) -> Cmd:
    try:
        result = subprocess.run(
            args, stdin=subprocess.DEVNULL, capture_output=True, text=True, shell=False,
        )
    except FileNotFoundError as exc:
        raise ConfigError(f"{args[0]} CLI is not installed") from exc
    return Cmd(result.returncode, result.stdout, result.stderr)


def _version(slug: str) -> tuple[int, ...] | None:
    numbers = [tuple(int(part) for part in piece.split(".")) for piece in slug.split("-") if VERSION.fullmatch(piece)]
    return numbers[-1] if numbers else None


def _levels(model: dict) -> set[str]:
    levels = model.get("supported_reasoning_levels") or []
    return {item.get("effort") for item in levels if isinstance(item, dict)}


def newest_codex(catalog: dict, route: str, effort: str) -> str:
    models = catalog.get("models") if isinstance(catalog, dict) else None
    if not isinstance(models, list):
        raise ConfigError("codex debug models did not return a catalog")
    matches: list[tuple[tuple[int, ...], str, set[str]]] = []
    for model in models:
        if not isinstance(model, dict) or not isinstance(model.get("slug"), str):
            continue
        slug = model["slug"]
        if route not in slug.split("-"):
            continue
        version = _version(slug)
        if version is None:
            continue
        matches.append((version, slug, _levels(model)))
    if not matches:
        raise ConfigError(f"no {route} model in the Codex catalog")
    _version_key, slug, levels = max(matches)
    if effort not in levels:
        raise ConfigError(f"{slug} does not support effort {effort}")
    return f"{slug}-{effort}"


def newest_grok(text: str) -> str:
    found = GROK_SLUG.findall(text)
    if not found:
        raise ConfigError("grok models did not list a versioned model")
    return max(found, key=lambda slug: _version(slug) or ())


def resolve_route(route: str, effort: str, run=run_cmd) -> str:
    if effort not in EFFORTS:
        raise ConfigError(f"unknown effort {effort}")
    try:
        if route in {"sol", "terra"}:
            result = run(["codex", "debug", "models"])
        elif route == "grok":
            result = run(["grok", "models"])
        elif route == "opus":
            result = run(["claude", "--version"])
        else:
            raise ConfigError(f"unknown route {route}")
    except FileNotFoundError as exc:
        raise ConfigError(f"{exc} CLI is not installed") from exc
    if route in {"sol", "terra"}:
        if result.code != 0:
            raise ConfigError("codex debug models failed")
        try:
            catalog = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ConfigError("codex debug models did not return JSON") from exc
        return newest_codex(catalog, route, effort)
    if route == "grok":
        if result.code != 0:
            raise ConfigError("grok models failed")
        return newest_grok(result.stdout)
    if result.code != 0:
        raise ConfigError("claude CLI is not installed")
    # Claude Code has no model catalog. The opus alias is that CLI's newest opus.
    return "opus"


def cache_resolve(resolve):
    seen: dict[tuple[str, str], str] = {}

    def cached(route: str, effort: str) -> str:
        key = (route, effort)
        if key not in seen:
            seen[key] = resolve(route, effort)
        return seen[key]

    return cached
