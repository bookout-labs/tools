"""Write graph drawings and their README embedding."""

from pathlib import Path

from foreman.delivery import diagram

LEGEND = "Any step can pause (quota wait, human question, waiting on GitHub) and resumes at that step; review rounds are capped by max_review_rounds."


def drawings() -> dict[str, str]:
    # Slice 13 introduces the compiled dispatch cell. Its drawing is absent until then.
    return {"delivery": diagram(), "dispatch": ""}


def readme_diagrams(drawings: dict[str, str], blueprint: str, dispatch_blueprint: str) -> str:
    native = "\n\n".join(f"```mermaid\n{text}```" for text in drawings.values() if text)
    return (f"### Blueprint\n\n```mermaid\n{blueprint}```\n\n{LEGEND}\n\n"
            f"```mermaid\n{dispatch_blueprint}```\n\n### Today's code\n\n{native}")


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
    blueprint = (graph / "target.mmd").read_text()
    dispatch_blueprint = (graph / "dispatch-target.mmd").read_text()
    pictures = drawings()
    for name, drawing in pictures.items():
        (graph / f"{name}.mmd").write_text(drawing)
    readme.write_text(before + start + "\n" + readme_diagrams(pictures, blueprint, dispatch_blueprint) + "\n" + end + after)
