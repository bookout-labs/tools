Write the spec. Cover the behavior, the product decisions, and the technical design.
Set `spec_gate` to `ready` when a person could implement from the spec.
Read the target checkout before asking a person. Use Read, Glob and Grep to inspect
existing behavior and answer questions the code and issue can settle yourself.
Reserve `needs_human` for intent, product or policy choices, or external facts absent
from the code and issue. Return non-empty `questions`, each with `question`,
`why_code_unanswered` and workspace-relative `checked_paths` naming files you read.
Use empty `checked_paths` only if no checkout file covers the topic, explaining why
in `why_code_unanswered`. On `ready`, return `questions: []`.

Follow the output contract for builds, uses, and edit-only slices.
If the state contains `spec_rejection`, fix that problem before returning a ready spec.
The graph orders the slices before filing any child issues.

The model list is config outside this node. Do not edit it.
