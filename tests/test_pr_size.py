import tests

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest

from foreman.workspace import LiveWorkspace
from test_runner import BRANCH


class InspectionAttributesTest(unittest.TestCase):
    """Inspection uses the base attributes and ignores ambient grep settings."""

    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.root = directory / "checkout"
        self.root.mkdir()
        remote = directory / "origin.git"
        self.git("init", "--bare", str(remote))
        self.git("init", "-b", "main")
        self.git("remote", "add", "origin", str(remote))
        self.git("config", "user.name", "Synthetic Test")
        self.git("config", "user.email", "synthetic@example.invalid")
        self.write(".gitattributes", (Path(__file__).resolve().parents[1] / ".gitattributes").read_text())
        self.write("src/items.py", "old item\n")
        self.base = self.commit()
        # Main moves past the branch point, so only the merge base can give the PR's count.
        self.write("src/main-only.py", "main\n" * 401)
        self.commit()
        self.git("push", "origin", "main")
        self.git("switch", "-c", BRANCH, self.base)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def write(self, path, text):
        file = self.root / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(text)

    def commit(self):
        self.git("add", ".")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", "Synthetic change")
        return self.git("rev-parse", "HEAD")

    def test_hardened_grep_uses_base_attributes_and_basic_patterns(self):
        self.write("src/items.py", "first\nlast")
        self.write(".gitattributes", "src/items.py -diff\n")
        head = self.commit()
        self.git("config", "grep.patternType", "fixed")
        self.git("config", "grep.extendedRegexp", "true")
        self.git("config", "color.grep", "always")
        self.write("../local-attributes", "src/items.py -diff\n")
        self.git("config", "core.attributesFile", str(self.root.parent / "local-attributes"))
        raw = LiveWorkspace(self.root)._inspection_git(
            self.base, "grep", "-c", "-I", "-z", "--no-textconv", "-e", "^", head,
            "--", "src/items.py", allowed_returncodes=(0, 1),
        )
        self.assertEqual(raw, head.encode() + b":src/items.py\x002\n")
