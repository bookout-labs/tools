# Blueprint decisions

## Agreed direction

Agreed by the owner 2026-11-22. “i agree with all of your take aways from this book” and “if I didn't comment on any of those takeaways, then I'm good with them”.

- Roll out each version and its merge authority through synthetic replays, smoke repositories, about three real issues, then normal volume.
- Start a run when the Ready label is added to an issue. A webhook delivers the trigger and the issue's run label and status comment mirror the run's state (D6).
- Use GitHub, the CARR record layer and T3 as the shared context system. Git versions the blueprint.
- Keep worker configuration separate from stage procedures. Agreed by the owner 2026-11-22.
- Add scorers, benchmarks and fix PRs driven by observed failures. Agreed by the owner 2026-11-22.
- Complete delivery on an observed merge. Agreed by the owner 2026-11-22.
- Keep typed outputs, budgets and exact-head checks. Agreed by the owner 2026-11-22.

[Notion’s book (archived)](https://web.archive.org/web/20261121015523/https://www.notion.com/a-new-shape-for-work)

## D1. What sends a pull request back?

Agreed by the owner 2026-11-22. Only a concrete P0 or P1 correctness, security or scope defect in the full current pull request sends it back. All other findings are comments. Every review uses a fresh session. Another model family is a preference. If that family is unavailable, dispatch uses the next qualified model in models.toml order, including the builder's family. Only the shared quota wait applies when no qualified model is available. Warp workers advise and can suggest revisions. [Warp transitions](https://docs.warp.dev/factories/blueprints/routes/)

## D2. Who queues the pull request?

Agreed by the owner 2026-11-22. “I'm good with the trust ladder”. Foreman queues the exact reviewed revision after required checks pass, under the authority earned through synthetic replays, live smoke, a limited batch of about three real issues and normal volume. GitHub enforces approvals and queue rules. Keep merge_held until a synthetic repository test proves the App and outside writers cannot move the branch during an open merge request. Release dependents only after GitHub confirms the observed merge. A person decides when to merge in Warp. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D3. Who approves a plan?

Agreed by the owner 2026-11-22. The checking model can approve the plan once Foreman validates the repository and seams. The plan proceeds when the independent check clears it. The human may comment on, approve or stop a plan through their coordinator or personal agent. The agent posts each response as an issue comment under the human's identity. Record the human’s “we're good” as approval of the plan they identified. The human's review is optional. Ask the human through the agent when an unresolved choice needs their judgment. Warp's default planning flow asks for human approval. [Warp gates](https://docs.warp.dev/factories/blueprints/gates/)

## D4. Keep the 400-line cap?

Agreed by the owner 2026-11-22. Remove the 400-line gate and its override. Report the changed-line count for the checked tree. Split work at real dependencies while planning. Warp's default flow encourages scoped work without a universal threshold. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D5. Which conversation continues?

Agreed by the owner 2026-11-22. Resume the builder's conversation when the same verified run and PR lineage continue on a compatible CLI. A fresh review session reads the full current PR each round. Warp can continue a worker's conversation during revisions. [Warp workers](https://docs.warp.dev/factories/factory-agents/)

## D6. How does a run start and show its state?

Agreed by the owner 2026-11-22: A. A run starts when the Ready label is added to an issue in any repository where the App is installed, on an organization or a personal account. The issue shows the run's state as one run label (Ready, Doing, Needs answer or Done) plus a status comment with the typed result and its links, so a halt stays distinct from a merge. Project boards are optional views. An organization board can collect labeled issues with GitHub's built-in auto-add workflow. GitHub sends `projects_v2_item` events only for organization-owned Projects, so a board column cannot be the trigger for personal repositories. Warp starts runs from filtered GitHub events, including issue labels. [Warp triggers](https://docs.warp.dev/factories/automation-triggers/) · [GitHub issues webhook](https://docs.github.com/en/webhooks/webhook-events-and-payloads#issues) · [GitHub Projects webhook](https://docs.github.com/en/webhooks/webhook-events-and-payloads#projects_v2_item)

## D7. Which worker methods belong in each step?

Agreed by the owner 2026-11-22: as recommended. Warp assigns reusable skills to workers. Keep the pinned how and why excerpts for research, architect for planning, statechart for stateful work, interrogate's evidence probe for the independent plan review, practical TDD for bug fixes, benchmark-checklist for performance work and review-quality's evidence rubric. Add five methods:

- Research on bugs and regressions builds a feedback loop first with Matt Pocock's `diagnosing-bugs`.
- Spec applies the vertical-slice and blocking-edge rules from Matt Pocock's `to-tickets` when a parent splits into children.
- Verify drives the change with the target repository's own `verify-<app>` skill (D11). pstack `create-verification-skill` creates it and `maintain-verification-skill` keeps it current.
- Implement runs the target repository's own formal verification skill, `formal-<area>`, on changes that touch the verified code. pstack `create-formal-verification-skill` creates it and `maintain-formal-verification-skill` updates it when a change is intended. Formal verification is opt-in per repository: it runs only where the repository has its own skill. Agreed by the owner 2026-11-23 ("A").
- Review checks what the change could break beyond the diff with pstack `blast-radius`.
- Fix PRs driven by observed failures, outside the stages, use pstack `correct`.

Leave out `to-spec` and Matt Pocock's `triage` and `code-review`. Measure the selection using the synthetic benchmarks. [Warp skills](https://docs.warp.dev/factories/factory-skills/)

## D8. Where should checks and evidence live?

Agreed by the owner 2026-11-22. Delete the proof step and universal tool-fact veto. Run Foreman's exact-tree check inside the editing loop, keep evidence the issue asks for, and let GitHub run its required checks. Warp's builder runs repository checks. [Warp issue-to-PR guide](https://docs.warp.dev/guides/agent-workflows/running-an-issue-to-pr-blueprint/)

## D9. Which paths count as existing seams?

Agreed by the owner 2026-11-22: (a). Only Git-tracked regular files in the pinned target checkout qualify as existing seams. Reject directories and symlinks. Apply the same rule to directly supplied and edit-only slices before the independent plan check. Warp exposes a plan gate. Foreman validates exact paths to bind the plan to the pinned checkout. [Warp gates](https://docs.warp.dev/factories/blueprints/gates/)

## D10. What happens after an interruption?

Agreed by the owner 2026-11-22: (a) with (ii). Resume at the interrupted step and reconcile before repeating a model call or remote write. If a model call's usage is ambiguous, charge the worst case to the budget, log usage unknown, and continue within the remaining allowance. Foreman reconciles calls and writes at the saved step because its local CLIs and GitHub effects can outlive the process. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D11. Where does end-to-end verification run?

Agreed by the owner 2026-11-23: "2. Warp does it that way for a reason. It's the better design choice", then "1-A, 2-yes, 3-yes". Warp runs user-level verification as its own stage with its own worker. The worker builds and starts the app from the pull request, uses it as a user would, and reports each criterion as passed, failed or unverified with evidence. It never edits, approves or merges, and the evidence complements code review rather than replacing it.

- A Verify stage runs between Implement and Review. It drives the published head with the target repository's own `verify-<app>` skill, found as a `verify-*/SKILL.md` in the target checkout. A repository without that skill gets `no_skill`, with no worker call.
- Foreman posts one evidence comment for the tested head. If the head moved, Foreman posts the evidence marked stale and returns to Implement, which reconciles the moved head as it does after Review. Warp's publication step ends instead. Foreman diverges because nothing may verify or review a head until Foreman has reconciled it.
- A known checked head that Merge returns for fresh review keeps the evidence already posted for it. Its code is unchanged, so Verify doesn't run again.
- The verifier prefers a qualified model from a different family from the builders, as the reviewer does.
- A failed criterion never routes straight to Implement. Review reads the evidence and decides severity.
- `verify-<app>` moves from Implement to the Verify worker, so the builder runs only the repository checks.
- The worker that reviews is called `reviewer`, and the new worker is called `verifier`, matching Warp's terms.

Warp's recipe starts on a maintainer's label. Foreman runs unattended, so Verify runs on every published head and feeds Review. [Warp Computer Use verification](https://docs.warp.dev/factories/use-cases/computer-use-verification/)
