---
name: efficiency
description: Detects command thrashing, repeated check retries without hypothesis changes, and wasteful token burn.
stages:
  - implement
  - verify
labels:
  - value: efficient
    description: Direct investigation and targeted verification with minimal wasted turns or retries.
    score: 1.0
  - value: wasteful
    description: Excessive broad searches, redundant test reruns, or repeated edits without new diagnostic signal.
    score: 0.5
  - value: thrashing
    description: Repeated identical failing commands, verify/review loops without fixing root cause, or high token burn for trivial scope.
    score: 0.0
  - value: insufficient_evidence
    description: Step timings or transcripts are too sparse to judge run efficiency.
    score: 0.5
passingScore: 0.8
samplingRate: 1.0
model: grok-4.7-high
selfImprovement: true
---

Evaluate whether the delivery run moved purposefully from reading to editing and verification.

Flag:
1. Repeating failing verification runs without addressing the captured error output.
2. Reading or searching the same files redundantly across turns.
3. Heavy token consumption relative to a small or single-file change.
