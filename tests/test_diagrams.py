from pathlib import Path
import re
import tomllib
import unittest

from foreman.config import load_config
from foreman.delivery import DryRun, compile_delivery
from foreman.diagrams import drawings, readme_diagrams
from foreman.dispatch import FailoverDispatcher
from foreman.dry_run import ScriptHandler


ROOT = Path(__file__).resolve().parents[1]
GRAPH = ROOT / "docs/graph"
EDGE = re.compile(
    r'^\s*(\w+)(?:\[.*?\]|\(.*?\))?\s+'
    r'(?:-->(?:\|([^|]+)\|)?|-\.(?:->| &nbsp;(.*?)&nbsp; \.->))\s*(\w+)'
)


def edges(text):
    result = set()
    for line in text.splitlines():
        if line.lstrip().startswith("%%") or not re.search(r"[-=]+>", line):
            continue
        match = EDGE.match(line)
        if match is None:
            raise ValueError(f"Unrecognized Mermaid edge: {line}")
        source, label, native_label, target = match.groups()
        result.add((source, target, label or native_label or ""))
    return result


class DiagramTests(unittest.TestCase):
    def test_native_drawings_and_readme_are_exact(self):
        compiled = compile_delivery(FailoverDispatcher(ScriptHandler()), load_config(), run=DryRun())
        pictures = drawings()
        self.assertEqual(pictures["delivery"], compiled.get_graph().draw_mermaid())
        for name, text in pictures.items():
            self.assertEqual((GRAPH / f"{name}.mmd").read_bytes(), text.encode())
        readme = (ROOT / "README.md").read_text()
        embedded = readme.split("<!-- graph:start -->\n", 1)[1].split("\n<!-- graph:end -->", 1)[0]
        self.assertEqual(embedded, readme_diagrams(pictures))

    def test_drift_is_exactly_the_scheduled_gaps(self):
        path = GRAPH / "gaps.toml"
        gaps = tomllib.loads(path.read_text()) if path.exists() else {}
        for name in ("delivery", "dispatch"):
            actual = edges((GRAPH / f"{name}.mmd").read_text())
            target = edges((GRAPH / ("target.mmd" if name == "delivery" else "dispatch-target.mmd")).read_text())
            for direction, difference in (("extra", actual - target), ("missing", target - actual)):
                recorded = []
                for slice_name, sources in gaps.get(name, {}).get(direction, {}).items():
                    self.assertRegex(slice_name, r"^slice_(?:[2-9]|1[0-6])(?:[ab])?$")
                    for source, targets in sources.items():
                        recorded.extend((source, target, label) for target, label in targets)
                self.assertEqual(len(recorded), len(set(recorded)), "duplicate gap")
                self.assertEqual(set(recorded), difference, f"{name} {direction}")

    def test_edge_labels_and_invalid_syntax(self):
        self.assertEqual(edges('a -->|fail: fix| b["step"]\na -. &nbsp;call&nbsp; .-> c;'),
                         {("a", "b", "fail: fix"), ("a", "c", "call")})
        with self.assertRaises(ValueError):
            edges("a ==> b")
