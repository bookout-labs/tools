# Draft the plan

Read the target checkout at the named base and the related repository checkouts. Draft slices for
the admitted repository and issue only. Each slice states observable behavior: acceptance criteria,
non-goals and how each criterion is proven, by test, drive or contract. A test or contract proof
names behavior the base lacks, so its acceptance test fails at the base. Prove a criterion the base
already meets, as in a refactor or a perf change, by drive. Mark a drive criterion that cannot be driven, with the reason. Never name a code
edit, an expression or a function to write. Give each slice its depends_on parents and an advisory
list of files you expect it to touch. List each existing test a slice removes or renames, with the
reason. Name it by its file and, when only some of the file's tests go, by the test's name as the
source states it. List every fact the plan relies on outside the target
checkout as an assumption, citing a tracked file in a related repository checkout at its pinned
commit. If you cannot cite it, ask a grounded question instead. A plan that needs a change to the
check command, CI workflows, the verify skill, accepted tests or the contract file puts that change
in its own slice, filed as its own issue that the feature slice depends on. File a CI workflow slice
without Ready. Every other protected slice carries Ready and runs first. On revision, use the saved candidate,
findings and settled rulings. Return one typed draft or grounded questions about product,
repository, architecture or policy for the coordinator or personal agent to relay.
