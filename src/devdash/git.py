"""Async git queries. Nothing here blocks the event loop or raises on git failure:
every query returns either a RepoStatus or a RepoError the UI can display."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GIT_TIMEOUT = 10.0
FETCH_TIMEOUT = 60.0

# Never prompt for credentials, never take index.lock while polling, stable output.
_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}


@dataclass(frozen=True)
class RepoStatus:
    branch: str | None  # None when HEAD is detached
    head: str | None  # short commit id; None before the first commit
    upstream: str | None
    ahead: int
    behind: int
    staged: int
    modified: int
    untracked: int
    conflicts: int
    last_commit_subject: str | None
    last_commit_time: datetime | None

    @property
    def changed(self) -> int:
        return self.staged + self.modified + self.conflicts

    @property
    def dirty(self) -> bool:
        return bool(self.changed or self.untracked)


@dataclass(frozen=True)
class RepoError:
    message: str


GitResult = RepoStatus | RepoError


@dataclass(frozen=True)
class _Run:
    returncode: int
    stdout: str
    stderr: str

    @property
    def error_line(self) -> str:
        lines = (self.stderr or self.stdout).strip().splitlines()
        return lines[0].removeprefix("fatal: ") if lines else f"git exited with code {self.returncode}"


class _GitUnavailable(Exception):
    pass


async def _git(path: Path, *args: str, timeout: float = GIT_TIMEOUT) -> _Run:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=path,
            env=_GIT_ENV,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise _GitUnavailable("git is not installed") from None
    except OSError as e:
        raise _GitUnavailable(f"can't run git: {e.strerror}") from None
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return _Run(proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace"))


def _check_path(path: Path) -> RepoError | None:
    if not path.exists():
        return RepoError("Path does not exist")
    if not path.is_dir():
        return RepoError("Path is not a directory")
    return None


async def get_status(path: Path) -> GitResult:
    if error := _check_path(path):
        return error
    try:
        status, log = await asyncio.gather(
            _git(path, "status", "--porcelain=v2", "--branch"),
            _git(path, "log", "-1", "--format=%ct%x1f%s"),
        )
    except _GitUnavailable as e:
        return RepoError(str(e))
    except TimeoutError:
        return RepoError("git timed out")

    if status.returncode != 0:
        if "not a git repository" in status.stderr:
            return RepoError("Not a git repository")
        return RepoError(status.error_line)

    subject, when = None, None
    if log.returncode == 0 and "\x1f" in log.stdout:  # fails harmlessly on a repo with no commits
        ts, _, subject = log.stdout.strip().partition("\x1f")
        try:
            when = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        except ValueError:
            pass

    return _parse_porcelain_v2(status.stdout, subject, when)


def _parse_porcelain_v2(out: str, subject: str | None, when: datetime | None) -> RepoStatus:
    branch = head = upstream = None
    ahead = behind = staged = modified = untracked = conflicts = 0
    for line in out.splitlines():
        if line.startswith("# branch.oid "):
            oid = line.removeprefix("# branch.oid ")
            head = None if oid == "(initial)" else oid[:7]
        elif line.startswith("# branch.head "):
            name = line.removeprefix("# branch.head ")
            branch = None if name == "(detached)" else name
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            a, _, b = line.removeprefix("# branch.ab ").partition(" ")
            ahead, behind = abs(int(a)), abs(int(b))
        elif line.startswith(("1 ", "2 ")):
            xy = line[2:4]
            staged += xy[0] != "."
            modified += xy[1] != "."
        elif line.startswith("u "):
            conflicts += 1
        elif line.startswith("? "):
            untracked += 1
    return RepoStatus(branch, head, upstream, ahead, behind, staged, modified, untracked,
                      conflicts, subject, when)


async def fetch(path: Path) -> RepoError | None:
    """`git fetch` so ahead/behind reflects the remote. Returns an error or None."""
    if error := _check_path(path):
        return error
    try:
        run = await _git(path, "fetch", "--quiet", timeout=FETCH_TIMEOUT)
    except _GitUnavailable as e:
        return RepoError(str(e))
    except TimeoutError:
        return RepoError("git fetch timed out")
    return RepoError(run.error_line) if run.returncode != 0 else None
