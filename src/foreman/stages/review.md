Review the implementation. Reply approve or changes.
When delivery state has a verified_head, review that commit.
List every outside-tool fact the diff relies on in tool_facts, such as a CLI flag, output field, error string, exit code, or model ID.
Cite each claim by its entry ID in tests/fixtures/manifest.toml at verified_head. Check that the fixture supports the claim. Use null if no fixture supports it.
Report merge risk with alignment, impact, verification, rollback, and error detection.
Set level to low, medium, or high.

The model list is config outside this node. Do not edit it.
