"""Recapture Git's NUL record layouts using only a synthetic temporary repo."""

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory


fixtures = Path(__file__).resolve().parents[1] / "tests/fixtures/git"
print(subprocess.check_output(["git", "--version"], text=True).strip())
with TemporaryDirectory() as directory:
    root = Path(directory)

    def git(*args):
        return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True).stdout

    def commit():
        git("add", "--all")
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid",
            "-c", "commit.gpgsign=false", "commit", "-qm", "Synthetic capture")

    def capture(name, *args, returncode=0):
        result = subprocess.run(["git", *args], cwd=root, capture_output=True)
        assert result.returncode == returncode, result.stderr
        assert result.stderr == b"", result.stderr
        (fixtures / name).write_bytes(result.stdout)
        print(name, "argv:", ["git", *args])
        print("returncode:", result.returncode, "stdout:", repr(result.stdout))

    git("init", "-q")
    for name, data in {"items.txt": b"first\nold\nlast", "empty": b"", "binary": b"\0binary\n",
                       "space : name.txt": b"one\ntwo\n", "old name.txt": b"rename\n"}.items():
        (root / name).write_bytes(data)
    commit()
    capture("grep-lines.z", "--no-replace-objects", "grep", "-c", "-I", "-z",
            "--no-textconv", "-e", "^", "HEAD", "--")
    git("mv", "old name.txt", "new : name.txt")
    commit()
    capture("name-status-rename.z", "--no-replace-objects", "diff", "--name-status", "-z",
            "--find-renames", "--no-ext-diff", "--no-textconv", "--no-relative", "HEAD^", "HEAD", "--")
    for name in ("items.txt", "space : name.txt", "new : name.txt"):
        (root / name).unlink()
    commit()
    capture("grep-no-text.z", "--no-replace-objects", "grep", "-c", "-I", "-z",
            "--no-textconv", "-e", "^", "HEAD", "--", returncode=1)
