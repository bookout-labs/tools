# Answer three fixed questions about the plan

Read the exact candidate, the originating issue and, as needed, the pinned target and related
repository checkouts. Keep repository and GitHub state read-only.

1. Does the plan cover what the issue asks? Check that the slices belong in the target repository,
   that any removed or renamed test is justified by the issue, and that any protected change has its own slice.
2. Does any slice dictate code in place of behavior? Flag a prescribed edit, expression or function to write.
3. Can each criterion be proven the way the plan says? Check the proof against the pinned files and
   any assumptions it relies on, including the reason given for a criterion marked not drivable.

Report concrete defects in findings. Set cites to a verbatim excerpt of the issue text or to an object
with the repository and the root-relative path of a tracked file at its pin. Explain how the cited
source supports the finding. Set cites to null when there is no such source. Foreman sends the plan
back to draft only for a finding with a verified citation. It keeps the other findings as advisory notes.

Ask the human only when the issue's own text cannot be met by any candidate, citing the issue line.
Return the question on its own. Use settled rulings from the spec history, including applicable rulings inherited from the parent, as context. Give a finding
the ruling ID that settles it, and ask again only about a choice those rulings leave open.
