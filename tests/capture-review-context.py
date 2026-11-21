"""Recapture review-context Git evidence using only synthetic temporary repos."""

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory


fixtures = Path(__file__).with_name("fixtures") / "git"
os.environ.update({
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_ATTR_NOSYSTEM": "1",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_ALLOW_PROTOCOL": "file",
})
print(subprocess.check_output(["git", "--version"], text=True).strip())
with TemporaryDirectory() as directory:
    root = Path(directory) / "checkout"
    root.mkdir()

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

    def commit():
        git("add", "--all")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic capture")

    def capture(name, *args, returncode=0):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True)
        assert result.returncode == returncode, result.stderr
        (fixtures / name).write_bytes(result.stderr if name.endswith(".stderr.txt") else result.stdout)
        print(name, "argv:", ["git", *args])
        print("returncode:", result.returncode, "stdout:", repr(result.stdout), "stderr:", repr(result.stderr))

    git("init", "-q", "--object-format=sha1", "-b", "main")
    for name, data in {"items.txt": b"first\nold\nlast", "empty": b"", "binary": b"\0binary\n",
                       "space : name.txt": b"one\ntwo\n", "old name.txt": b"rename\n",
                       "nested/file.txt": b"nested\n"}.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_bytes(data)
    commit()
    base = git("rev-parse", "HEAD").decode().strip()
    grep = ("--no-replace-objects", "--literal-pathspecs", "-c", "core.attributesFile=/dev/null", "--attr-source=HEAD",
            "grep", "-G", "--no-color", "-c", "-I", "-z", "--no-textconv", "-e", "^", "HEAD", "--")
    capture("grep-lines.z", *grep)

    def probe(*args, cwd=root):
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        return {"argv": ["git", *args], "returncode": result.returncode,
                "stdout": result.stdout, "stderr": result.stderr}

    plain_grep = ("grep", "-c", "-I", "-z", "--no-textconv", "-e", "^", "HEAD", "--")
    pattern_config = ("-c", "grep.patternType=fixed", "-c", "grep.extendedRegexp=true")
    fixed = probe(*pattern_config, *plain_grep)
    basic = probe(*pattern_config, *grep)
    extended = probe("-c", "grep.extendedRegexp=true", *grep)
    group_pattern = ("-c", "-I", "-z", "-e", r"\(first\)", "HEAD", "--")
    extended_group = probe("-c", "grep.extendedRegexp=true", "grep", *group_pattern)
    basic_group = probe("-c", "grep.extendedRegexp=true", "grep", "-G", *group_pattern)
    expected_lines = (fixtures / "grep-lines.z").read_bytes().decode()
    assert fixed["returncode"] == 1 and fixed["stdout"] == "", fixed
    for result in (basic, extended):
        assert result["returncode"] == 0 and result["stdout"] == expected_lines, result
    assert extended_group["returncode"] == 1 and extended_group["stdout"] == "", extended_group
    assert basic_group["returncode"] == 0 and basic_group["stdout"] == "HEAD:items.txt\u00001\n", basic_group
    (fixtures / "grep-basic-regexp.json").write_text(json.dumps({
        "fixed_without_G": fixed, "fixed_with_G": basic, "extended_with_G": extended,
        "extended_group_without_G": extended_group, "extended_group_with_G": basic_group,
    }, indent=2) + "\n")

    local_attributes = "items.txt -diff\nspace* -diff\n"
    user_attributes = "nested/file.txt -diff\n"
    (root / ".gitattributes").write_text(local_attributes)
    git("add", ".gitattributes")
    (root.parent / "local-attributes").write_text(user_attributes)
    user_config = ("-c", "core.attributesFile=../local-attributes")
    unpinned = probe(*user_config, "grep", "-G", *plain_grep[1:])
    tree_only = probe(*user_config, "--attr-source=HEAD", "grep", "-G", *plain_grep[1:])
    hardened = probe(*grep)
    assert unpinned["returncode"] == tree_only["returncode"] == hardened["returncode"] == 0
    assert unpinned["stdout"] == "HEAD:old name.txt\u00001\n", unpinned
    assert tree_only["stdout"] == expected_lines.replace("HEAD:nested/file.txt\u00001\n", ""), tree_only
    assert hardened["stdout"] == expected_lines, hardened
    (fixtures / "grep-attribute-source.json").write_text(json.dumps({
        "HEAD_attributes": None, "worktree_and_index_attributes": local_attributes,
        "user_attributes": user_attributes, "unpinned": unpinned,
        "tree_source_only": tree_only, "tree_source_and_no_user_file": hardened,
    }, indent=2) + "\n")
    git("reset", "--", ".gitattributes")
    (root / ".gitattributes").unlink()

    capture("ls-tree.z", "--no-replace-objects", "ls-tree", "-rz", "--name-only", "--full-tree", base, "--")
    capture("grep-fatal.stderr.txt", "-c", "grep.patternType=invalid", "grep", "-c", "-I", "-e", "^", "HEAD", "--",
            returncode=128)
    git("mv", "old name.txt", "new : name.txt")
    commit()
    capture("name-status-rename.z", "--no-replace-objects", "diff", "--name-status", "-z",
            "--find-renames", "--no-ext-diff", "--no-textconv", "--no-relative", "HEAD^", "HEAD", "--")
    head = git("rev-parse", "HEAD").decode().strip()
    capture("merge-base.txt", "--no-replace-objects", "merge-base", base, head)
    git("init", "-q", "--bare", "../origin.git")
    git("remote", "add", "origin", "../origin.git")
    git("push", "-q", "origin", "HEAD:refs/heads/main")
    receiver = Path(directory) / "receiver"
    receiver.mkdir()

    assert probe("init", "-q", "--object-format=sha1", "-b", "main", cwd=receiver)["returncode"] == 0
    assert probe("remote", "add", "origin", "../origin.git", cwd=receiver)["returncode"] == 0
    before = [probe("cat-file", "-t", sha, cwd=receiver) for sha in (base, head)]
    fetched = probe("fetch", "origin", base, head, cwd=receiver)
    after = [probe("cat-file", "-t", sha, cwd=receiver) for sha in (base, head)]
    assert all(result["returncode"] == 128 for result in before), before
    assert fetched["returncode"] == 0, fetched
    assert all(result["returncode"] == 0 and result["stdout"] == "commit\n" for result in after), after
    (fixtures / "fetch-by-sha.json").write_text(json.dumps({"before": before, "fetch": fetched, "after": after}, indent=2) + "\n")
    print("fetch-by-sha.json", fetched)
    for name in ("items.txt", "space : name.txt", "new : name.txt", "nested/file.txt"):
        (root / name).unlink()
    commit()
    capture("grep-no-text.z", *grep, returncode=1)
