"""Write native graph drawings and their README embedding."""

from pathlib import Path

from foreman.delivery import diagram


def drawings() -> dict[str, str]:
    # Slice 13 introduces the compiled dispatch cell. Its drawing is absent until then.
    return {"delivery": diagram(), "dispatch": ""}


def readme_diagrams(drawings: dict[str, str]) -> str:
    return "\n\n".join(f"```mermaid\n{text}```" for text in drawings.values() if text)


def write_diagrams(root: Path) -> None:
    pictures = drawings()
    for name, text in pictures.items():
        (root / "docs" / "graph" / f"{name}.mmd").write_text(text)
    readme = root / "README.md"
    text = readme.read_text()
    start, end = "<!-- graph:start -->", "<!-- graph:end -->"
    before, rest = text.split(start, 1)
    _, after = rest.split(end, 1)
    readme.write_text(before + start + "\n" + readme_diagrams(pictures) + "\n" + end + after)
