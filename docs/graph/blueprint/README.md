# Foreman v2 blueprint

[`target.yaml`](target.yaml) defines the approved target. Review the [workflow diagram](views/target.svg), [glossary](glossary.md), [decisions](decisions.md), [verification plan](verification.md), [migration](migration.md), and [code map](code-map.md). To use the interactive [review index](index.html), open the file in a browser from a local checkout.

The generator reads the Foreman baseline and the [pstack revision](https://github.com/cursor/plugins/tree/d42aff08aa58965d2735c4ead61a9af82b89d0d0) pinned in `target.yaml`. Make both commits available in their respective Git checkouts. From the repository root, run the following command to regenerate the Mermaid sources, inventory, code map, and HTML index.

```sh
uv run --python 3.12 python docs/graph/blueprint/gen.py --pstack /path/to/cursor/plugins --no-svg
```

To regenerate SVGs and their checksums with Node and the pinned Mermaid CLI, omit `--no-svg` and pass `--puppeteer /path/to/puppeteer.json` if you need to set the Chrome executable. Rendering fails if any edge label overlaps another edge label or a node. The error names the diagram and the overlapping text.

CI checks out the Foreman history before the tests run. Set `FOREMAN_PINNED_SKILL_REPO` to your pstack checkout to run text regeneration locally. The SVG check verifies the published Mermaid sources and SVGs against `views/SHA256SUMS` without a browser.

## Check the implementation

Run the projector from the repository root to write `actual.yaml` and print the missing, extra, and mismatched elements.

```sh
uv run --python 3.12 python docs/graph/blueprint/project_actual.py --output actual.yaml
```

The projector compiles Foreman with inert adapters and reads its graph registrations, stage declarations, and source. The generated YAML is for inspection. CI recomputes the projection in the Python 3.12 unittest suite without network access. This is Foreman's equivalent of Warp's pull request configuration check. [Warp factory definition checks](https://docs.warp.dev/factories/factory-as-code/#pull-request-checks)

Run the same check directly.

```sh
uv run --python 3.12 python docs/graph/blueprint/project_actual.py --check
```

Each entry in [`waivers.yaml`](waivers.yaml) names a migration issue and identifies one difference by its scope, expected value, and observed value. After implementing a slice, run the check and delete its stale entries and any empty issue group. Any new or changed difference fails the check. Use the migration issue and the printed diff to investigate before proposing a target change.
