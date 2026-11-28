# Blueprint decisions

## Agreed direction

Agreed by the owner 2026-11-22. “i agree with all of your take aways from this book” and “if I didn't comment on any of those takeaways, then I'm good with them”.

- Roll out each version and its merge authority through synthetic replays, smoke repositories, about three real issues, then normal volume.
- Start a run when the Ready label is added to an issue. A webhook delivers the trigger and the issue's run label and status comment mirror the run's state (D6).
- Use GitHub, the CARR record layer (the team's record and doctrine store) and T3 Code (the app that hosts the coordinator or personal agent) as the shared context system. Git versions the blueprint.
- Keep worker configuration separate from stage procedures. Agreed by the owner 2026-11-22.
- Add scorers, benchmarks and fix PRs driven by observed failures. Agreed by the owner 2026-11-22.
- Complete delivery on an observed merge. Agreed by the owner 2026-11-22.
- Keep typed outputs, budgets and exact-head checks. Agreed by the owner 2026-11-22.
- Redesign Foreman from the owner's 2026-11-24 brief, items 1 to 24, as one drawing approved once before any build (tools #299). The merge waits on checks the builder cannot influence, and plain code is preferred over a model's opinion. Every item replaces or removes something. The redesign folds in tools #160 to #165, #248, #274 and #292.

[Notion’s book (archived)](https://web.archive.org/web/20261121015523/https://www.notion.com/a-new-shape-for-work)

## D1. What sends a pull request back?

Agreed by the owner 2026-11-22. Only a concrete P0 or P1 correctness, security or scope defect in the full current pull request sends it back. All other findings are comments. Revised by the 2026-11-24 brief (#299, item 17): review asks three fixed questions, each in a fresh pass. A blocking finding about issue fit or breakage carries a failing demonstration that Foreman runs at the head. If it does not fail, the finding is advisory. A blocking fix-or-patch finding cites the file:line of the code it duplicates or works around. Another model family is a preference. If that family is unavailable, dispatch uses the next qualified model in models.toml order, including the builder's family. Only the shared quota wait applies when no qualified model is available. Warp workers advise and can suggest revisions. [Warp code review](https://docs.warp.dev/factories/use-cases/code-review/)

## D2. Who queues the pull request?

Agreed by the owner 2026-11-22. “I'm good with the trust ladder”. Foreman queues the exact reviewed revision after required checks pass, under the authority earned through synthetic replays, live smoke, a limited batch of about three real issues and normal volume. GitHub enforces approvals and queue rules. Revised by the 2026-11-24 brief (#299, item 19): merge_held is the default until a synthetic branch probe proves the App and outside writers cannot move the branch during an open merge request. The probe is part of doctor's full check, which admission runs whenever the contract file's digest has no green record, and merge needs a merge queue ruleset. Release dependents through admission only after the production check confirms the merge (D12). A person decides when to merge in Warp. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D3. Who approves a plan?

Agreed by the owner 2026-11-22. Revised by the 2026-11-24 brief (#299, item 8). The checking model can approve the plan once Foreman validates the repository, the file list and the assumption citations. The plan proceeds when the independent check clears it, without the human's review. The human may comment through their coordinator or personal agent. A `foreman stop` comment posted on the issue under the human's identity ends the run cancelled at the next stage entry. The digest approval is deleted, because nobody was shown the digest and no step waited on it. Ask the human through the agent when an unresolved choice needs their judgment. Warp's default planning flow asks for human approval. [Warp gates](https://docs.warp.dev/factories/blueprints/gates/)

## D4. Keep the 400-line cap?

Agreed by the owner 2026-11-22. Remove the 400-line gate and its override. Report the changed-line count for the checked tree. Split work at real dependencies while planning. Warp's default flow encourages scoped work without a universal threshold. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D5. Which conversation continues?

Agreed by the owner 2026-11-22. Revised by the 2026-11-24 brief (#299, item 2). The builder keeps one conversation for the life of the PR. Dispatch saves its session on every accepted answer, not only on quota. A failed check, a failed criterion, a review finding or a merge repair resumes it when the checkout fingerprint matches the saved one. Otherwise the builder starts fresh. Each review pass and each Verify drive uses a fresh session. Warp continues a worker's conversation during revisions. [Warp workers](https://docs.warp.dev/factories/factory-agents/)

## D6. How does a run start and show its state?

Agreed by the owner 2026-11-22: A. A run starts when the Ready label is added to an issue in any repository where the App is installed, on an organization or a personal account. The issue shows the run's state as one run label (Ready, Doing, Needs answer or Done) plus a status comment with the typed result and its links, so a halt stays distinct from a merge. Project boards are optional views. An organization board can collect labeled issues with GitHub's built-in auto-add workflow. GitHub sends `projects_v2_item` events only for organization-owned Projects, so a board column cannot be the trigger for personal repositories. Warp starts runs from filtered GitHub events, including issue labels. [Warp triggers](https://docs.warp.dev/factories/automation-triggers/) · [GitHub issues webhook](https://docs.github.com/en/webhooks/webhook-events-and-payloads#issues) · [GitHub Projects webhook](https://docs.github.com/en/webhooks/webhook-events-and-payloads#projects_v2_item)

## D7. Which worker methods belong in each step?

Agreed by the owner 2026-11-22: as recommended. Warp assigns reusable skills to workers. Keep the pinned how and why skills for research, architect for planning, statechart for stateful work, interrogate's evidence probe for the independent plan review, practical TDD for bug fixes, benchmark-checklist for performance work and review-quality's evidence rubric. Revised by the 2026-11-24 brief (#299, item 5): every skill loads whole at its pinned commit, as Warp loads a SKILL.md. No skill is cut to a line range. Add five methods:

- Research on bugs and regressions builds a feedback loop first with Matt Pocock's `diagnosing-bugs`.
- Spec applies the vertical-slice and blocking-edge rules from Matt Pocock's `to-tickets` when a parent splits into children.
- Verify drives the change with the target repository's own `verify-<app>` skill (D11). pstack `create-verification-skill` creates it and `maintain-verification-skill` keeps it current.
- Implement runs the target repository's own formal verification skill, `formal-<area>`, on changes that touch the verified code. pstack `create-formal-verification-skill` creates it and `maintain-formal-verification-skill` updates it when a change is intended. Formal verification is opt-in per repository: it runs only where the repository has its own skill. Agreed by the owner 2026-11-23 ("A").
- Review checks what the change could break beyond the diff with pstack `blast-radius`.
- Fix PRs driven by observed failures, outside the stages, use pstack `correct`.

Leave out `to-spec` and Matt Pocock's `triage` and `code-review`. Measure the selection using the synthetic benchmarks. [Warp skills](https://docs.warp.dev/factories/factory-skills/)

## D8. Where should checks and evidence live?

Agreed by the owner 2026-11-22. Delete the proof step and universal tool-fact veto. Run Foreman's exact-tree check inside the editing loop, keep evidence the issue asks for, and let GitHub run its required checks. Revised by the 2026-11-24 brief (#299, items 9, 10 and 15): before the build, a separate worker in the verifier role writes acceptance tests that plain code proves fail at the base. The builder cannot edit them or the protected files. After a passing check, plain code mutates a capped sample of changed lines and sends any mutation no test notices back to the builder. Warp's builder runs repository checks. [Warp issue-to-PR guide](https://docs.warp.dev/guides/agent-workflows/running-an-issue-to-pr-blueprint/)

## D9. Which paths can a slice name?

Agreed by the owner 2026-11-22: (a). Revised by the 2026-11-24 brief (#299, items 6 and 7). A slice states observable behavior, non-goals and how each criterion is proven, and never names a code edit. Builds and uses seams are removed. A slice keeps depends_on and an advisory list of files it expects to touch. Each listed existing path is a Git-tracked regular file in the pinned target checkout. Reject directories and symlinks. Each fact the plan relies on outside the target checkout cites a tracked file in a related repository at its pinned commit or becomes a gate question. Warp's spec states desired behavior, non-goals and validation criteria. Foreman validates exact paths and citations to bind the plan to pinned checkouts. [Warp gates](https://docs.warp.dev/factories/blueprints/gates/)

## D10. What happens after an interruption?

Agreed by the owner 2026-11-22: (a) with (ii). Resume at the interrupted step and reconcile before repeating a model call or remote write. If a model call's usage is ambiguous, charge the worst case to the budget, log usage unknown, and continue within the remaining allowance. Foreman reconciles calls and writes at the saved step because its local CLIs and GitHub effects can outlive the process. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D11. Where does end-to-end verification run?

Agreed by the owner 2026-11-23: "2. Warp does it that way for a reason. It's the better design choice", then "1-A, 2-yes, 3-yes". Warp runs user-level verification as its own stage with its own worker. The worker builds and starts the app from the pull request, uses it as a user would, and reports each criterion as passed, failed or unverified with evidence. It never edits, approves or merges, and the evidence complements code review rather than replacing it.

- A Verify stage runs between Implement and Review. It drives the published head with the target repository's own `verify-<app>` skill, found at the contract file's verify skill path. Doctor's readiness check requires that skill, so the `no_skill` route is removed.
- Foreman posts one evidence comment for the tested head. If the head moved, Foreman posts the evidence marked stale and returns to Implement, which reconciles the moved head as it does after Review. Warp's publication step ends instead. Foreman diverges because nothing may verify or review a head until Foreman has reconciled it.
- A known checked head that Merge returns for fresh review keeps the evidence already posted for it. Its code is unchanged, so Verify doesn't run again.
- The verifier prefers a qualified model from a different family from the builders, as the reviewer does.
- Revised by the owner 2026-11-24, verify A (decision 67c6553f): a failed criterion goes back to Implement and counts toward the failed-check cap. An unverified criterion blocks the merge unless the plan marked it not drivable with a reason. This reverses the earlier line "a failed criterion never routes straight to Implement".
- `verify-<app>` moves from Implement to the Verify worker, so the builder runs only the repository checks.
- The worker that reviews is called `reviewer`, and the new worker is called `verifier`, matching Warp's terms.

Warp's recipe starts on a maintainer's label. Foreman runs unattended, so Verify runs on every published head and gates Review. [Warp Computer Use verification](https://docs.warp.dev/factories/use-cases/computer-use-verification/)

## D12. What happens when a merge fails in production?

Agreed by the owner 2026-11-24, rollback A (decision 74a43493). After Foreman's merge, Foreman runs the target contract's read-only production check against the merge commit. If it fails, Foreman files a revert issue carrying Ready whose slice reverts the merge commit, so the revert runs through the normal path, and files a regression issue with the evidence. The run ends `reverted` and releases no dependents. This supersedes only the "a person decides on rollback" part of the 2026-11-23 ruling (tools #26). Warp's incident recipe investigates production read-only and leaves rollback to people. Foreman reverts its own merge because no person stands between review and production. [Warp production incidents](https://docs.warp.dev/factories/use-cases/production-incident-resolution/)

## D13. Can Foreman merge when the reviewer shares the builder's family?

Agreed by the owner 2026-11-24, merge B (decision 57aa3a15). Foreman merges even when the reviewer is the builder's model family. No change may add a human approval or merge step. The merge instead waits on checks the builder cannot influence: acceptance tests written before the build, the mutation check, Verify's verdict and review demonstrations that plain code runs. Warp's reviewer runs on a different model, and a human merges. [Warp code review](https://docs.warp.dev/factories/use-cases/code-review/)

## D14. What must a target repository supply?

From the owner's 2026-11-24 update to the brief (#299, items 22 to 24). Each target repository carries one contract file, `.foreman/contract.yaml`, holding the check command and its JUnit XML test report, container image and setup commands, how to start the app and tell it is up, the related repositories, each pinned per run at admission, the names of the secrets the app needs, the verify skill path, the mutation command and the production check. Repository variables keep only the human login, the budget caps and the merge hold. `foreman doctor --repo OWNER/NAME` checks readiness in plain code. Its quick part runs at admission before any paid call, and a repository that fails it starts no run. `docs/target-repo-setup.md` is generated from doctor's list. Two choices are the auditor's, not audit findings, and stay open to pushback: moving the check command from a repository variable into the contract file, and not building an onboarding command until a third repository needs one. Warp keeps the image, setup commands, repositories and secrets in the factory definition. [Warp runners](https://docs.warp.dev/factories/runners/)

## D15. What did the owner rule on the v3 review's questions?

Agreed by the owner 2026-11-27: "I'll go with all your recommendations on those 18 questions". The whole-design review of the v3 build ended with 18 questions for the owner, each with options and a recommendation. Each ruling below is that recommendation. Question 2, the 15 redraws made after v3 was approved, waits on the owner's review of a table, and none of them is drawn here.

- Question 4, what makes production confirm wait. The repository's production check command waits for the deploy itself, within its time limit, and Foreman runs it once. This is what was built. A GitHub deployment event that wakes confirm may come later, if app and engine report deployments to GitHub.
- Question 6, routing defects. The gate and the record for a valid result with no route are deleted from the drawing. CI already checks that every result has exactly one route, and nothing built them.
- Question 10, two fields with no drawn meaning. The drawing now gives the values the code uses: work_kind is feature, bug_fix, refactoring or perf_issue, and spec_gate is ready or needs_human. No code changed.
- Question 12, a revert's test deletions. A revert that changes only files its merge commit changed may lose any base test. Foreman does not run the check at the merge commit's parent to list the exact tests that commit added. Reverts are rare and review reads the diff. This is what was built.
- Question 13, whether the issue closes after merge. The PR closes its issue with a closing keyword, so the merge closes the issue and the closed-issue recheck reaches a dependent whose prerequisite no Foreman run owns. The code already did this and did not change.
- Question 14, children and Ready. file_children adds Ready to every child except a CI workflow slice and hands each one straight to admission, because intake ignores Foreman's own label updates. This is what was built.
- Question 15, how a question reaches the human and comes back. The step pauses inside itself, so needs_human has no edge. The status comment shows the question with its reply marker, the line foreman answer and the question's ID, and an issue comment under the human's login that starts with that line wakes the run with the answer. This is what was built.
- Question 16, the parent issue after its children are filed. It gets Done and stays open, and nothing updates it when the children merge. GitHub's sub-issue progress bar already shows that. This is what was built.
