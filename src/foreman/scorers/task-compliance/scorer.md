---
name: task-compliance
description: Evaluates whether the delivery run solved the exact requested task without drive-by edits or missed acceptance criteria.
stages:
  - triage
  - spec
  - implement
  - verify
labels:
  - value: pass
    description: All acceptance criteria and stated constraints were addressed accurately within scope.
    score: 1.0
  - value: partial
    description: Core intent was implemented, but a minor requirement or edge case was missed or extra drive-by edits crept in.
    score: 0.5
  - value: fail
    description: Acceptance criteria were missed, misrepresented, or broken in the final state.
    score: 0.0
  - value: insufficient_evidence
    description: Transcripts or diff evidence were missing or too truncated to judge task compliance.
    score: 0.5
passingScore: 1.0
samplingRate: 1.0
model: claude-sonnet-5-5-high
selfImprovement: true
---

Judge whether the delivery agent fulfilled the actual issue request and acceptance criteria cleanly.

Check:
1. Did implementation stay scoped to the slice acceptance and avoid unrelated refactors?
2. Were required checks and proof obligations executed and accurately reported (never fabricated)?
3. Were halt or blocker states reported honestly instead of bypassed?

Use `insufficient_evidence` only when run evidence is genuinely missing or truncated.
