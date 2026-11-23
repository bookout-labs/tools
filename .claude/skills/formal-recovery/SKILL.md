---
name: formal-recovery
description: "Model-check Foreman's crash recovery with TLA+: a paid worker call is launched at most once and charged exactly once, and publish never repeats a landed push, never overwrites foreign work, and reports published only at the checked head. Use when a change touches src/foreman/dispatch.py, AttemptJournal in src/foreman/worker.py, publish() in src/foreman/steps/implement.py, or the push, remote_head and pull_request_for adapters."
---

# Formal recovery check

Foreman can crash between any two steps, and GitHub or another person can act at any time. Tests cover the crash points someone thought of. These two TLA+ models check every order of steps within small bounds.

## The promises

| Model | Property | Plain promise | Harm if broken |
| --- | --- | --- | --- |
| `formal/DispatchRecovery.tla` | `LaunchedAtMostOnce` | A reserved worker call runs at most once. | A resume pays a provider twice for one attempt. |
| | `ChargedAtMostOnce` | A call is recorded in `attempts` once. | The run's budget is overcounted and stops early. |
| | `LaunchedIsCharged` | Every call that ran is charged when dispatch ends. | Paid work escapes the token and dollar caps. |
| | `ChargedWasSent` | Only a sent call is charged. | A call that never ran uses up the budget. |
| | `Ends` (and no deadlock) | Dispatch always finishes. | A run hangs after a crash. |
| `formal/PublishReconciliation.tla` | `PublishedIsChecked` | Published means GitHub showed this run's PR at the checked head. | Review and merge act on a head that was never checked. |
| | `NoRepeatedPush` | A push that already landed is never issued again. | Duplicate writes after a crash. |
| | `ForeignPreserved` | A branch someone else pushed stays theirs. | Foreman force-overwrites another person's work. |
| | `Publishes` | With no one else pushing, publication finishes once faults and crashes run out. | A run stays stuck in retry halts. |

## Run it

```sh
sh formal/check.sh                       # both models
sh formal/check.sh PublishReconciliation # one model
```

It needs Java 11 or later (`JAVA=/path/to/java` to choose one; on a Mac, `brew install openjdk@21`). It downloads TLA+ tools v1.7.4 once to `~/.cache/foreman/` and checks the jar's SHA-256. A pass prints each model's distinct state count and exits 0. CI runs the same command.

Bounds: `formal/DispatchRecovery.cfg` checks 2 models in the fallback list and 3 crashes. `formal/PublishReconciliation.cfg` checks 3 faults (failed reads, and pushes or creates that fail before or after they land) and 2 crashes. Both finish in about a second.

## Does my change touch this?

Covered code, with the model action for each step:

- `src/foreman/dispatch.py` `reserve`: `ReserveInsert` (`journal.reserve`) and `ReserveCommit` (the node's checkpointed update).
- `src/foreman/dispatch.py` `reconcile`: `ReconcileRead` (`journal.read`) and `ReconcileCharge` (the `attempts` record with `reservation: None` in one update, and fallback to the next model on quota).
- `src/foreman/dispatch.py` `DispatchRun.launch`: `LaunchCheck` (a halt before send), `LaunchSend` (`journal.send`), `LaunchCall` (`self.handler`) and `LaunchFinish` (`journal.finish`).
- `src/foreman/worker.py` `AttemptJournal`: the `journal` row states. `reserve` inserts NULL, `send` sets `X''` only where the row is NULL, `finish` stores the result, and `read` maps NULL to launch, `X''` to interrupted and a result to itself.
- `src/foreman/steps/implement.py` `publish`: `ReadRemote`, `ReadMovedPr`, `Push`, `AfterPush`, `ReadPr`, `Create`, `AfterCreate` and `Check`, in that order.
- `src/foreman/workspace.py` `LiveWorkspace.push` (no `--force`) and `remote_head`, and `src/foreman/github.py` `LiveForge.pull_request_for` and `open_pull_request`.

A change touches the check if it adds, removes, reorders or merges any of these steps; changes what is written in one checkpointed update; changes which journal state launches, charges or halts; adds a write or a read in publish; or changes how a halt or crash resumes (`src/foreman/runner.py`). If it does, edit the matching model action to describe the new code before you rerun. Keep one indivisible step in the code as one action in the model.

## When it fails

TLC prints the violated property and a numbered trace of states. Read the trace as steps in the code: each state's `pc` names the step about to run, and a jump in `crashes` is a crash.

- **Bug.** Every step in the trace can happen in the code. Report the property, the harm from the table, and the trace in the code's words. Do not weaken the property or the model to make it pass.
- **Modeling error.** A step in the trace cannot happen in the code, for example two code steps merged into one action, or a branch the code does not have. Fix the model to match the code and say what was wrong.

## Show the check can still fail

After any change to a model, break the code's rule in the model and confirm that TLC catches it. Then undo the break. These breaks were each caught at the sync commit below:

- Call `self.handler` before `journal.send`: `LaunchedIsCharged` fails.
- Relaunch an `X''` row, without the `IS NULL` guard: `LaunchedAtMostOnce` fails when checked alone; with every property, TLC reports `LaunchedIsCharged` first.
- Checkpoint the attempt record apart from clearing `reservation`: `ChargedAtMostOnce` fails.
- Charge a halt that came before `send`: `ChargedWasSent` fails.
- Insert the journal row after the reserve checkpoint: TLC reports a deadlock.
- Push even when `remote_head` already equals the checked head: `NoRepeatedPush` fails.
- Drop the `pr.headRefOid != head` check: `PublishedIsChecked` fails.
- Let push overwrite a foreign head (force push): `ForeignPreserved` fails.
- Make `publication_unreconciled` final instead of a retry: `Publishes` fails (with `-deadlock`, TLC reports the temporal violation).

## Scope

The check covers one stage call's dispatch and the publish step, after commit has checkpointed the checked head. It assumes LangGraph commits each node's update atomically (durability `sync`), that SQLite commits each journal call, that git rejects a non-fast-forward push without `--force`, and that GitHub keeps one open PR per branch. It does not cover the quota pause and wake, the dollar arithmetic, review posting or merge, prepare, edit or commit, or more than two fallback models.

## Keep it in step

Coverage last confirmed at commit `1786f8d0dc95aa946b2c0805ee099e26128e208a`. Before you trust a pass, run `git log 1786f8d0dc95aa946b2c0805ee099e26128e208a..HEAD -- src/foreman/dispatch.py src/foreman/worker.py src/foreman/steps/implement.py src/foreman/workspace.py src/foreman/github.py src/foreman/runner.py` and read each commit against the list above. Move this commit forward only after you confirm that the models describe the code at the new commit. For drift that built up without this skill, use pstack `maintain-formal-verification-skill`.
