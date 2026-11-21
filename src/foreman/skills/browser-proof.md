# Prove the named screen

For a slice with a non-empty `screen`, read the named `docs/screens/*.md` document.
Drive that screen with Playwright against a copy of production using synthetic records.
Check the slice acceptance, the records, counts, and values the screen promises, and any saves the slice changes.
Keep the Playwright trace from that run.

Return an artifact with `screen` equal to the slice's screen path and `trace` set to the saved trace path.
For example, return `{"screen": "docs/screens/deals.md", "trace": "artifacts/deals.zip"}`.
Missing evidence fails admission. A screenshot or a typecheck does not replace the trace.

Use this evidence in the factory loop. The engine keeps `npm run check` as its one CI check.

Report a missing trace as `insufficient_evidence`. Report a trace for the wrong screen or a failed screen as `block`. When the trace shows failure, include `"passed": false` in the artifact. A trace of the named screen that passes accepts the gate.
