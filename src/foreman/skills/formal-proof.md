# Prove the named constraints

For every entry in the slice's `properties`, identify its `name` and named PostgreSQL `constraint`.
Require that constraint in the database schema.
Run a test that attempts a violating write and asserts that PostgreSQL rejects it because of that named constraint.
Use an isolated database with synthetic records.

Return an artifact with a `constraints` list of results containing `constraint` and the boolean `rejected`.
For example, return `{"constraints": [{"constraint": "deals_amount_positive", "rejected": true}]}`.
Set `rejected` to true only when the rejection test passes for that constraint.
Every property needs its named constraint with `rejected` true. Missing evidence fails admission.
A TypeScript check or application validation alone does not prove an always-true database rule.

Use this evidence in the factory loop. The engine keeps `npm run check` as its one CI check.
Keep numeric scores in `measure.toml`. `approve` passes, `block` fails, and `insufficient_evidence` is reported as such.
Sampling does not decide merge.

Report missing evidence or a checker that did not run as `insufficient_evidence`; use `"ran": false` when the checker did not run. Report an accepted violating write with `"rejected": false` as `block`. A typecheck offered instead of the rejection test is also `block`. Rejection evidence for every named constraint accepts the gate without assigning a score.
