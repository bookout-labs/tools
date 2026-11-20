# Code quality

Grade an implementation transcript on these axes:

- Design. The shape of the change fits the codebase.
- Correctness. The change does what it claims.
- Complexity. The change does not add a needless branch or abstraction.
- Repo conventions. The change follows the idioms already in the repository.
- Code smells. Magic values and dead code are called out.
- Tests. Tests are present and check a behavior. A test that restates the code fails this axis.
- Naming. New names say what they are.

Reply approve, block, or insufficient_evidence.
insufficient_evidence means the transcript has no diff, or too little diff to grade.
The scores for those labels live in measure.toml. Do not invent new scores.
