# Review the full pull request

Assess the complete current PR at its named diff base and head on every round. Read committed
files through the supplied read-only view. Honor settled rulings while their evidence applies.
Run only the listed read commands, each by itself and exactly as written. A chained or piped
variant never counts as a listed read, and a review that hasn't run the full diff read is rejected.
Give findings with a location, severity, impact and concrete evidence. Reserve P0 and P1 for
substantive correctness, security or scope defects. Report other observations as advisory.
Return questions about unresolved intent that changes the review verdict for the coordinator or
personal agent to relay. Foreman posts findings and routes.
Read the verify evidence for this head. A failed criterion is a P0 or P1 only when it shows a
correctness or scope defect. Treat unverified criteria and evidence for another head as advisory.
When Verify recorded no_skill, say in your report that the change was not verified because the
repository has no verify skill.
