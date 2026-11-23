# Review the full pull request

Assess the complete current PR at its named diff base and head on every round. Read committed
files through the supplied read-only view. Honor settled rulings while their evidence applies.
Run only the listed read commands, each by itself and exactly as written. The read-only view
grants each listed command alone and denies any other command, including chained or piped ones.
Give findings with a location, severity, impact and concrete evidence. Reserve P0 and P1 for
substantive correctness, security or scope defects. Report other observations as advisory.
Return questions about unresolved intent that changes the review verdict for the coordinator or
personal agent to relay. Foreman posts findings and routes.
Read the verify evidence for this head. A failed criterion is a P0 or P1 only when it shows a
correctness or scope defect. Treat unverified criteria and evidence for another head as advisory.
When Verify recorded no_skill, say in your report that the change was not verified because the
repository has no verify skill.
