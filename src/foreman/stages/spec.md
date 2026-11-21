Write the spec. Cover the behavior, the product decisions, and the technical design.
Set `spec_gate` to `ready` when a person could implement from the spec.
If a product choice is still open, set `spec_gate` to `needs_human`.

List what each slice builds and uses. Reuse the seam name exactly where slices share an interface, module, or contract.
For a seam already in the checkout, set `exists` to true and give its workspace-relative file path.
If the state contains `spec_rejection`, fix that problem before returning a ready spec.
The graph orders the slices before filing any child issues.

The model list is config outside this node. Do not edit it.
