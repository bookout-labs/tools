# Foreman v3 blueprint

[`target.yaml`](target.yaml) defines the approved target. Review the [workflow diagram](views/target.svg), [glossary](glossary.md), [decisions](decisions.md), [verification plan](verification.md), [migration](migration.md), and [code map](code-map.md). To use the interactive [review index](index.html), open the file in a browser from a local checkout.

The generator reads the Foreman baseline and the [pstack](https://github.com/cursor/plugins/tree/d42aff08aa58965d2735c4ead61a9af82b89d0d0) and [mattpocock/skills](https://github.com/mattpocock/skills/tree/abeeaf95660ba9e8b7c753eb38f441ca707a8823) revisions pinned in `target.yaml`. Make each commit available in its own Git checkout. From the repository root, run the following command to regenerate the Mermaid sources, inventory, code map, and HTML index.

```sh
uv run --python 3.12 python docs/graph/blueprint/gen.py --pstack /path/to/cursor/plugins --matt-skills /path/to/mattpocock/skills --no-svg
```

To regenerate SVGs and their checksums with Node and the pinned Mermaid CLI, omit `--no-svg` and pass `--puppeteer /path/to/puppeteer.json` if you need to set the Chrome executable. Rendering fails if an edge label overlaps another label or a node, or if a connector crosses another edge's label or a node outside its endpoints. The error names the diagram and the elements involved.

CI checks out the Foreman history before the tests run. Set `FOREMAN_PINNED_SKILL_REPO` to your pstack checkout and `FOREMAN_PINNED_MATT_SKILLS` to your mattpocock/skills checkout to run text regeneration locally. The SVG check verifies the published Mermaid sources and SVGs against `views/SHA256SUMS` without a browser.

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

Each entry in [`waivers.yaml`](waivers.yaml) names a migration issue and identifies one difference by its scope, expected value, and observed value. After implementing a slice, run the check and delete its stale entries and any empty issue group. Re-key an entry whose difference only narrowed or whose runtime prompt hash changed, keeping its subject narrowed to the items still open (see [verification](verification.md)). Any new or changed difference fails the check. Use the migration issue and the printed diff to investigate before proposing a target change.
