# Target repository setup

This page is generated from doctor's requirement list by `foreman setup-page --write`. Do not edit it by hand.
Run `foreman doctor --repo OWNER/NAME` to check a repository against every requirement below.

## Quick check

Doctor reads GitHub and the contract file at every admission, before any paid call.

### The GitHub App is installed on the repository and GitHub lets it make doctor's reads

**Why:** Foreman reads and writes the repository only as its GitHub App, and GitHub delivers the events that start and wake runs to the App's installation.

**How:** Install the Foreman GitHub App on the repository with every permission it requests, subscribed to issues, issue_comment, pull_request, pull_request_review, check_suite, status. The Foreman host sets FOREMAN_APP_ID and the App's private key at FOREMAN_APP_KEY_PATH.

### FOREMAN_HUMAN_LOGIN is set and not blank, and each budget cap that is set parses. An unset cap uses its default.

**Why:** The human login's issue comments answer and stop runs, and the caps bound each run's work tokens and dollars.

**How:** Set FOREMAN_HUMAN_LOGIN in the repository's Actions variables. FOREMAN_MAX_TOKENS (a positive whole number) and FOREMAN_MAX_BUDGET_USD (a positive dollar amount) are optional.

### The FOREMAN_HUMAN_LOGIN account can write to the repository

**Why:** Only a login that can write to the repository answers or stops its runs.

**How:** Give the FOREMAN_HUMAN_LOGIN account write access to the repository.

### The Ready label exists

**Why:** Adding Ready to an issue starts its run.

**How:** Create the Ready label in the repository.

### A merge queue ruleset protects the base branch

**Why:** Foreman merges only through GitHub's merge queue, which enforces the repository's required checks.

**How:** Add a ruleset that targets the default branch with one merge queue rule and a merge method.

### The contract file parses strictly, so an unknown or duplicate key is named, and validates

**Why:** The contract file names the repository's check, verify skill, image, setup, secrets and app start, versioned with the code it checks.

**How:** Commit .foreman/contract.yaml to the default branch with check, verify_skill, test_report, image, setup, secrets, start, production_check and mutation.

### The verify skill exists at the contract's path

**Why:** Verify follows the repository's own skill to prove a change works.

**How:** Commit the skill as a regular file at the path the contract's verify_skill names.

## Full check

Doctor runs it from the command, and at admission whenever the contract file's digest has no green record. It builds the contract's container at the default branch's head.

### Each related repository checks out at its default-branch head, its service starts and its health command answers

**Why:** Workers and the app's checks reach each related repository's real service, started in the attempt container from a checkout of that repository.

**How:** Install the Foreman GitHub App on each repository related_repositories names, and make each one's start command start its service from a checkout of its default branch, with a health command that exits 0 once the service answers and timeout_seconds.

### The contract image builds

**Why:** Every check runs in a container from the contract's image, never on the Foreman host.

**How:** Name an image docker on the Foreman host can pull in the contract's image field.

### The setup commands succeed

**Why:** Each container runs the setup commands in its checkout, with the test value of each named secret, before any check.

**How:** List setup commands that succeed in a fresh checkout within thirty minutes, and set FOREMAN_TEST_<NAME> on the Foreman host for each secret the contract names.

### The check passes on the default branch and writes its test report

**Why:** Foreman proves each change with the repository's check, so the check passes before any change.

**How:** Make the contract's check pass on the default branch within check_timeout_seconds, writing each test_report path the contract names.

### The mutation command runs on the default branch

**Why:** Before commit, Foreman flips sampled changed lines with the repository's own mutation tool, so a change no test notices goes back to the builder.

**How:** Set mutation's command, which reads file:line entries on standard input and prints one JSON line per mutation with its line, id and killed or survived, and its sample_cap. It exits 0 for an empty list on the default branch.

### The app starts and its health command answers, when the contract names a start

**Why:** Verify drives the running app.

**How:** Set start's command, a health command that exits 0 once the app answers, and timeout_seconds. A repository with no app leaves start out.

### The production check command runs against the default branch head

**Why:** After each merge Foreman runs the read-only production check against the merge commit, and a failed check files a revert of Foreman's merge.

**How:** Set production_check's command, which reads the deployed commit from a source that needs no credentials and exits 0 when production runs the commit it is given and the check passes, its not_deployed_exit and its timeout_seconds. It runs on the Foreman host in the checkout, with no environment but PATH.

### The synthetic branch probe shows that a push to a PR's source branch while its arm is open drops the PR from the merge queue and that Foreman reads the moved head

**Why:** Foreman arms the reviewed head with no human approval step, so a branch that moves after the arm must never merge unseen. GitHub drops the pull request from the merge queue and Foreman reads the moved head, which goes back through its checks. Until the probe passes the repository is not admitted, so nothing is armed (merge_held).

**How:** Keep the merge queue ruleset on the default branch. Doctor proves it with a synthetic pull request that it arms, pushes to and closes, acting as the App.
