Review round 1 in full. Later rounds check unresolved findings and added interdiff lines also in the PR diff.
Read the labeled PR diff and, in later rounds, the history-only interdiff with the Workspace commands below.
Treat "Settled, don't reopen" as rulings. If you disagree, reference the ruling ID in ruling_id so the graph records a coordinator note.
List every outside-tool fact the diff relies on in tool_facts, such as a CLI flag, output field, error string, exit code, or model ID.
When a fact record is declared, cite each claim by its entry ID at verified_head and check that the fixture supports it. Otherwise use null fixture references. Use null if no fixture supports a claim.
Report alignment, impact, verification, rollback, and error_detection as dimensions with applies, assessment and nonempty evidence.
Every dimension must cite at least one changed path. For applies=false explain why it is inapplicable and still ground it in evidence.
Each citation has side (base or head), repository-relative path and line (positive integer or null for the whole path).
Paths and text lines must exist in that committed tree. Use base for deletions or rename sources in the full PR diff from the merge-base, never for deletions only in the interdiff from previous. Binary and mode-only changes permit whole-path citations; binaries have no text lines.
A previous-only path is never risk evidence, even with line=null. Resolve its earlier finding by ID and ground dimensions in the remaining net PR changes. Previous is for navigation only and is not a citation side.
Unchanged dependency evidence is allowed alongside changed-path evidence. Findings may cite any existing head text line, regardless of inline posting eligibility.
These rules apply in every round even with no findings or new added lines. The schema checks structural grounding, not prose truth or completeness.
Set level to low, medium, or high. The graph derives the verdict from findings and resolutions.

The model list is config outside this node. Do not edit it.
