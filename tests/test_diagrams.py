import ast
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from foreman.cli import main
from foreman.delivery import compile_delivery, diagram


ROOT = Path(__file__).resolve().parents[1]


class DiagramTests(unittest.TestCase):
    def test_model_stage_loops_do_not_filter_verify(self):
        tree = ast.parse(Path(compile_delivery.__code__.co_filename).read_text())
        comparisons = (node for node in ast.walk(tree) if isinstance(node, ast.Compare))
        self.assertFalse(any(isinstance(value, ast.Constant) and value.value == "verify" for node in comparisons for value in node.comparators))

    def test_publication_transition_tests_exist(self):
        module = ast.parse((ROOT / "tests/test_publication.py").read_text())
        methods = {
            method.name
            for cls in module.body if isinstance(cls, ast.ClassDef)
            for method in cls.body
            if isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef))
            and method.name.startswith("test_")
        }
        document = (ROOT / "docs/publication-recovery.md").read_text()
        self.assertIn("## Transition tests\n", document)
        section = document.split("## Transition tests\n", 1)[1].split("\n## ", 1)[0]
        table = re.search(
            r"(?m)^\| Transition or crash window \| Test \|\n\| --- \| --- \|\n((?:\|.*\|\n)*)",
            section,
        )
        if table is None:
            self.fail("publication transition table is missing")
        rows = table[1].splitlines()
        self.assertTrue(rows, "publication transition table has no rows")
        references = {f"`{name}`" for name in methods}
        for row in rows:
            transition, cell = row[1:-1].split("|")
            self.assertIn(cell.strip(), references, transition.strip())

    def test_write_embeds_compiled_delivery_diagram(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph = root / "docs/graph"
            graph.mkdir(parents=True)
            readme = root / "README.md"
            readme.write_text("before\n<!-- graph:start -->\nold\n<!-- graph:end -->\nafter\n")

            with patch("foreman.cli.Path.cwd", return_value=root):
                self.assertEqual(main(["diagram", "--write"]), 0)

            drawing = diagram()
            self.assertEqual((graph / "delivery.mmd").read_text(), drawing)
            self.assertEqual(readme.read_text(),
                             f"before\n<!-- graph:start -->\n### Today's code\n\n```mermaid\n{drawing}"
                             "```\n<!-- graph:end -->\nafter\n")

    def test_write_rejects_missing_directory_or_markers_without_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            graph = root / "docs/graph"
            readme = root / "README.md"
            for text in (None, "missing markers", "<!-- graph:end --><!-- graph:start -->"):
                if text is not None:
                    graph.mkdir(parents=True, exist_ok=True)
                    readme.write_text(text)
                error = StringIO()
                with patch("foreman.cli.Path.cwd", return_value=root), redirect_stderr(error):
                    self.assertEqual(main(["diagram", "--write"]), 2)
                self.assertIn("cannot write diagrams:", error.getvalue())
                self.assertFalse((graph / "delivery.mmd").exists())
                if text is not None:
                    self.assertEqual(readme.read_text(), text)
