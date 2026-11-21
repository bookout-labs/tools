# Tools

Shared tools for the engine repositories. This repo lives in the bookout-labs organization. An engine does not copy it. The engine keeps a `tools.yaml` pointer.

`foreman` is the graph runner in this repo. Promptfoo grades the graph. Both are invoked from here, so every engine follows the same loop.

## Delivery graph

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	triage(triage)
	research(research)
	spec(spec)
	implement(implement)
	verify(verify)
	review(review)
	proof(proof)
	needs_human(needs_human)
	merge(merge)
	done(done)
	__end__([<p>__end__</p>]):::last
	__start__ -.-> done;
	__start__ -.-> triage;
	implement -.-> done;
	implement -.-> verify;
	merge --> done;
	needs_human -.-> spec;
	needs_human -.-> triage;
	proof -.-> done;
	proof -.-> implement;
	proof -.-> review;
	research --> done;
	review -.-> done;
	review -.-> implement;
	review -.-> merge;
	spec -.-> done;
	spec -.-> implement;
	spec -.-> needs_human;
	triage -.-> done;
	triage -.-> implement;
	triage -.-> needs_human;
	triage -.-> research;
	triage -.-> spec;
	spec -.-> spec;
	verify -.-> done;
	verify -.-> implement;
	verify -.-> proof;
	verify -.-> review;
	done --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc

```

Generate the diagram with `foreman diagram delivery`. The model list is `src/foreman/models.toml`. Quota exhaustion or a missing CLI tries the next model in that list. A node does not edit the list, and it does not decide the next node.

Review excludes every model family that implemented any round of the change. A review reports alignment, impact, verification, rollback, and error detection. It rates merge risk as low, medium, or high.

Foreman fetches the pull request's base and verified head from `origin` and reviews their three-dot diff. Each finding names a file, line, severity (`blocking` or `nit`), and a summary of up to 200 characters. Findings on changed lines in the reviewed commit post inline. Other findings appear with their locations in the GitHub review body alongside `detail`. A changes verdict requires findings and sends them with the detail to the next implementation round. The checkpoint marks a changes verdict with only nit findings as `review_change_kind: nits`.

An unavailable PR or diff stops the run with `review_unavailable`. A failed GitHub review post stops it with `review_not_posted`. Invalid stage output stops with `invalid_output`. If every eligible CLI is missing, the run stops with `cli_missing`. If the builder model is missing or every eligible reviewer is out of quota, the run stops with `no_reviewer`. The record includes `halt_detail` for these stops.

Live verify stops with `dirty_worktree` if the checkout has changes or Git cannot report a full HEAD SHA. Otherwise it runs the check command in the engine checkout and records that SHA if the check passes. Dry runs skip preflight and use a scripted verdict.

Use `--preflight <command>` to check environment readiness in the engine checkout. Each new or resumed invocation checks readiness once, just before its first implement dispatch. Routes that never reach implement skip the probe. After an ordinary check failure, Foreman runs preflight again if configured. A passing preflight leaves the check failure in the round count. A failing preflight stops the run as `env_unavailable` without consuming a round. Check exits 126 (command is not executable) and 127 (command not found) also stop as `env_unavailable`. The stop detail includes the failed command and its exit code.

Restore the environment and rerun the same command to resume from the saved checkpoint.

When the check passes, the graph evaluates the browser and formal evidence required by the slice. Missing evidence stops the run. A failed proof consumes a round and sends the slice back to implement while rounds remain.

The cap is three failures across slice planning, verify, proof, and review. It is config. A node cannot raise it.

After approval, Foreman looks up the pull request for the checked-out branch and arms it with `gh pr merge <pull-request> --repo <repo> --squash --auto --match-head-commit <sha>`. The worktree and pull request must still be at the verified SHA, the worktree must be clean, and the open pull request must target `main` from a branch starting with the issue number and a hyphen.

Foreman looks up the local branch by name. If the pull request uses a differently named upstream branch, the lookup fails and Foreman leaves the merge unarmed.

Before arming, Foreman reads the target repository's GitHub Actions variable `FOREMAN_MERGE_HOLD`. A non-empty value or a read error pauses the run as `merge_held` and records the value or error. An unset or empty value permits arming. Clear the hold or resolve the read error, then rerun the same command to retry the merge.

If the merge command times out, Foreman checks the same pull request at the verified SHA. An open pull request with a squash auto-merge request or a merged pull request confirms the result. A failed merge check or unconfirmed command pauses the run as `merge_not_armed` and records the reason. Resolve it and rerun the same command to retry. If HEAD moved, run `git reset --keep <verified-sha>` on the PR branch before rerunning. New commits need a new run. A dry run prints `merge skipped (dry run)` with the command template and reports the scripted verification result.

Stage prompts live in `src/foreman/stages/`. The code-quality skill supplies the scorer rubric. The merge-risk skill is attached to review. A node reads those files. It does not invent the loop.

A parent or slice goes straight to implement when the behavior is decided and the change fits in one pull request. A parent on this route is its own slice. It carries the same proof obligations as any slice and files no child issues. Changes that need more than one pull request or leave a product choice open go to spec. An informational request goes to spec on a parent and to research on a slice. An open product choice pauses a live run for a person's answer.

A ready parent spec lists the seams each slice builds and uses. Foreman checks existing seam paths in the workspace and orders the slices so builders come first. A cycle, dangling seam, duplicate builder, or seam marked existing that a slice also builds sends the reason back to spec and uses a round. At the cap, the run stops as `spec_cap` without filing children. Single-slice parents skip ordering.

Foreman records the ordered plan and stops the parent as `sliced`. Each child issue carries a fenced JSON record of its slice and native GitHub blocked-by links for its dependencies. A live slice run stops as `missing_slice` if the record is missing or invalid. It reports `blocked` before triage while a blocker is open, a planned link is missing, or a planned blocker has no merged pull request. Run the same slice again after its blockers clear. Rerunning the parent restores missing links and adds the `factory` label when a dependent slice is ready.

A coordinator session reads `src/foreman/roles/coordinator.md` from the tools revision named in the engine. It files the parent issue and explains the run. It does not slice, build, or arm a merge.

## What enters the graph

`foreman accept` reads one event and either ignores it or prints the request. It does not call a model.

A GitHub event starts when it is `issue_labeled` and the labels include `factory`. The command ignores alerts and other GitHub events. It rejects unknown sources.

```yaml
source: github
event: issue_labeled
labels: [factory]
title: Encode the timecode
body: The comment thread crashes.
```

```sh
foreman accept event.yaml
```

## Scoring

`src/foreman/measure.toml` holds the sampling rate, the passing bar, and the label scores. One implementation run in every ten is sampled for scoring. Merge arming uses verification and review independently of scoring.

- `approve` scores 1 and passes.
- `block` scores 0.2 and fails. A produced artifact that fails its proof or screen check is `block`.
- `insufficient_evidence` scores 0.5 and always remains a distinct non-pass. A missing required proof or browser trace leaves the work unjudged.

`foreman improve` proposes a change for a known scoring failure. For redundant tests, it targets the builder instructions in `src/foreman/stages/implement.md`. The proposal does not change `models.toml` or the review cap. A person opens the pull request.

```sh
foreman improve redundant-tests
```

Promptfoo runs the same contract without a model:

```sh
sh evals/eval.sh
```

## Point an engine at it

Put this file at the root of the engine and name it `tools.yaml`.

```yaml
source: bookout-labs/tools
ref: main
graph: delivery
check: npm run check
```

Examples for the current engines are in `examples/`. `ref` should move from `main` to a tag once a graph change should wait for the engine to opt in.

Check a pointer:

```sh
foreman check-pointer tools.yaml
```

Walk the graph without calling a model:

```sh
foreman run delivery --dry-run --repo DoctorCRE/doctorcre-engine --request "Add a field" --check "npm run check"
```

Print the graph:

```sh
foreman diagram delivery
```

Start a live run from the root of the engine checkout. Use `--request` to supply the issue text when it is not available in the checkout.

```sh
foreman run delivery --repo DoctorCRE/doctorcre-engine --issue 42 --check "npm run check" --request "Add a field"
```

The runner uses the pinned model slugs from `models.toml` with the installed Codex, Claude, and Grok CLIs. SQLite saves state by repo and issue under `$XDG_STATE_HOME/foreman/checkpoints.sqlite3`, or `~/.local/state/foreman/checkpoints.sqlite3` by default. Run the same issue again from the same checkout to see its saved state. If you see a question, add `--answer "…"` to continue with the saved request, check command, and any preflight command.

Before review, Foreman fetches the pull request's base branch from `origin` and counts changes in `src/` and `tests/` against the merge base. Use Git 2.43 or later. Files marked `linguist-generated` in the merge base do not count. If Foreman presents a split plan, use `--answer split` to accept it or `--answer override` to continue with that measured diff.

## Tests

```sh
uv sync
uv run python -m unittest discover -s tests
```
