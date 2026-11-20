# Code quality

This file is for the scorer. A review does not use these labels.

Grade an implementation transcript on these axes:

- Design. The shape of the change fits the codebase.
- Correctness. The change does what it claims.
- Complexity. The change does not add a needless branch or abstraction.
- Repo conventions. The change follows the idioms already in the repository.
- Code smells. Magic values and dead code are called out.
- Tests. Tests are present and check a behavior. A test that restates the code fails this axis.
- Naming. New names say what they are.

Reply with one label.

- `approve` means the work was judged and passed.
- `block` means the work was judged and failed. Use it when a produced artifact fails a proof or shows that the screen check failed.
- `insufficient_evidence` means the work could not be judged. Use it when a required proof or browser trace was never produced, or when the transcript has too little diff to grade. This label is a non-pass.

Use the scores in `measure.toml`.
