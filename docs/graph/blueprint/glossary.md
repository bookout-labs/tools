# Foreman glossary

| Term | Meaning |
| --- | --- |
| `foreman_repository` | The `bookout-labs/tools` repository that contains Foreman and its blueprint. |
| `target_repository` | The repository of the admitted issue and the only destination for that run's changes. |
| `target_checkout` | The pinned checkout of the target repository. Implementation owns its editable worktree. Verify runs the app there read-only and writes captures to its attempt scratch. |
| Blueprint | The versioned design the human approves for future Foreman runs. |
| Run | One execution for a GitHub issue under a pinned blueprint and runtime release. |
| Stage | One of seven LangGraph subgraphs. A stage owns a result and its permitted routes. |
| Step | A checkpointed node inside a stage or shared dispatch. |
| Worker | An assigned role that uses a CLI and configured model policy to perform a procedure. |
| Procedure | The prompt, skills, inputs and typed worker output for a step that calls a worker. |
| Model policy | The existing configuration for eligible models, ordering and fallback. |
| Slice | One piece of implementation with acceptance, owned seams and declared dependencies. |
| Seam | A named interface or file a slice provides or uses. |
| Candidate | A saved plan or supplied slice bound to a repository, issue, base revision and content digest. |
| Ready | The draft worker says the candidate is complete enough to validate. |
| Approval | An authorized person's explicit assent for the named choice, plan revision or PR head. For a choice or plan, the coordinator or personal agent posts the approval as an issue comment under the human's identity. |
| Gate | A saved pause for the human's answer to a specific unresolved choice. The coordinator or personal agent posts the answer or cancellation as an issue comment under the human's identity. |
| Settled ruling | A saved human answer that later checks of the same issue get as context and cite by ID. |
| Halt | A named terminal stop with evidence. A provider or GitHub wait keeps the run open. |
| Checkpoint | LangGraph's saved continuation and input records for this run. |
| Reservation | A durable record of authorized spending and identity persisted before a paid worker call. |
| Attempt | One worker execution identified by its reservation and recorded response or usage. |
| Base revision | The Git commit that pins the starting target checkout. A review also records its PR diff base. |
| Head | The exact commit on the PR's source branch at the moment of observation. |
| Checked tree | The exact tree whose unchanged contents passed the configured target repository check. |
| Receipt | Saved evidence of one observed GitHub, Git or worker result. |
| Arm | Ask GitHub to queue or auto-merge the exact reviewed commit when eligible. |
| P0 / P1 | Typed priorities for critical or serious evidenced correctness, security or scope defects. |
| Advisory | A finding that can be posted without requesting implementation again. |
| Waiver | An exact, temporary structural difference with a migration issue and a condition for removal. |
| Scorer | A versioned rubric that assesses recorded work without deciding workflow routes. |
| Hand fix | A person modifies the change or repairs a run to get the PR to merge. |
| Criterion | One acceptance criterion of a slice. Verify reports each one as passed, failed or unverified. |
| Evidence | What the verifier did and observed for each criterion at the tested head, and where its captures are saved. Foreman posts it as one comment for that head, marked stale if the head moved. |
