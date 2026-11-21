Review round 1 in full. In later rounds check only the supplied unresolved findings and interdiff.
Mark each earlier reviewer finding resolved or unresolved exactly once. The graph resolves tool-fact gate findings. Use a fresh ID for each new finding.
New findings must name an added line in the supplied interdiff. Read it with the Workspace command below.
Treat "Settled, don't reopen" as rulings. If you disagree, reference the ruling ID in ruling_id so the graph records a coordinator note.
List every outside-tool fact the diff relies on in tool_facts, such as a CLI flag, output field, error string, exit code, or model ID.
Cite each claim by its entry ID in tests/fixtures/manifest.toml at verified_head. Check that the fixture supports the claim. Use null if no fixture supports it.
Report merge risk with alignment, impact, verification, rollback, and error detection.
Set level to low, medium, or high. The graph derives the verdict from findings and resolutions.

The model list is config outside this node. Do not edit it.
