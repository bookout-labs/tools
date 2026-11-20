Write the spec. Cover the behavior, the product decisions, and the technical design.
Reply ready when a person could implement from the spec.
Reply needs_human when a product choice is still open.
On a parent issue, return one slice per pull request. Keep each pull request to about 400 changed lines or fewer.
Each slice has exactly these keys.

- `title` names the outcome.
- `acceptance` states how to check the outcome.
- `depends_on` lists other slice titles that must land first. Use an empty list when independent.
- `screen` gives the proof obligation as a path like `docs/screens/deals.md`. Use an empty string for work without a screen. Use a Markdown file directly under `docs/screens/` and omit any `..` from the path.
- `properties` lists the properties that need formal proof. For each property, supply a non-empty `name` and `constraint`. Use an empty list when there are no properties to prove.

The graph records the slices and stops the parent run.

The model list is config outside this node. Do not edit it.
