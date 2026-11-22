# Foreman v2 blueprint

[`target.yaml`](target.yaml) defines the approved target. Review the [workflow diagram](views/target.svg), [glossary](glossary.md), [decisions](decisions.md), [verification plan](verification.md), [migration](migration.md), and [code map](code-map.md). To use the interactive [review index](index.html), open the file in a browser from a local checkout.

The generator reads the Foreman baseline and the [pstack revision](https://github.com/cursor/plugins/tree/d42aff08aa58965d2735c4ead61a9af82b89d0d0) pinned in `target.yaml`. Make both commits available in their respective Git checkouts. From the repository root, run the following command to regenerate the Mermaid sources, inventory, code map, and HTML index.

```sh
uv run --python 3.12 python docs/graph/blueprint/gen.py --pstack /path/to/cursor/plugins --no-svg
```

To regenerate SVGs and their checksums with Node and the pinned Mermaid CLI, omit `--no-svg` and pass `--puppeteer /path/to/puppeteer.json` if you need to set the Chrome executable.

CI checks out the Foreman history before the tests run. Set `FOREMAN_PINNED_SKILL_REPO` to your pstack checkout to run text regeneration locally. The SVG check verifies the published Mermaid sources and SVGs against `views/SHA256SUMS` without a browser.

The projection guide (`actual-projection.md`), projector (`project_actual.py`), and its outputs (`actual.yaml`, `diff.txt`, and `waivers.yaml`) belong to the projection check described in the [migration](migration.md). The design package's `synthesis-notes.md` records the rationale for combining earlier proposals. The [decisions](decisions.md) and [migration](migration.md) describe the choices that apply to this blueprint.
