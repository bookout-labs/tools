Foreman halted with merged_unreviewed. GitHub merged the pull request at a head Foreman did not review.

GitHub merged PR #12 at cccccccccccccccccccccccccccccccccccccccc, not the reviewed head aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa. No delivery credit or dependent release.

Watch: bookout-labs/tools#101

Run report

Repository: Example/Engine
Issue: #8
Blueprint version: 3
Stage: merge
Step: report
Steps taken: triage.admit triage.route spec.validate spec.check implement.prepare implement.edit implement.commit implement.publish verify.drive verify.post review.inspect review.post review.route merge.arm merge.observe merge.report
Skill pins: a local repository@<pin> pstack/skills; a local repository@<pin> skills
Plan revisions: 0
Failed checks: 0
Blocking rounds: 0
Tokens: 73458 of 20000000
Dollars: $0.113660099999999985 of $3

| Stage | Step | Model | Skills | Outcome | Tokens | Dollars |
| --- | --- | --- | --- | --- | --- | --- |
| triage | route | claude-opus-5-5-high | none | ran | 5814 | $0.037886699999999995 |
| spec | check | grok-4.7-high | pstack/skills/interrogate/SKILL.md@<pin12> (a local repository) | cli_missing | 0 | $0 |
| spec | check | gpt-6.1-sol-high | pstack/skills/interrogate/SKILL.md@<pin12> (a local repository) | ran | 28008 | unknown |
| implement | edit | gpt-6.1-sol-high | none | ran | 28008 | unknown |
| verify | drive | claude-opus-5-5-high | .claude/skills/verify-web/SKILL.md (Example/Engine) | ran | 5814 | $0.037886699999999995 |
| review | inspect | claude-opus-5-5-high | src/foreman/skills/review-quality.md (Foreman 0.3.0), pstack/skills/blast-radius/SKILL.md@<pin12> (a local repository) | ran | 5814 | $0.037886699999999995 |

<!-- foreman status -->