# Review quality

Judge the diff that was built.

The change fits the codebase, does what the slice acceptance says, and adds no needless branch or abstraction. It uses the repository's idioms and names. A test that restates the code fails this review.

If the spec names a property, name the evidence for it. A type check, a behavioral test, or a database constraint is evidence. Call it formal verification only when a formal method checked that property.
