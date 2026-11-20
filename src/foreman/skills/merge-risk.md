# Merge risk

Assess the pull request before auto-merge is armed, and mitigate avoidable risk during implementation before opening the PR. Use the verification result in the delivery state.

- Alignment (1x weight). The code matches the request and the spec. Call out drive-by fixes.
- Impact (2x weight). Name the blast radius and any sensitive path: auth, permissions, payments, personal data, schema migrations, or secrets.
- Verification (2x weight). Say whether the engine check ran, and whether a user-facing change has end-to-end proof.
- Rollback (1.5x weight). Say whether the change reverts in one commit, needs a code change, or can lose data.
- Error detection (1x weight). Say whether a production failure would be logged or reported rather than swallowed silently.

Score internally using 1 (good), 2 (decent), 3 (poor) weighted as above (`score = sum(w * v) / 7.5`):
- `low`: weighted score < 1.5
- `medium`: weighted score 1.5 to < 2.25
- `high`: weighted score >= 2.25

Set level to low, medium, or high. Reduce high or medium risk (e.g. add missing error logging or tests) before finishing implementation when possible.
