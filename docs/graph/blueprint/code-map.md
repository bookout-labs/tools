# Foreman code map

Paths are relative to the Foreman repository. Planned owners may be absent at the pinned baseline.

## Triage

Read target_repository issue and dependencies. Update the issue's run label and status comment. No repository edits or PR writes.

Entry `src/foreman/steps/triage.py::compile_stage`. Maximum 110 physical lines in the owner module.

Never create child issues, edit source.

Uses Forge, dispatch.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| triage.admit | `src/foreman/steps/triage.py::admit` | TR1 |
| triage.route | `src/foreman/steps/triage.py::route` | TR2 |

Baseline files [`src/foreman/accept.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/accept.py), [`src/foreman/runner.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/runner.py), [`src/foreman/delivery.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/delivery.py), [`src/foreman/stage_output.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stage_output.py), [`src/foreman/state.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/state.py).

## Research

Read target_checkout at the pinned base and the admitted issue. Write one issue answer.

Entry `src/foreman/steps/research.py::compile_stage`. Maximum 85 physical lines in the owner module.

Never edit source, file child issues.

Uses Forge, dispatch.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| research.answer | `src/foreman/steps/research.py::answer` | RE2 |
| research.post | `src/foreman/steps/research.py::post` | RE1 |

Baseline files [`src/foreman/delivery.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/delivery.py), [`src/foreman/github.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/github.py), [`src/foreman/stage_output.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stage_output.py).

## Spec

Read-only target_checkout at pinned base until filing. GitHub child writes stay in target_repository.

Entry `src/foreman/steps/spec.py::compile_stage`. Maximum 410 physical lines in the owner module.

Never edit source, choose a replacement target repository.

Uses Forge, Workspace, dispatch.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| spec.draft | `src/foreman/steps/spec.py::draft` | SP1 |
| spec.validate | `src/foreman/steps/spec.py::validate` | SC1 |
| spec.check | `src/foreman/steps/spec.py::check` | SC2 |
| spec.file_children | `src/foreman/steps/spec.py::file_children` | FS1 |

Baseline files [`src/foreman/delivery.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/delivery.py), [`src/foreman/stages.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stages.py), [`src/foreman/spec_check.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/spec_check.py), [`src/foreman/stage_output.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stage_output.py), [`src/foreman/github.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/github.py), [`src/foreman/workspace.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/workspace.py), [`src/foreman/runner.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/runner.py).

## Implement

Exclusive target_checkout, candidate issue, attempt parent, owned branch and matching PR. No direct base-branch writes.

Entry `src/foreman/steps/implement.py::compile_stage`. Maximum 420 physical lines in the owner module.

Never merge the PR, run a standalone proof gate, require a universal tool-fact record.

Uses Workspace, Forge, dispatch.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| implement.prepare | `src/foreman/steps/implement.py::prepare` | IM1 |
| implement.edit | `src/foreman/steps/implement.py::edit` | IM2 |
| implement.commit | `src/foreman/steps/implement.py::commit` | IM4 |
| implement.publish | `src/foreman/steps/implement.py::publish` | PU2 |

Baseline files [`src/foreman/delivery.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/delivery.py), [`src/foreman/workspace.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/workspace.py), [`src/foreman/check_output.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/check_output.py), [`src/foreman/github.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/github.py), [`src/foreman/stage_output.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stage_output.py).

## Review

Full PR diff at current head and recorded diff base. Review worker has read-only access.

Entry `src/foreman/steps/review.py::compile_stage`. Maximum 235 physical lines in the owner module.

Never edit implementation, submit human approval, impose tool-fact bookkeeping.

Uses Forge, dispatch.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| review.inspect | `src/foreman/steps/review.py::inspect` | RV1 |
| review.post | `src/foreman/steps/review.py::post` | RV2 |
| review.route | `src/foreman/steps/review.py::route` | RV3 |

Baseline files [`src/foreman/delivery.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/delivery.py), [`src/foreman/stages.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stages.py), [`src/foreman/review_context.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/review_context.py), [`src/foreman/review_round.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/review_round.py), [`src/foreman/github.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/github.py), [`src/foreman/stage_output.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stage_output.py).

## Merge

Exact target repository PR, branch, base, checked head and GitHub merge receipt.

Entry `src/foreman/steps/merge.py::compile_stage`. Maximum 240 physical lines in the owner module.

Never direct merge, submit human approval, infer merged from queued.

Uses Forge.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| merge.arm | `src/foreman/steps/merge.py::arm` | ME1 |
| merge.observe | `src/foreman/steps/merge.py::observe` | ME2 |
| merge.release | `src/foreman/steps/merge.py::release` | ME3 |
| merge.report | `src/foreman/steps/merge.py::report` | DN1 |

Baseline files [`src/foreman/delivery.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/delivery.py), [`src/foreman/github.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/github.py), [`src/foreman/readiness.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/readiness.py), [`src/foreman/runner.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/runner.py), [`src/foreman/outcomes.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/outcomes.py).

## Dispatch

Bound each paid attempt and recover its result once.

Entry `src/foreman/dispatch.py::compile_dispatch`. Maximum 250 physical lines in the owner module.

Never choose workflow routes, publish artifacts, guess usage.

Uses runner, worker, budget.

| Step | Owner symbol | Scenario |
| --- | --- | --- |
| dispatch.reserve | `src/foreman/dispatch.py::reserve` | DP4 |
| dispatch.reconcile | `src/foreman/dispatch.py::reconcile` | DP2 |

Baseline files [`src/foreman/runner.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/runner.py), [`src/foreman/dispatch.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/dispatch.py), [`src/foreman/worker.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/worker.py), [`src/foreman/worker_auth.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/worker_auth.py), [`src/foreman/config.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/config.py), [`src/foreman/budget.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/budget.py), [`src/foreman/stages.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/stages.py), [`src/foreman/skill_rules.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/skill_rules.py), [`src/foreman/adapters/claude.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/adapters/claude.py), [`src/foreman/adapters/codex.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/adapters/codex.py), [`src/foreman/adapters/grok.py`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/adapters/grok.py), [`src/foreman/models.toml`](https://github.com/bookout-labs/tools/blob/b96293d6e6403e854097cb8c2c3d94afe747e286/src/foreman/models.toml).
