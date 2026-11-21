from pathlib import Path
import unittest

from foreman.fixture_manifest import FIXTURE_DIR, MANIFEST_PATH, FixtureManifest


class FixtureManifestTest(unittest.TestCase):
    def test_manifest_matches_fixture_inventory(self):
        repo = Path(__file__).resolve().parents[1]
        root = repo / FIXTURE_DIR
        manifest_path = repo / MANIFEST_PATH
        manifest = FixtureManifest.from_toml(manifest_path.read_text(encoding="utf-8"))
        files = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path != manifest_path and path.is_file() and not path.is_symlink()
        }
        with self.subTest("fixtures without manifest entries"):
            self.assertEqual(files - manifest.fixtures.keys(), set())
        with self.subTest("manifest entries without fixtures"):
            self.assertEqual(manifest.fixtures.keys() - files, set())
