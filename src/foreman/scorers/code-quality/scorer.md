---
name: code-quality
description: Evaluates maintainability, error handling, minimal helper reuse, and test hygiene of the delivered change.
stages:
  - implement
  - review
labels:
  - value: pass
    description: Code matches repo idioms, avoids duplication, handles errors explicitly, and tests behavior rather than restating code.
    score: 1.0
  - value: minor_issues
    description: Functional implementation with small maintainability or test-precision rough edges.
    score: 0.5
  - value: fail
    description: Introduces silent failure swallowing, duplication, fragile hacks, or tautological tests.
    score: 0.0
  - value: insufficient_evidence
    description: Transcripts or diff evidence were missing or too truncated to judge code quality.
    score: 0.5
passingScore: 0.8
samplingRate: 1.0
model: claude-sonnet-5-5-high
selfImprovement: true
---

Evaluate code quality strictly against observed diffs, test outputs, and review notes.

Check:
1. Small single-purpose functions and explicit error handling instead of silent catches.
2. Tests validate behavior that would fail without the fix rather than mirroring implementation constants.
3. No secrets, personal data, or leftover debug scaffolding in code or fixtures.
