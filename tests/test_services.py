import asyncio
import os
import socket
from pathlib import Path

from devdash.config import Project, Service
from devdash.services import LogLine, ManagedService, ServiceManager, State, _clean, port_open


def make_manager(tmp_path: Path, *services: Service):
    project = Project("p", tmp_path, services)
    lines: list[LogLine] = []
    manager = ServiceManager((project,), lambda ms, line: lines.append(line), lambda ms: None)
    return manager, project, lines


async def wait_for_state(ms: ManagedService, *states: State, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while ms.state not in states:
            await asyncio.sleep(0.02)


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False


async def test_captures_stdout_stderr_and_exit_code(tmp_path: Path) -> None:
    manager, project, _ = make_manager(
        tmp_path, Service("svc", "echo out; echo err >&2; exit 3", tmp_path))
    ms = manager.get(project, project.services[0])
    await manager.start(ms)
    await wait_for_state(ms, State.EXITED)
    assert ms.exit_code == 3
    streams = {(line.stream, line.text) for line in ms.lines}
    assert ("out", "out") in streams and ("err", "err") in streams


async def test_stop_kills_whole_group_even_if_it_ignores_sigterm(tmp_path: Path) -> None:
    cmd = "trap '' TERM; (trap '' TERM; sleep 60) & sleep 60"
    manager, project, _ = make_manager(tmp_path, Service("svc", cmd, tmp_path))
    ms = manager.get(project, project.services[0])
    await manager.start(ms)
    pgid = ms.pid
    assert pgid and group_alive(pgid)
    await manager.stop(ms, timeout=0.5)
    assert ms.state == State.STOPPED
    assert not group_alive(pgid)


async def test_missing_cwd_fails_without_raising(tmp_path: Path) -> None:
    manager, project, _ = make_manager(tmp_path, Service("svc", "true", tmp_path / "nope"))
    ms = manager.get(project, project.services[0])
    await manager.start(ms)
    assert ms.state == State.FAILED


async def test_port_open() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        assert await port_open(port)
    assert not await port_open(port)


def test_clean_keeps_colour_drops_cursor_codes() -> None:
    assert _clean(b"\x1b[2J\x1b[H\x1b[32mok\x1b[0m\r\n") == "\x1b[32mok\x1b[0m"
    assert _clean(b"10%\r50%\r100%") == "100%"


async def test_update_projects_keeps_running_services(tmp_path: Path) -> None:
    old = Service("svc", "sleep 30", tmp_path)
    manager, project, _ = make_manager(tmp_path, old, Service("idle", "true", tmp_path))
    running = manager.get(project, old)
    await manager.start(running)

    # Rescan: the project lost both services and gained a new one.
    new_project = Project("p", tmp_path, (Service("fresh", "true", tmp_path),))
    manager.update_projects((new_project,))
    assert set(manager.services) == {("p", "svc"), ("p", "fresh")}  # idle dropped
    assert manager.services[("p", "svc")] is running and running.active
    assert [ms.service.name for ms in manager.for_project(new_project)] == ["fresh", "svc"]
    await manager.stop(running)
