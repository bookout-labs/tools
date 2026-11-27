# Check and approve the plan

Challenge the exact candidate against the originating issue, the pinned target checkout and the
related repository checkouts. Confirm each slice's changes belong to the target repository,
including when a matching path exists in Foreman's repository or a related repository. Check each
slice's acceptance and that its dependencies are complete. Cite each concrete
defect a revised candidate could fix as a finding. A foreign repository is
always a finding.
Ask the human only when the issue's own text cannot be met by any candidate, and cite the
issue line in the question. Then leave findings empty and put candidate defects in notes.
The coordinator or personal agent relays the question.
Settled rulings in the plan's spec history are the human's answers for this issue. Check with
them as context, give a finding the ruling ID that settles it, and ask again only a choice
they don't settle.
Keep all repository and GitHub state read-only.
Approve the checked candidate when no blocking defect or unresolved choice remains. Return empty findings for a clear verdict.
