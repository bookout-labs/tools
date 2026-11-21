import tests

import json
from pathlib import Path
import re
import unittest


class CorpusSchemaTest(unittest.TestCase):
    def test_artifact_paths_are_relative_filenames(self):
        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / 'evals/corpus/case.schema.json').read_text())
        pattern = schema['$defs']['artifact']['properties']['path']['pattern']
        for path in ('request.md', 'nested/answer.py', '.hidden', 'a..b/file'):
            with self.subTest(path=path):
                self.assertIsNotNone(re.fullmatch(pattern, path))
        for path in ('', '/', '/request.md', '.', './request.md', 'a/./b', 'a/.',
                     '..', '../request.md', 'a/../b', 'a/..', 'a//b', 'a/'):
            with self.subTest(path=path):
                self.assertIsNone(re.fullmatch(pattern, path))
