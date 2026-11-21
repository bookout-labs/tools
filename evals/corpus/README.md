# Known-answer corpus v1

This is the public contract for #164. The repository is public, so it stores
commitments only. Case requests, answers, reference metadata, and receipts belong
in an evaluator-only private store. No private store is provisioned by this slice.
A digest does not establish secrecy or isolation. These cases are not ready for
worker replay until #165 proves the boundary below.

## Case format

`commitments.json` lists opaque case IDs and SHA-256 digests of private `case.json`
files, plus the frozen request and answer digests. `case.schema.json` defines
those private files. All content digests are lowercase
SHA-256 over exact file bytes, with no newline or JSON normalization. Git commit
identities use full 40-character object IDs. Every private artifact has a relative
filename and digest. Paths resolve within the private case directory. Symlinks,
absolute paths, and parent traversal are forbidden.

A private case directory contains these artifacts.

- `request.md` freezes the amended issue title and complete body, including its
  approved spec revision. Later issue edits create a new case version.
- `answer.py` contains independent behavioral assertions, never a patch comparison.
  Its command is immutable and runs only in the trusted grader after worker exit.
- `review.json` records the independent acceptance receipt, checked head, verdict,
  original receipt URL or identifier, and check evidence. A PR title is insufficient.
- `wrong.patch` describes a representative incorrect behavior relative to the
  shipped result. The same answer must reject it with an assertion failure.
- `positive.log` and `negative.log` preserve actual grader output. Import errors,
  missing dependencies, and zero tests do not demonstrate a negative activation.

`case.json` binds those artifacts to the original issue, immutable start and result
commits, checked PR and head, expected outcome, and observed test results. The
checked head may differ from the squash result. The receipt must cover that head;
curation must verify the feature files match and record unrelated base changes
before accepting the squash result.
An answer command uses trusted `PYTHON`, `CANDIDATE`, and `ANSWER` absolute paths.
The candidate is the submitted source tree. The answer directory is private and
read-only to the grader. The command must not depend on candidate-owned tests for
its expected assertions. It may exercise candidate test doubles as specified APIs.
Missing artifacts, digest mismatches, absent receipts, or unavailable commands
mean incomplete, never pass. Commands are trusted operator input, never worker input.

## Worker boundary owned by #165

The worker receives only `request.md` and an export of the start tree. It receives
no `case.json`, commitments, answer, review receipt, result SHA, reference PR,
failing variant, or grader output. It receives no `.git`, later objects, remote,
GitHub token, or App key, and cannot fetch over the network. Export means files
from the start commit, not a clone or a checkout of current main.

If the isolation boundary is unavailable, #165 reports isolation as incomplete,
never pass.

Answers and reference metadata load into a separate trusted grader only after
the worker exits. Keeping them in an ignored directory, on a sibling path, in git
history, or in a public PR does not satisfy this boundary. #165 must deny probes
for filesystem access, git history, public PR retrieval, and a direct Forge write.
It must use fake Forge and prohibit all live publication and merge effects.
This contract adds no runner, graph node, or fixture-manifest job store.

## Acceptance and remaining cases

A case is curated when its frozen result passes its answer, its representative
wrong behavior fails that same answer, and an independent acceptance receipt is
pinned. A fresh independent acceptance review of the answer's coverage remains
necessary before promoting newly curated answers. Isolation is a separate receipt.

The commitments cover a feature case and a provisional bug case. Their private
bundles are local curation staging only. They must be transferred into an
evaluator-only private store outside all worker mounts, credentials, and network
access before use. Each curation PR identifies its staging location and unmet
obligations. The bug case has positive and negative grader evidence, but its
independent acceptance receipt is not yet pinned. It remains incomplete under
164-D1. Both answers still need independent coverage review and #165 isolation.

A parent answer must compare semantic children and dependencies after normalizing
IDs and titles. It checks acceptance criteria, seams, parent links, coverage,
no extra or duplicate children, acyclic producer-before-consumer edges, inherited
contract revision, and work kind. For the synthetic parent fixture, dispatch waits
for foundation merge and resume repeats no create or link effects. Keeping titles
while dropping or reversing an edge, or weakening acceptance, must fail.
A synthetic fixture does not complete the requirement for a real shipped parent.
