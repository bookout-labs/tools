Read the request and set `triage` to one of these values.

- implement, when the behavior is decided and the change fits in one pull request of about 400 changed lines or fewer. This applies to parents and slices.
- spec, when the change needs more than one pull request or a product choice is open
- question, when the request asks for information and not a change
- needs_human, when you cannot tell which of those it is

When `triage` is `implement` and the delivery state has no `slice`, return one `slice` for the issue. Include all five keys.

- `title` names the outcome.
- `acceptance` states how to check the outcome, including response checks and other behavior checks.
- `depends_on` is an empty list.
- `screen` gives the browser proof obligation. Use a Markdown file directly under `docs/screens/` with no `..`, or an empty string for work without a screen.
- `properties` lists named PostgreSQL `CHECK`, `UNIQUE`, `FOREIGN KEY`, `EXCLUSION`, or `NOT NULL` constraints. Set `name` to the rule in plain words and `constraint` to the exact PostgreSQL constraint name. Use an empty list when the change adds no database rule.

The model list is config outside this node. Do not edit it.
