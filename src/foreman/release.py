"""The commit of Foreman's own code: the release a run saves at its first start, and the one the smoke proves."""

import subprocess
from importlib.metadata import distribution
from urllib.parse import urlparse
from urllib.request import url2pathname

from pydantic import BaseModel


class _VcsInfo(BaseModel):
    commit_id: str


class _DirInfo(BaseModel):
    editable: bool = False


class _DirectUrl(BaseModel):
    """Where the installer took the install from, as PEP 610's direct_url.json records it."""

    url: str
    vcs_info: _VcsInfo | None = None
    dir_info: _DirInfo | None = None


def release() -> str:
    """The commit this install runs, from what its installer recorded.

    A git install, such as the receiver's `uvx --from git+...@<ref>`, records the commit it resolved. An editable checkout, as
    `uv run` installs one, runs its HEAD while no tracked file differs from it. Any other install, or a checkout with tracked
    changes, runs no commit, and the answer is empty.
    """
    recorded = distribution("bookout-tools").read_text("direct_url.json")
    if recorded is None:
        return ""
    origin = _DirectUrl.model_validate_json(recorded)
    if origin.vcs_info is not None:
        return origin.vcs_info.commit_id
    if origin.dir_info is None or not origin.dir_info.editable:
        return ""
    root = url2pathname(urlparse(origin.url).path)
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True).stdout.strip()
        changed = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root,
                                 capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return ""
    return "" if changed else head
