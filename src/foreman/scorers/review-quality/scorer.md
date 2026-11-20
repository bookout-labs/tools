---
name: review-quality
description: Audits whether the PR reviewer verified acceptance, proof evidence, and merge risk instead of rubber-stamping.
stages:
  - review
labels:
  - value: thorough
    description: Reviewer checked acceptance against actual diff behavior, verified proof claims, and scored merge risk accurately.
    score: 1.0
  - value: superficial
    description: Reviewer checked surface style or restated summary without checking edge cases or risk mitigation.
    score: 0.5
  - value: rubber_stamp
    description: Reviewer approved despite clear failing acceptance, missing proof, or unaddressed high merge risk.
    score: 0.0
  - value: insufficient_evidence
    description: Review output or implementation diff is missing so review quality cannot be assessed.
    score: 0.5
passingScore: 0.8
samplingRate: 0.5
model: grok-4.7-high
selfImprovement: true
---

Judge the review stage independently from the builder's summary.

Check:
1. Did the review cite concrete evidence for named properties or risk dimensions?
2. Were inline comments grounded in actual changed lines and actionable?
3. Did `changes` verdicts give specific, testable fix instructions instead of vague critiques?
