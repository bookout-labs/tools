Implement the change in this worktree.
Work on the slice branch named `<issue>-<short-name>`. Follow the spec when one is present.
Commit that change when this worktree can record the commit.
If git cannot write the commit here, leave the completed files in this worktree. The runner commits them on that branch.
Do not commit from another checkout.
Report the implementation and its head commit SHA as `head_sha`.
The implementation value is one document: a string, or an object whose values are the sections of that document.
A test must check a behavior the change can break. Do not add a test that restates the code.

The model list is config outside this node. Do not edit it.
