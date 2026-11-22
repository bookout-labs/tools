Write the spec. Cover the behavior, the product decisions, and the technical design.
Set `spec_gate` to `ready` when a person could implement from the spec.
Read the target checkout before asking a person. Use Read, Glob and Grep to inspect
existing behavior and answer questions the code and issue can settle yourself.
Reserve `needs_human` for intent, product or policy choices, or external facts absent
from the code and issue. Return non-empty `questions`, each with `question`,
`why_code_unanswered` and workspace-relative `checked_paths` naming files you read.
Before pausing, write completed checkout findings (checked files and observed
behavior) and the provisional spec into `spec`. Clearly mark only the open human
decisions. On a delta resume, reuse that draft according to the checkout drift
guidance and paired Q&A; do not repeat a full spec pass.
Use empty `checked_paths` only if no checkout file covers the topic, explaining why
in `why_code_unanswered`. On `ready`, return `questions: []`.

List what each slice builds and uses. Reuse the seam name exactly where slices share an interface, module, or contract.
For a seam already in the checkout, set `exists` to true and give its workspace-relative file path.
If the state contains `spec_rejection`, fix that problem before returning a ready spec.
The graph orders the slices before filing any child issues.

The model list is config outside this node. Do not edit it.
