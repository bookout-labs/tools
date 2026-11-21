"""Provenance and claim references for repository-tracked outside-tool evidence."""

import tomllib
from pathlib import PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import Field, HttpUrl, model_validator

from foreman.stage_output import Contract, Text
from foreman.workspace import Workspace

FIXTURE_DIR = "tests/fixtures"
MANIFEST_PATH = f"{FIXTURE_DIR}/manifest.toml"


class FixtureSource(Contract):
    tool: Text
    version: Text
    notes: Text | None = None
    claim_ids: list[Text] = Field(default_factory=list)


class CapturedRun(FixtureSource):
    kind: Literal["captured_run"]
    argv: list[str] = Field(min_length=1)
    returncode: int | None = None
    derived_from: Text | None = None


class QuotedSource(FixtureSource):
    kind: Literal["quoted_source"]
    sources: list[HttpUrl] = Field(min_length=1)


class FixtureManifest(Contract):
    fixtures: dict[Text, Annotated[CapturedRun | QuotedSource, Field(discriminator="kind")]]

    @model_validator(mode="after")
    def derived_captures_match(self) -> Self:
        for path, fixture in self.fixtures.items():
            if isinstance(fixture, CapturedRun) and fixture.derived_from is not None:
                original = self.fixtures.get(fixture.derived_from)
                if not isinstance(original, CapturedRun):
                    raise ValueError(f"{path}: derived_from must name an existing captured run")
                if fixture.derived_from == path or original.derived_from is not None:
                    raise ValueError(f"{path}: derived_from must name an original capture")
                if fixture.argv != original.argv:
                    raise ValueError(f"{path}: argv must match derived_from {fixture.derived_from}")
        return self

    @classmethod
    def from_toml(cls, content: str) -> "FixtureManifest":
        return cls.model_validate(tomllib.loads(content))


def fact_record_path(path: str) -> str:
    parsed = PurePosixPath(path)
    if not path or parsed.is_absolute() or ".." in parsed.parts or parsed.as_posix() != path or path == ".":
        raise ValueError("fact record must be a normalized repository-relative file path")
    return path


def recorded_evidence(workspace: Workspace, head: str, manifest_path: str = MANIFEST_PATH) -> dict[str, list[str]]:
    manifest_path = fact_record_path(manifest_path)
    directory = PurePosixPath(manifest_path).parent.as_posix()
    files = workspace.files_at(head, directory)
    if manifest_path not in files:
        return {}
    manifest = FixtureManifest.from_toml(workspace.read_file_at(head, manifest_path))
    files.remove(manifest_path)
    tracked = {path.removeprefix(f"{directory}/") if directory != "." else path for path in files}
    return {path: fixture.claim_ids for path, fixture in manifest.fixtures.items() if path in tracked}


def recorded_fixtures(workspace: Workspace, head: str, manifest_path: str = MANIFEST_PATH) -> set[str]:
    return set(recorded_evidence(workspace, head, manifest_path))
