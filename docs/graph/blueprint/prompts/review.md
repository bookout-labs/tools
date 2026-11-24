# Review the full pull request

Answer the one fixed question Foreman names for this pass about the complete current PR at its
diff base and head. Read the change through the granted diff command. You may read the related
repository checkouts and run the contract's check command in the attempt container. Edit nothing.
Pass (a): against the issue text, not the plan, does the change deliver the issue, and only the
issue? Pass (b): what does it break beyond the diff, in callers, data and security? Pass (c): is
it a fix or a patch? Does it remove the cause, or add a guard, retry, wrapper or flag the plan did
not name, duplicate existing code or ignore the repository's pattern?
Give each finding a location, severity, impact and evidence. Reserve P0 and P1 for concrete
correctness, security or scope defects, and for a pass (c) patch that cites the code it duplicates
or works around. A blocking finding from pass (a) or (b) carries a
demonstration that fails at the head: a command, or a test file's path and content that Foreman
writes and runs. A blocking finding from pass (c) cites the file:line of the existing code it
duplicates or works around. A new test that mocks a boundary named in the plan's assumptions is a
blocking pass (b) finding that cites the mock's file:line and the assumption. Report other
observations as advisory. Honor settled rulings while their evidence applies. Verify evidence is
context. In pass (b), rule on each mutation the builder named as equivalent. If the mutation can
change behavior, report a blocking finding that names it and the input it changes. Foreman's
mutation run is its demonstration. Return a question about unresolved
intent that changes the verdict for the coordinator or personal agent to relay. Foreman runs the
demonstrations, posts the report and routes.
