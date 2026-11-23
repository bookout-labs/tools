"""Write graph drawings and their README embedding."""

from pathlib import Path

from foreman.delivery import diagram


def write_diagrams(root: Path) -> None:
    graph = root / "docs" / "graph"
    if not graph.is_dir():
        raise ValueError("run from the repository root containing docs/graph and README.md")
    readme = root / "README.md"
    text = readme.read_text()
    start, end = "<!-- graph:start -->", "<!-- graph:end -->"
    if text.count(start) != 1 or text.count(end) != 1 or text.index(start) > text.index(end):
        raise ValueError("README.md must contain ordered graph:start and graph:end markers exactly once")
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    drawing = diagram()
    (graph / "delivery.mmd").write_text(drawing)
    readme.write_text(before + f"{start}\n### Today's code\n\n```mermaid\n{drawing}```\n{end}" + after)
