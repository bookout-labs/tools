# Slice 129b live receipts

Run on 2026-11-21 against `bookout-labs/foreman-smoke` only, using App 5861795,
installation 184333419 and the unchanged 129a credential module.
`FOREMAN_APP_ID` and `FOREMAN_INSTALLATION_ID` selected App authentication.
The credential module alone read the owner-only key at its default path.

## D6 identity

`gh api users/bookout-labs-foreman%5Bbot%5D` returned user ID 359513816.
Both author and committer on Foreman commit
[`6cb33a2`](https://github.com/bookout-labs/foreman-smoke/commit/6cb33a2fc5a9dc541660af55a04e93af157a77a3)
are `bookout-labs-foreman[bot]`, with email
`359513816+bookout-labs-foreman[bot]@users.noreply.github.com`.
The message retains `Co-Authored-By: gpt-6.1-sol-high <noreply@openai.com>`.
The real-Git test also verifies a re-committed worker tree and unchanged local identity configuration.

## D5 permission matrix

Every row used the real installation token through LiveForge or LiveWorkspace.
No token was emitted, written to disk or passed in argv.

| Forge operation | Live evidence |
| --- | --- |
| Default branch, issue load, labels, open and marked issue lists | Synthetic issues [17](https://github.com/bookout-labs/foreman-smoke/issues/17) and [18](https://github.com/bookout-labs/foreman-smoke/issues/18), label `slice` and marker `129b-matrix` |
| Issue creation, body update, sub-issue and parent reads and writes, blocker reads and writes | Issue 18 is a child of 17 and blocked by 17 |
| Issue comment and comment listing | Issue 17's bot permission-proof comment |
| Issue closure and linked merged PR lookup | `is_merged(17)` after PR 19 merged |
| PR create, base lookup, review target and PR snapshot | Bot PRs [19](https://github.com/bookout-labs/foreman-smoke/pull/19) and [20](https://github.com/bookout-labs/foreman-smoke/pull/20) target `main` |
| Review listing and COMMENT submission | [PR 19 review](https://github.com/bookout-labs/foreman-smoke/pull/19#pullrequestreview-6005403729), [PR 20 review](https://github.com/bookout-labs/foreman-smoke/pull/20#pullrequestreview-6005407421) |
| Comment write | [PR 19 comment](https://github.com/bookout-labs/foreman-smoke/pull/19#issuecomment-7063177536) |
| Hold lookup | `gh variable list --json name,value` succeeded; hold absent. A simulated installation-token HTTP 403 raises `MergeHoldUnreadable` |
| Effective branch rules, classic branch policy, commit parents, checks and statuses | `readiness` on PR 19 returned `ready`, with successful `test` run 136858186034 from Actions app 15368; ruleset 27781059 read separately |
| Fetch, remote head lookup and push | Both PR heads published by App-authenticated LiveWorkspace over HTTPS |
| Merge arming, queue snapshot and reconciliation | Queue receipts below; both synthetic PRs merged |

## D8 bot smoke

`foreman run delivery --repo bookout-labs/foreman-smoke --issue 16
--check 'python3 -m unittest discover -s tests -t .'` completed `done verified`.
It used Claude Opus 5.5 for triage and review and GPT-6.1 Sol for implementation.
PR 20's author, COMMENT reviewer and queue-admission actor are `bookout-labs-foreman[bot]`.
Its checkpoint recorded queue ID `MQE_lQDOV2N3Ts8AAAABQ4vYVs4ABaqZzgQzrlI`, status
`QUEUED`, source head `6cb33a2fc5a9dc541660af55a04e93af157a77a3` and policy `SQUASH`.

PR 19 exercised `gh pr merge --auto --squash --match-head-commit` twice.
At 13:18:31 UTC checks were missing and the bot enabled auto-merge.
At 13:18:33 the bot disabled it, so the green path could be exercised separately.
At 13:18:54 `readiness` was green and the same command admitted the expected head to
queue ID `MQE_lQDOV2N3Ts8AAAABQ4vO0M4ABaqZzgQzrgY` with no autoMergeRequest.
The confirmation accepted that queue membership.
GitHub reported autoMergeRequest method `MERGE` on the pending path even with
`--squash`; the effective queue policy and final queue merge used `SQUASH`.
Both arming events and queue admission are attributed to the bot in PR 19's timeline.

The merge-group `test` jobs succeeded for
[PR 19](https://github.com/bookout-labs/foreman-smoke/actions/runs/44908255973)
and [PR 20](https://github.com/bookout-labs/foreman-smoke/actions/runs/44908282712).
Both PRs were intended to merge and did merge. Nothing remains armed by this proof.
No rulesets were changed and no admin bypass was used.
