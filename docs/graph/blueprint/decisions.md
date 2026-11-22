# Decisions for Joe

Decisions D6, D7, D9 and D10 remain open.

## Agreed direction

Agreed by Joe 2026-11-22. “i agree with all of your take aways from this book” and “if I didn't comment on any of those takeaways, then I'm good with them”.

- Roll out each version and its merge authority through synthetic replays, smoke repositories, about three real issues, then normal volume.
- Start a run when an issue enters Ready on a GitHub Project board. A webhook delivers the trigger and the board mirrors the run's state.
- Use GitHub, the CARR record layer and T3 as the shared context system. Git versions the blueprint.
- Keep worker configuration separate from stage procedures. Agreed by Joe 2026-11-22.
- Add scorers, benchmarks and fix PRs driven by observed failures. Agreed by Joe 2026-11-22.
- Complete delivery on an observed merge. Agreed by Joe 2026-11-22.
- Keep typed outputs, budgets and exact-head checks. Agreed by Joe 2026-11-22.

[Notion’s book (archived)](https://web.archive.org/web/20261121015523/https://www.notion.com/a-new-shape-for-work)

## D1. What sends a pull request back?

Agreed by Joe 2026-11-22. Only a concrete P0 or P1 correctness, security or scope defect in the full current pull request sends it back. All other findings are comments. Every review uses a fresh session. Another model family is a preference. If that family is unavailable, dispatch uses the next qualified model in models.toml order, including the builder's family. Only the shared quota wait applies when no qualified model is available. Warp workers advise and can suggest revisions. [Warp transitions](https://docs.warp.dev/factories/blueprints/routes/)

## D2. Who queues the pull request?

Agreed by Joe 2026-11-22. “I'm good with the trust ladder”. Foreman queues the exact reviewed revision after required checks pass, under the authority earned through synthetic replays, live smoke, a limited batch of about three real issues and normal volume. GitHub enforces approvals and queue rules. Keep merge_held until a synthetic repository test proves the App and outside writers cannot move the branch during an open merge request. Release dependents only after GitHub confirms the observed merge. A person decides when to merge in Warp. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D3. Who approves a plan?

Agreed by Joe 2026-11-22. The checking model can approve the plan once Foreman validates the repository and seams. The plan proceeds when the independent check clears it. Joe may comment, bless or stop a plan through the issue or the coordinator. Record his “we're good” as approval of the plan he referred to. His review is optional. Ask Joe when an unresolved choice needs his judgment. Warp's default planning flow asks for human approval. [Warp gates](https://docs.warp.dev/factories/blueprints/gates/)

## D4. Keep the 400-line cap?

Agreed by Joe 2026-11-22. Remove the 400-line gate and its override. Report the changed-line count for the checked tree. Split work at real dependencies while planning. Warp's default flow encourages scoped work without a universal threshold. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)

## D5. Which conversation continues?

Agreed by Joe 2026-11-22. Resume the builder's conversation when the same verified run and PR lineage continue on a compatible CLI. A fresh review session reads the full current PR each round. Warp can continue a worker's conversation during revisions. [Warp workers](https://docs.warp.dev/factories/factory-agents/)

## D6. Which Project board and columns should Foreman use?

The Ready webhook and visible run state are agreed. Pilot on one team-owned GitHub Project. Use Ready for to-do, Doing for active work, Needs answer while Joe's decision is pending, and Done for typed terminal outcomes. Show the result on the card so a halt remains distinct from a merge. Joe chooses the Project and exact column mapping. Reusing an existing board saves setup and may require mapping its status names. The App needs Projects access and a webhook subscription, and the board needs tests for duplicate delivery and updates from Foreman itself. Warp can start runs from filtered GitHub events and custom webhooks. Its documented native GitHub events do not list Project status changes. [Notion’s book (archived)](https://web.archive.org/web/20261121015523/https://www.notion.com/a-new-shape-for-work) · [Warp triggers](https://docs.warp.dev/factories/automation-triggers/) · [GitHub Projects webhook](https://docs.github.com/en/webhooks/webhook-events-and-payloads#projects_v2_item)

## D7. Which worker methods belong in each step?

Warp assigns reusable skills to workers. Use the pinned how and why excerpts for research, architect for planning, statechart for stateful work and interrogate's evidence probe for the independent plan review. Use practical TDD for bug fixes and benchmark-checklist for performance work. Keep review-quality's evidence rubric. The pinned interrogate file has no Lead Judgment section. Using the current skill rules would retain more general guidance, including duplicate review instructions and universal proof methods. Measure the narrower selection using the synthetic benchmarks. [Warp skills](https://docs.warp.dev/factories/factory-skills/)

## D8. Where should checks and evidence live?

Agreed by Joe 2026-11-22. Delete the proof step and universal tool-fact veto. Run Foreman's exact-tree check inside the editing loop, keep evidence the issue asks for, and let GitHub run its required checks. Warp's builder runs repository checks. [Warp issue-to-PR guide](https://docs.warp.dev/guides/agent-workflows/running-an-issue-to-pr-blueprint/)

## D9. Which paths count as existing seams?

Repository and seam validation followed by a separate plan check is agreed. Require existing seams to be tracked regular files in the pinned checkout. Apply the same rule to directly supplied and edit-only slices. Allowing directories or symlinks would need explicit containment and identity rules. The precise seam-path policy remains open. Warp exposes a plan gate. [Warp gates](https://docs.warp.dev/factories/blueprints/gates/)

## D10. What happens after an interruption?

Warp exposes persistent work and worker conversations. Keep Foreman's checkpoint at the pending step and reconcile before paying again or repeating a remote write. If provider evidence cannot establish usage, make that uncertainty visible. Rewinding a whole stage would risk rerunning a worker or repeating an already accepted write. [Warp lifecycle](https://docs.warp.dev/factories/how-factories-work/)
