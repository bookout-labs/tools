# Set up factory App commands

Use the same invocation in Codex and Claude shells. From an installed checkout, run
`.venv/bin/python -m foreman.worker_auth gh pr list --repo bookout-labs/tools`.
On the MacBook add `--studio foreman-studio` before `gh` or `git`.
Only `bookout-labs/tools` is allowed. Account-bound MCP GitHub writes are excluded.
Every invocation requests a current credential. There is no worker token file or personal fallback.

Git network syntax is `git fetch origin main`, `git push origin HEAD:BRANCH`,
`git ls-remote origin`, or `git clone https://github.com/bookout-labs/tools DIRECTORY`.
Network options are deliberately excluded. For an existing checkout, first set its
origin and every push URL to `https://github.com/bookout-labs/tools` using Joe's shell.
Local status, diff, log, show, add, commit, switch, branch, rev-parse, merge and cherry-pick are supported.
Output is captured and redacted, so commands are noninteractive with a 30-second deadline.
After an ambiguous write failure inspect GitHub receipts before retrying.

## One-time machine changes for Joe

These steps require changes outside the worker worktree. This slice does not perform them.

1. On Studio, install this revision and its dependencies at
   `/Users/booko/.local/share/foreman-worker` with a `.venv` there.
   Run these commands on Studio, and repeat them on MacBook at step 4.
   After merge, deploy the approved main revision instead of the slice branch.

   ```sh
   git clone --branch 129c-worker-launcher https://github.com/bookout-labs/tools /Users/booko/.local/share/foreman-worker
   python3.12 -m venv /Users/booko/.local/share/foreman-worker/.venv
   /Users/booko/.local/share/foreman-worker/.venv/bin/python -m pip install /Users/booko/.local/share/foreman-worker
   ```

   Keep that checkout and its parent directories writable only by Joe.
   Keep `~/.config/bookout-foreman` mode 700 and `app.pem` mode 600.
   The App key stays on Studio. No token is copied, saved, or printed.
2. On MacBook, create a dedicated SSH identity with
   `ssh-keygen -t ed25519 -f ~/.ssh/foreman-worker`. Keep its private key on MacBook.
   Make it available to the SSH agent for BatchMode use.
   Transfer only the public `.pub` line to Studio through the normal authenticated machine channel.
3. On Studio, enable Remote Login for Joe if needed. Append this single line to
   Joe's `~/.ssh/authorized_keys`, replacing `PUBLIC_KEY` with that public key line.
   Keep `.ssh` mode 700 and `authorized_keys` mode 600.

   ```text
   restrict,command="cd /Users/booko/.local/share/foreman-worker && FOREMAN_APP_ID=5861795 FOREMAN_INSTALLATION_ID=184333419 .venv/bin/python -m foreman.worker_auth --serve" PUBLIC_KEY
   ```

   `restrict` disables forwarding, PTY, agent forwarding, X11 and user rc execution.
   The forced command accepts exactly `bookout-labs/tools` in `SSH_ORIGINAL_COMMAND`.
   Do not test this endpoint by running raw ssh in a captured terminal. Its stdout is the credential wire.
4. On MacBook, install the same revision with dependencies in a local checkout.
   Add the following stanza to `~/.ssh/config`. Replace `STUDIO_HOST` with the
   authenticated Studio address. Confirm Studio's host-key fingerprint through
   an existing trusted channel and provision that key in `known_hosts` first.

   ```sshconfig
   Host foreman-studio
       HostName STUDIO_HOST
       User booko
       IdentityFile ~/.ssh/foreman-worker
       IdentitiesOnly yes
       BatchMode yes
       StrictHostKeyChecking yes
       RequestTTY no
       ForwardAgent no
       ClearAllForwardings yes
   ```

5. For local Studio workers, set `FOREMAN_APP_ID=5861795` and
   `FOREMAN_INSTALLATION_ID=184333419` in each worker's launcher environment.
   Leave App configuration and the key absent on MacBook.
6. Run the launcher from both Codex and Claude on each machine. Push a disposable
   branch and open a PR as the bot, then repeat after more than one hour.
   Record only actor, PR URL, head SHA and timestamps. Do not record credentials.
   This rollout proof requires the machine setup above. Unit tests use a fake clock.

## Shared brief text for the coordinator

Factory GitHub commands use `.venv/bin/python -m foreman.worker_auth gh ...` or
`.venv/bin/python -m foreman.worker_auth git ...` from the installed tools checkout.
The only allowed repo is `bookout-labs/tools`. On MacBook insert
`--studio foreman-studio` before `gh` or `git`. Use the same launcher for Codex
and Claude. Do not use account-bound GitHub MCP write tools. Never fetch or
print credentials yourself. See `docs/worker-launcher.md` for supported git syntax.
