Read the request and set `triage` to one of these values.

- implement, when the behavior is decided and the change fits in one pull request of about 400 changed lines or fewer. This applies to parents and slices.
- spec, when the change needs more than one pull request or a product choice is open
- question, when the request asks for information and not a change
- needs_human, when you cannot tell which of those it is

When a parent issue triages as `implement`, return one `slice` for it.
When `triage` is `needs_human`, put the question for the person in `human_question`.

The model list is config outside this node. Do not edit it.
