from foreman.dry_run import DryRun
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
from foreman.delivery import LiveRun, compile_delivery
from foreman.diagrams import LEGEND, drawings, readme_diagrams
from foreman.dry_run import ScriptHandler


ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs/graph"
SLICES = frozenset(f"slice_{name}" for name in (
    "1", "2", "3", "4", "5", "6", "7", "8a", "8b", "9", "10a", "10b",
    "11a", "11b", "12", "13", "14a", "14b", "15", "16",
))
SHAPE = r'(?:\["[^"\n]*"\]|\(\[[^\]\n]*\]\)|\([^()\n]*\)|\{\{"[^"\n]*"\}\}|\{"[^"\n]*"\}|\[/"[^"\n]*"/\])'
NODE = rf'\w+(?:{SHAPE})?(?::{{3}}\w+)?'
EDGE = re.compile(
    rf'\s*({NODE})\s*(?:-->(?:\|([^|]+)\|)?|-\.(?:->| &nbsp;(.*?)&nbsp; \.->))'
    rf'\s*({NODE})\s*;?\s*'
)


def edges(text):
    result, declared, connected = set(), set(), set()
    lines = text.splitlines()
    if lines and lines[0] == "---":
        lines = lines[lines.index("---", 1) + 1:]
    for line in lines:
        if not line.strip() or line.lstrip().startswith("%%"):
            continue
        match = EDGE.fullmatch(line)
        if match is not None:
            source, label, native_label, target = match.groups()
            source, target = re.match(r'\w+', source)[0], re.match(r'\w+', target)[0]
            edge = (source, target, label or native_label or "")
            if edge in result:
                raise ValueError(f"Duplicate Mermaid edge: {line}")
            result.add(edge)
            connected.update((source, target))
        elif re.fullmatch(rf'\s*{NODE}\s*;?\s*', line):
            declared.add(re.match(r'\s*(\w+)', line)[1])
        elif not re.fullmatch(
            r'\s*(?:(?:graph|flowchart) TD;?|classDef \w+ [^;]+;?)\s*', line,
        ):
            raise ValueError(f"Unrecognized Mermaid line: {line}")
    return result, declared - connected


class DiagramTests(unittest.TestCase):
    def test_model_stage_loops_do_not_filter_verify(self):
        tree = ast.parse(Path(compile_delivery.__code__.co_filename).read_text())
        comparisons = (node for node in ast.walk(tree) if isinstance(node, ast.Compare))
        self.assertFalse(any(isinstance(value, ast.Constant) and value.value == "verify" for node in comparisons for value in node.comparators))

    def test_native_drawings_and_readme_are_exact(self):
        compiled = compile_delivery(ScriptHandler(), load_config(), run=LiveRun(forge=DryRun(), workspace=DryRun()))
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
            actual, actual_isolated = edges((GRAPH / f"{name}.mmd").read_text())
            target, target_isolated = edges((GRAPH / ("target.mmd" if name == "delivery" else "dispatch-target.mmd")).read_text())
            # Node labels, shapes and arrow kinds are presentation until #137 agrees stage semantics.
            self.assertEqual(actual_isolated, target_isolated, f"{name} isolated nodes")
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
        blueprint, _ = edges((GRAPH / "target.mmd").read_text())
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
                         ({("a", "b", "fail: fix"), ("a", "c", "call")}, set()))
        for line in ("a ==> b", "a --> b --> c", "a-->b-->c", "a --- b", "a -.- c", "a --x d", 'a --> b["step"] --> c["step"]'):
            with self.subTest(line=line), self.assertRaises(ValueError):
                edges(line)
        self.assertEqual(edges("done --> __end__([End])"), ({("done", "__end__", "")}, set()))

    def test_added_isolated_halt_changes_projection(self):
        original = 'a --> b'
        self.assertNotEqual(edges(original), edges(original + '\nh["HALT invalid_output"]:::halt'))
        self.assertEqual(edges('h["HALT"]\na --> b'), ({("a", "b", "")}, {"h"}))
        self.assertEqual(edges('b["step"]\na --> b'), edges(original))
        read_text = Path.read_text
        def mutated(path, *args, **kwargs):
            return read_text(path, *args, **kwargs) + ('\nh["HALT"]' if path == GRAPH / "delivery.mmd" else '')
        with patch.object(Path, "read_text", mutated), self.assertRaisesRegex(AssertionError, "delivery isolated nodes"):
            self.test_drift_is_exactly_the_scheduled_gaps()

    def test_duplicate_edge_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate Mermaid edge"):
            edges('a --> b\na --> b')

    def test_stage_class_syntax_is_presentation(self):
        for path in (GRAPH / "stages").glob("*.target.mmd"):
            with self.subTest(path=path.name):
                text = path.read_text()
                self.assertEqual(edges(text), edges(re.sub(r':::\w+', '', text)))

    def test_unsupported_syntax_mutation_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unrecognized Mermaid line"):
            edges('a --> b\na ==> c')

    def test_order_whitespace_comments_and_style_are_presentation(self):
        self.assertEqual(edges('a["start"] --> b\nb --> c\nh["halt"]'),
                         edges('%% comment\nh["halt"]:::halt\n  b-->c;\na["new label"] -.-> b'))

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
