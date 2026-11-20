# Merge risk

Assess the pull request before auto-merge is armed. Use the verification result in the delivery state.

- Alignment. The code matches the request and the spec. Call out drive-by fixes.
- Impact. Name the blast radius and any sensitive path: auth, permissions, payments, personal data, or secrets.
- Verification. Say whether the engine check ran, and whether a user-facing change has end-to-end proof.
- Rollback. Say whether the change reverts in one commit, needs a code change, or can lose data.
- Error detection. Say whether a production failure would be logged or reported.

Set level to low, medium, or high. A higher risk should be reduced before the pull request opens, when that is possible.
