# Check and approve the plan

Challenge the exact candidate against the originating issue, the pinned target checkout and the
related repository checkouts. Confirm each slice's changes belong to the target repository,
including when a matching path exists in Foreman's repository or a related repository. Open each
cited assumption and confirm the cited file says what the plan relies on. A fact outside the target
checkout that the plan relies on without listing it as an assumption is a finding. A criterion
marked not drivable without a sound reason is a finding. Check that each slice
states observable behavior with non-goals and a proof for every criterion, and that its
dependencies are complete. Rule on each test a slice lists as removed or renamed. A reason the
issue does not support is a finding. A slice that names a code edit, an expression or a function to write is
a finding. A change to a protected file outside its own slice is a finding. Cite each concrete
defect a revised candidate could fix as a finding. A foreign repository or a wrong assumption is
always a finding.
Ask the human only when the issue's own text cannot be met by any candidate, and cite the issue
line in the question. Then leave findings empty and put candidate defects in notes. The
coordinator or personal agent relays the question.
Settled rulings in the plan's spec history are the human's answers for this issue, including applicable rulings inherited from its parent. Check with
them as context, give a finding the ruling ID that settles it, and ask again only a choice they
don't settle.
Keep all repository and GitHub state read-only.
Approve the checked candidate when no blocking defect or unresolved choice remains. Return empty
findings for a clear verdict.
