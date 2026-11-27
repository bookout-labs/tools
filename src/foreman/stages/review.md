# Review the full pull request

Answer the one fixed question Foreman names for this pass about the complete current PR at its
diff base and head. Read the change through the granted diff command, run by itself and exactly
as written. Read committed files through the supplied read-only view. Edit nothing.
Pass (a): against the issue text, not the plan, does the change deliver the issue, and only the
issue? Pass (b): what does it break beyond the diff, in callers, data and security? Pass (c): is
it a fix or a patch? Does it remove the cause, or add a guard, retry, wrapper or flag the plan did
not name, duplicate existing code or ignore the repository's pattern?
Honor settled rulings while their evidence applies.
Give findings with a location, severity, impact and concrete evidence. Reserve P0 and P1 for
substantive correctness, security or scope defects. Report other observations as advisory.
Return questions about unresolved intent that changes the review verdict for the coordinator or
personal agent to relay. Foreman posts the three passes as one report and routes.
Read the verify evidence for this head. A failed criterion is a P0 or P1 only when it shows a
correctness or scope defect. Treat unverified criteria and evidence for another head as advisory.
When Verify recorded no_skill, say in your report that the change was not verified because the
repository has no verify skill.
