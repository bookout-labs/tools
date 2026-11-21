Review round 1 in full. Later rounds check unresolved findings and added interdiff lines also in the PR diff.
Read the reviewed diff with the Workspace command below.
Treat "Settled, don't reopen" as rulings. If you disagree, reference the ruling ID in ruling_id so the graph records a coordinator note.
List every outside-tool fact the diff relies on in tool_facts, such as a CLI flag, output field, error string, exit code, or model ID.
Cite each claim by its entry ID in tests/fixtures/manifest.toml at verified_head. Check that the fixture supports the claim. Use null if no fixture supports it.
Report merge risk with alignment, impact, verification, rollback, and error detection.
Set level to low, medium, or high. The graph derives the verdict from findings and resolutions.

The model list is config outside this node. Do not edit it.
