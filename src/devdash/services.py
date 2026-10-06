"""Start, stop and watch dev-server processes.

Each service runs in its own session/process group, so stopping it signals the
whole tree (npm -> node -> esbuild, ...) and not just the shell. Only processes
started by this manager are ever signalled.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import signal
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from devdash.config import Project, Service, short_path

STOP_TIMEOUT = 5.0
SHELL = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
# If devdash itself dies hard (kill -9, OOM), the kernel SIGTERMs the service.
_PDEATHSIG = ["setpriv", "--pdeathsig", "TERM"] if shutil.which("setpriv") else []

# Keep colour (SGR) escapes so Rich can render them; drop everything else
# (cursor movement, clear-screen, OSC titles) that would garble a log view.
_NON_SGR = re.compile(r"\x1b\[[0-?]*[ -/]*[@-ln-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_SGR = re.compile(r"\x1b\[[0-?]*m")


class State(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED = "exited"
    FAILED = "failed"


ACTIVE_STATES = (State.STARTING, State.RUNNING, State.STOPPING)


@dataclass(frozen=True)
class LogLine:
    text: str  # may contain SGR colour codes
    stream: Literal["out", "err", "sys"]

    @property
    def plain(self) -> str:
        return _SGR.sub("", self.text)


@dataclass(eq=False)
class ManagedService:
    project: Project
    service: Service
    lines: deque[LogLine]
    state: State = State.STOPPED
    exit_code: int | None = None
    pid: int | None = None
    started_at: float | None = None
    _proc: asyncio.subprocess.Process | None = field(default=None, repr=False)
    _exited: asyncio.Event | None = field(default=None, repr=False)

    @property
    def key(self) -> tuple[str, str]:
        return (self.project.name, self.service.name)

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def uptime(self) -> float | None:
        return time.monotonic() - self.started_at if self.started_at and self.active else None


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal a whole process group. Returns False if the group is gone."""
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pgid was reused by someone else's process: leave it alone
        return False


def _clean(raw: bytes) -> str:
    text = raw.decode(errors="replace").rstrip("\r\n")
    text = text.rsplit("\r", 1)[-1]  # progress bars: keep the final redraw
    return _NON_SGR.sub("", text).expandtabs(4)


class ServiceManager:
    def __init__(
        self,
        projects: tuple[Project, ...],
        on_line: Callable[[ManagedService, LogLine], None],
        on_state: Callable[[ManagedService], None],
        max_lines: int = 5000,
    ) -> None:
        self._on_line, self._on_state = on_line, on_state
        self._max_lines = max_lines
        self.services: dict[tuple[str, str], ManagedService] = {}
        self._tasks: set[asyncio.Task] = set()
        self.update_projects(projects)

    def update_projects(self, projects: tuple[Project, ...]) -> None:
        """Sync with a new project list (after a rescan). Running services are never
        touched: they keep their process, logs and (old) definition until stopped.
        Idle ones pick up the new definition; removed idle ones are dropped."""
        wanted = {(p.name, s.name): (p, s) for p in projects for s in p.services}
        for key, ms in list(self.services.items()):
            if key not in wanted and not ms.active:
                del self.services[key]
        for key, (project, service) in wanted.items():
            ms = self.services.get(key)
            if ms is None:
                self.services[key] = ManagedService(project, service, deque(maxlen=self._max_lines))
            elif not ms.active:
                ms.project, ms.service = project, service

    def get(self, project: Project, service: Service) -> ManagedService:
        return self.services[(project.name, service.name)]

    def for_project(self, project: Project) -> list[ManagedService]:
        """The project's services, plus any still-running ones a rescan removed."""
        listed = [self.get(project, s) for s in project.services]
        orphans = [ms for key, ms in self.services.items()
                   if key[0] == project.name and ms not in listed]
        return listed + orphans

    def running(self, project: Project | None = None) -> list[ManagedService]:
        return [m for m in self.services.values()
                if m.active and (project is None or m.project.name == project.name)]

    # --- events ----------------------------------------------------------

    def _log(self, ms: ManagedService, text: str, stream: Literal["out", "err", "sys"]) -> None:
        line = LogLine(text, stream)
        ms.lines.append(line)
        self._on_line(ms, line)

    def _set_state(self, ms: ManagedService, state: State) -> None:
        ms.state = state
        self._on_state(ms)

    # --- lifecycle -------------------------------------------------------

    async def start(self, ms: ManagedService) -> None:
        if ms.active:
            return
        svc = ms.service
        self._log(ms, f"── starting: {svc.command}  (in {short_path(svc.cwd)})", "sys")
        if not svc.cwd.is_dir():
            ms.exit_code = None
            self._log(ms, f"── failed: working directory does not exist: {svc.cwd}", "sys")
            self._set_state(ms, State.FAILED)
            return

        self._set_state(ms, State.STARTING)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        try:
            proc = await asyncio.create_subprocess_exec(
                *_PDEATHSIG, SHELL, "-c", svc.command,
                cwd=svc.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,  # own process group; detached from our terminal
            )
        except OSError as e:
            self._log(ms, f"── failed to start: {e}", "sys")
            self._set_state(ms, State.FAILED)
            return

        ms._proc, ms.pid, ms.exit_code = proc, proc.pid, None
        ms.started_at = time.monotonic()
        ms._exited = asyncio.Event()
        self._log(ms, f"── pid {proc.pid}", "sys")
        self._set_state(ms, State.RUNNING)
        task = asyncio.create_task(self._supervise(ms, proc, ms._exited))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _pump(self, ms: ManagedService, stream: asyncio.StreamReader, kind: Literal["out", "err"]) -> None:
        buf = b""
        while chunk := await stream.read(65536):
            buf += chunk
            *complete, buf = buf.split(b"\n")
            for raw in complete:
                self._log(ms, _clean(raw), kind)
            if len(buf) > 65536:  # a huge line with no newline: flush it anyway
                self._log(ms, _clean(buf), kind)
                buf = b""
        if buf:
            self._log(ms, _clean(buf), kind)

    async def _supervise(self, ms: ManagedService, proc: asyncio.subprocess.Process, exited: asyncio.Event) -> None:
        assert proc.stdout and proc.stderr
        pumps = asyncio.gather(self._pump(ms, proc.stdout, "out"), self._pump(ms, proc.stderr, "err"))
        code = await proc.wait()
        try:  # let buffered output drain; a background grandchild may hold the pipe open
            await asyncio.wait_for(asyncio.shield(pumps), 1.0)
        except TimeoutError:
            pass
        except Exception as e:
            self._log(ms, f"── log reader error: {e}", "sys")

        ms.exit_code = code
        stopping = ms.state == State.STOPPING
        if code < 0:
            reason = f"killed by {signal.Signals(-code).name}" if -code in signal.valid_signals() else f"signal {-code}"
        else:
            reason = f"exit code {code}"
        self._log(ms, f"── {'stopped' if stopping else 'exited'} ({reason})", "sys")
        ms._proc = None
        self._set_state(ms, State.STOPPED if stopping else State.EXITED)
        exited.set()

    async def stop(self, ms: ManagedService, timeout: float = STOP_TIMEOUT) -> None:
        proc, exited = ms._proc, ms._exited
        if proc is None or exited is None or ms.state == State.STOPPING:
            return
        pgid = proc.pid
        self._set_state(ms, State.STOPPING)
        self._log(ms, "── stopping (SIGTERM)", "sys")
        _signal_group(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(exited.wait(), timeout)
        except TimeoutError:
            self._log(ms, f"── still running after {timeout:g}s, sending SIGKILL", "sys")
            _signal_group(pgid, signal.SIGKILL)
            await exited.wait()
        # The shell is gone; make sure nothing else in its group outlived it.
        await self._reap_group(pgid, timeout=1.0)

    async def _reap_group(self, pgid: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while _signal_group(pgid, 0):
            if time.monotonic() > deadline:
                _signal_group(pgid, signal.SIGKILL)
                return
            await asyncio.sleep(0.1)

    async def restart(self, ms: ManagedService) -> None:
        await self.stop(ms)
        await self.start(ms)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(ms) for ms in self.running()), return_exceptions=True)

    def kill_all_sync(self, timeout: float = 3.0) -> None:
        """Last-resort cleanup once the event loop is gone (crash, SIGHUP, ...)."""
        # Every group we ever started, even ones whose leader exited: stray children count too.
        groups = [ms.pid for ms in self.services.values() if ms.pid]
        alive = [g for g in groups if _signal_group(g, signal.SIGTERM)]
        deadline = time.monotonic() + timeout
        while alive and time.monotonic() < deadline:
            time.sleep(0.1)
            alive = [g for g in alive if _signal_group(g, 0)]
        for g in alive:
            _signal_group(g, signal.SIGKILL)


async def port_open(port: int, timeout: float = 0.4) -> bool:
    """True if something accepts TCP connections on localhost:port (IPv4 or IPv6)."""

    async def probe(host: str) -> bool:
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        except (OSError, TimeoutError):
            return False
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass
        return True

    return any(await asyncio.gather(probe("127.0.0.1"), probe("::1")))
