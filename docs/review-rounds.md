# Review rounds

Each round reviews the full PR diff. The graph stores reviewer findings and explicit
resolutions at a pinned head. Missing, duplicate or unknown resolutions and reused
finding IDs halt as invalid output. Resolved findings become settled rulings.
Disagreement with a known ruling becomes a coordinator note.

Graph checks are separate from reviewer findings. The tool-fact check runs at every
verified head, carrying earlier claims until a later citation replaces them. A failed
check remains blocking until it passes at a head where it has never failed.

ReviewOutput remains the worker contract. Pure acceptance returns AcceptedReview,
which owns the derived verdict, findings, notes and next history. Reviewer prompts
omit checkpoint history, supplied explicitly as unresolved findings and settled rulings.
Other worker state remains available. Rulings exist only within a run.
