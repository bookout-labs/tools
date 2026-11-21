# Review fixtures

`review.diff` was captured with Git 2.56.0 in a synthetic repository. The base
had a three-line `items.txt` and a one-line `deleted.txt`. The head replaced
the middle line of `items.txt` and removed `deleted.txt`.

```sh
git diff --no-color --no-ext-diff --no-textconv --no-relative --src-prefix=a/ --dst-prefix=b/ --find-renames HEAD^...HEAD --
```

LiveForge uses gh 2.102.0. The [`gh pr view` manual](https://cli.github.com/manual/gh_pr_view)
lists `baseRefOid` and `headRefOid` among its JSON fields. The
[`gh api` manual](https://cli.github.com/manual/gh_api) documents `--input -`
for sending a request body from stdin.

The [GitHub REST API 2022-11-28 review endpoint](https://docs.github.com/en/rest/pulls/reviews?apiVersion=2022-11-28#create-a-review-for-a-pull-request)
documents `POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews`. Foreman
sends `commit_id`, `event: COMMENT`, `body`, and a `comments` array
with `path`, `line`, `side`, and `body` per inline comment. The
[`RIGHT` side identifies the new file](https://docs.github.com/en/rest/pulls/comments?apiVersion=2022-11-28#create-a-review-comment-for-a-pull-request).
