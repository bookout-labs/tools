"""Provenance for outside-tool evidence under tests/fixtures."""

import tomllib
from typing import Annotated, Literal

from pydantic import Field, HttpUrl

from foreman.stage_output import Contract, Text

FIXTURE_DIR = "tests/fixtures"
MANIFEST_PATH = f"{FIXTURE_DIR}/manifest.toml"


class FixtureSource(Contract):
    tool: Text
    version: Text
    notes: Text | None = None


class CapturedRun(FixtureSource):
    kind: Literal["captured_run"]
    argv: list[str] = Field(min_length=1)
    derived_from: Text | None = None


class QuotedSource(FixtureSource):
    kind: Literal["quoted_source"]
    argv: list[str] = Field(max_length=0)
    sources: list[HttpUrl] = Field(min_length=1)


class FixtureManifest(Contract):
    fixtures: dict[Text, Annotated[CapturedRun | QuotedSource, Field(discriminator="kind")]]

    @classmethod
    def from_toml(cls, content: str) -> "FixtureManifest":
        return cls.model_validate(tomllib.loads(content))
