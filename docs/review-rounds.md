# Review rounds

Round 1 reviews the full PR diff. Later rounds check unresolved findings and added
interdiff lines also present in the PR diff. Out-of-scope findings become notes. The graph stores reviewer findings and explicit
resolutions at a pinned head. Missing, duplicate or unknown resolutions and reused
finding IDs halt as invalid output. Resolved findings become settled rulings.
Disagreement with a known ruling becomes a coordinator note.

Graph checks are separate from reviewer findings. The tool-fact check runs at every
verified head, carrying earlier claims until a later citation replaces them. A failed
check remains blocking until it passes at a head where it has never failed.

ReviewOutput remains the worker contract. Pure acceptance returns AcceptedReview,
which owns the derived verdict, findings, notes and next history. Reviewer prompts
omit checkpoint history, supplied explicitly as unresolved findings and settled rulings.
Later rounds omit implementation, findings and detail to keep the prompt scoped.
Round 1 retains those fields. Rulings exist only within a run.

A repeated blocking kind among accepted reviewer findings pauses as design_rethink
without consuming a failure round. Recurrence compares against every earlier round,
including resolved findings. Graph checks never enter this comparison. A human answer
resumes implementation and becomes a design:N ruling in the next review prompt.
Without a checkpointer the run stops for the design fix.

Workspace parses the PR diff and interdiff once. Round 1 falls back to body comments
when its diff is malformed. Later parse or fetch failures halt as review_unavailable.
Inline comments always use the PR diff, including after main merges into the branch.
