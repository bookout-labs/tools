Write the spec. Cover the behavior, the product decisions, and the technical design.
Set `spec_gate` to `ready` when a person could implement from the spec.
If a product choice is still open, set `spec_gate` to `needs_human` and put the question in `human_question`.

When a parent spec is ready, return `slices` with one entry per pull request. Keep each pull request to about 400 changed lines or fewer.

The graph records the slices and stops the parent run.

The model list is config outside this node. Do not edit it.
