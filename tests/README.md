# Skill export fixture

Git 2.56.0 ran the following commands against a temporary repository. The archive contained `example/SKILL.md` at its root, with no `pstack/skills/` prefix.

```sh
git -C "$source" init --quiet
git -C "$source" add -- pstack
git -C "$source" -c user.name=Foreman -c user.email=foreman@example.invalid -c commit.gpgSign=false commit --quiet -m 'Synthetic skill fixture'
pin=$(git -C "$source" rev-parse HEAD)
git -C "$fetch" init --quiet
git -C "$fetch" fetch --quiet --no-tags --depth=1 -- "$source" "$pin"
git -C "$fetch" archive --format=tar --output="$archive" FETCH_HEAD:pstack/skills
```

`git ls-remote https://github.com/cursor/plugins HEAD` returned
`d42aff08aa58965d2735c4ead61a9af82b89d0d0`. The same fetch and archive
commands ran successfully against that pin.

Unit tests and evals create synthetic Git repositories with `skill_fixture.py`.
Use a local checkout containing the pinned commit to inspect the real method
text without fetching from the network during tests.

```sh
FOREMAN_PINNED_SKILL_REPO=/path/to/cursor/plugins \
  python -m unittest discover -s tests -p test_skill_rules.py
```
