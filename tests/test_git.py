import subprocess
from pathlib import Path

from devdash.git import RepoError, RepoStatus, _parse_porcelain_v2, get_status

PORCELAIN = """\
# branch.oid 1234567890abcdef
# branch.head main
# branch.upstream origin/main
# branch.ab +2 -1
1 M. N... 100644 100644 100644 a b staged.py
1 .M N... 100644 100644 100644 a b modified.py
1 MM N... 100644 100644 100644 a b both.py
2 R. N... 100644 100644 100644 a b R100 new.py\told.py
u UU N... 100644 100644 100644 100644 a b c conflict.py
? untracked.txt
? another.txt
"""


def test_parse_porcelain_v2() -> None:
    s = _parse_porcelain_v2(PORCELAIN, "msg", None)
    assert (s.branch, s.head, s.upstream) == ("main", "1234567", "origin/main")
    assert (s.ahead, s.behind) == (2, 1)
    assert (s.staged, s.modified, s.conflicts, s.untracked) == (3, 2, 1, 2)
    assert s.dirty


def test_parse_detached_and_initial() -> None:
    s = _parse_porcelain_v2("# branch.oid (initial)\n# branch.head (detached)\n", None, None)
    assert s.branch is None and s.head is None and s.upstream is None
    assert not s.dirty


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   cwd=cwd, check=True, capture_output=True)


async def test_get_status_on_real_repos(tmp_path: Path) -> None:
    assert await get_status(tmp_path / "missing") == RepoError("Path does not exist")
    assert await get_status(tmp_path) == RepoError("Not a git repository")

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    empty = await get_status(repo)
    assert isinstance(empty, RepoStatus) and empty.last_commit_subject is None

    (repo / "a").write_text("a")
    _git(repo, "add", "a")
    _git(repo, "commit", "-qm", "first [commit]")
    (repo / "a").write_text("changed")
    (repo / "b").write_text("new")
    status = await get_status(repo)
    assert isinstance(status, RepoStatus)
    assert status.last_commit_subject == "first [commit]"
    assert (status.modified, status.untracked) == (1, 1)
