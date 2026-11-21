import ast
from pathlib import Path
import re
import tomllib
import unittest
from contextlib import redirect_stderr
from io import StringIO
from tempfile import TemporaryDirectory
from unittest.mock import patch

from foreman.cli import main
from foreman.config import load_config
from foreman.delivery import DryRun, compile_delivery
from foreman.diagrams import LEGEND, drawings, readme_diagrams
from foreman.dry_run import ScriptHandler


ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs/graph"
SLICES = frozenset(f"slice_{name}" for name in (
    "1", "2", "3", "4", "5", "6", "7", "8a", "8b", "9", "10a", "10b",
    "11a", "11b", "12", "13", "14a", "14b", "15", "16",
))
SHAPE = r'(?:\["[^"\n]*"\]|\(\[[^\]\n]*\]\)|\([^()\n]*\))'
NODE = rf'\w+(?:{SHAPE})?'
EDGE = re.compile(
    rf'\s*(\w+)(?:{SHAPE})?\s*(?:-->(?:\|([^|]+)\|)?|-\.(?:->| &nbsp;(.*?)&nbsp; \.->))'
    rf'\s*(\w+)(?:{SHAPE})?\s*;?\s*'
)


def edges(text):
    result = set()
    lines = text.splitlines()
    if lines and lines[0] == "---":
        lines = lines[lines.index("---", 1) + 1:]
    for line in lines:
        if not line.strip() or line.lstrip().startswith("%%"):
            continue
        match = EDGE.fullmatch(line)
        if match is not None:
            source, label, native_label, target = match.groups()
            result.add((source, target, label or native_label or ""))
        elif any(marker in line for marker in ('--', '-.', '==')) or not re.fullmatch(
            rf'\s*(?:(?:graph|flowchart) TD;?|{NODE}(?:[:]{{3}}\w+)?|classDef \w+ [^;]+;?)\s*', line,
        ):
            raise ValueError(f"Unrecognized Mermaid line: {line}")
    return result


class DiagramTests(unittest.TestCase):
    def test_native_drawings_and_readme_are_exact(self):
        compiled = compile_delivery(ScriptHandler(), load_config(), run=DryRun())
        pictures = drawings()
        self.assertEqual(pictures["delivery"], compiled.get_graph().draw_mermaid())
        for name, text in pictures.items():
            self.assertEqual((GRAPH / f"{name}.mmd").read_bytes(), text.encode())
        readme = (ROOT / "README.md").read_text()
        embedded = readme.split("<!-- graph:start -->\n", 1)[1].split("\n<!-- graph:end -->", 1)[0]
        blueprint = (GRAPH / "target.mmd").read_text()
        dispatch_blueprint = (GRAPH / "dispatch-target.mmd").read_text()
        self.assertEqual(embedded, readme_diagrams(pictures, blueprint, dispatch_blueprint))
        self.assertIn(f"### Blueprint\n\n```mermaid\n{blueprint}```\n\n{LEGEND}", embedded)
        self.assertIn(f"### Today's code\n\n```mermaid\n{pictures['delivery']}```", embedded)
        self.assertIn(f"```mermaid\n{dispatch_blueprint}```", embedded)

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

    def test_drift_is_exactly_the_scheduled_gaps(self):
        path = GRAPH / "gaps.toml"
        gaps = tomllib.loads(path.read_text()) if path.exists() else {}
        for name in ("delivery", "dispatch"):
            actual = edges((GRAPH / f"{name}.mmd").read_text())
            target = edges((GRAPH / ("target.mmd" if name == "delivery" else "dispatch-target.mmd")).read_text())
            for direction, difference in (("extra", actual - target), ("missing", target - actual)):
                recorded = []
                for slice_name, sources in gaps.get(name, {}).get(direction, {}).items():
                    self.assertIn(slice_name, SLICES)
                    for source, targets in sources.items():
                        recorded.extend((source, target, label) for target, label in targets)
                self.assertEqual(len(recorded), len(set(recorded)), "duplicate gap")
                self.assertEqual(set(recorded), difference, f"{name} {direction}")

    def test_stage_targets_and_gap_owners(self):
        targets = {path.name.removesuffix(".target.mmd")
                   for path in (GRAPH / "stages").glob("*.target.mmd")}
        blueprint = edges((GRAPH / "target.mmd").read_text())
        steps = {node for source, target, _ in blueprint for node in (source, target)}
        steps = (steps - {"__start__", "__end__"}) | {"dispatch_cell"}
        self.assertEqual(targets, steps, "missing or orphan stage target")
        gaps = tomllib.loads((GRAPH / "gaps.toml").read_text()).get("stages", {})
        self.assertLessEqual(set(gaps), targets, "orphan stage gap")
        for owners in gaps.values():
            self.assertTrue(owners)
            self.assertLessEqual(set(owners), SLICES)
            for lines in owners.values():
                self.assertTrue(lines)
                self.assertTrue(all(line.strip() for line in lines))
        self.assertFalse(any(name.startswith("stages/") for name in drawings()))

    def test_edge_labels_and_invalid_syntax(self):
        self.assertEqual(edges('a -->|fail: fix| b["step"]\na -. &nbsp;call&nbsp; .-> c;'),
                         {("a", "b", "fail: fix"), ("a", "c", "call")})
        for line in ("a ==> b", "a --> b --> c", "a-->b-->c", "a --- b", "a -.- c", "a --x d", 'a --> b["step"] --> c["step"]'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                edges(line)
        self.assertEqual(edges("done --> __end__([End])"), {("done", "__end__", "")})

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
