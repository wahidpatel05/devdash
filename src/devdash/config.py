"""Load and validate ~/.config/devdash/config.toml.

Fatal problems (missing file, invalid TOML) raise ConfigError. Problems with an
individual project or service only skip that entry and are reported back as
warnings, so one typo doesn't take down the whole dashboard.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

DEFAULT_REFRESH_INTERVAL = 3.0
MIN_REFRESH_INTERVAL = 0.5
DEFAULT_THEME = "omarchy"
DEFAULT_LOG_LINES = 5000


class ConfigError(Exception):
    """The config could not be loaded at all."""


@dataclass(frozen=True)
class Service:
    name: str
    command: str
    cwd: Path
    port: int | None = None


@dataclass(frozen=True)
class Project:
    name: str
    path: Path
    services: tuple[Service, ...] = ()


@dataclass(frozen=True)
class Config:
    path: Path
    projects: tuple[Project, ...]
    refresh_interval: float = DEFAULT_REFRESH_INTERVAL
    theme: str = DEFAULT_THEME
    log_lines: int = DEFAULT_LOG_LINES
    warnings: tuple[str, ...] = ()

    @property
    def user_css_path(self) -> Path:
        return self.path.parent / "devdash.tcss"


def default_config_path() -> Path:
    if env := os.environ.get("DEVDASH_CONFIG"):
        return Path(env).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "devdash" / "config.toml"


def example_config_text() -> str:
    return resources.files("devdash").joinpath("example_config.toml").read_text()


def write_example_config(path: Path) -> None:
    if path.exists():
        raise ConfigError(f"{path} already exists; not overwriting it.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(example_config_text())


def load_config(path: Path) -> Config:
    try:
        text = path.read_text()
    except FileNotFoundError:
        raise ConfigError(
            f"No config found at {path}\n\n"
            "Create one with:  devdash --init-config"
        ) from None
    except (OSError, UnicodeDecodeError) as e:
        raise ConfigError(f"Can't read {path}: {e}") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}:\n{e}") from None
    return parse_config(data, path)


def parse_config(data: dict[str, Any], path: Path) -> Config:
    warnings: list[str] = []

    interval = data.get("refresh_interval", DEFAULT_REFRESH_INTERVAL)
    if isinstance(interval, bool) or not isinstance(interval, int | float):
        warnings.append(f"refresh_interval must be a number; using {DEFAULT_REFRESH_INTERVAL}s")
        interval = DEFAULT_REFRESH_INTERVAL
    elif interval < MIN_REFRESH_INTERVAL:
        warnings.append(f"refresh_interval too small; using {MIN_REFRESH_INTERVAL}s")
        interval = MIN_REFRESH_INTERVAL

    theme = data.get("theme", DEFAULT_THEME)
    if not isinstance(theme, str) or not theme.strip():
        warnings.append(f"theme must be a string; using {DEFAULT_THEME!r}")
        theme = DEFAULT_THEME

    log_lines = data.get("log_lines", DEFAULT_LOG_LINES)
    if isinstance(log_lines, bool) or not isinstance(log_lines, int) or log_lines < 100:
        warnings.append(f"log_lines must be an integer >= 100; using {DEFAULT_LOG_LINES}")
        log_lines = DEFAULT_LOG_LINES

    raw_scan = data.get("scan_dirs", [])
    if isinstance(raw_scan, str):
        raw_scan = [raw_scan]
    if not isinstance(raw_scan, list) or not all(isinstance(d, str) for d in raw_scan):
        warnings.append("scan_dirs must be a list of paths; ignoring it")
        raw_scan = []
    scan_dirs = [_expand(d) for d in raw_scan if d.strip()]

    raw_projects = data.get("projects", [])
    if not isinstance(raw_projects, list):
        warnings.append("`projects` must be an array of tables ([[projects]])")
        raw_projects = []

    projects: list[Project] = []
    seen: set[str] = set()
    for i, raw in enumerate(raw_projects, start=1):
        project = _parse_project(raw, i, warnings)
        if project is None:
            continue
        if project.name in seen:
            warnings.append(f"Duplicate project name {project.name!r}; skipping the later one")
            continue
        seen.add(project.name)
        projects.append(project)

    if scan_dirs:
        from devdash.discover import scan  # imports this module; keep it lazy

        taken = {_resolve(p.path) for p in projects}
        projects.extend(scan(scan_dirs, taken, seen, warnings))

    if not projects:
        warnings.append(f"No projects found: add [[projects]] or scan_dirs to {path}")

    return Config(path, tuple(projects), float(interval), theme.strip(), log_lines, tuple(warnings))


def _expand(value: str) -> Path:
    return Path(os.path.expandvars(value)).expanduser()


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path


def _parse_project(raw: Any, index: int, warnings: list[str]) -> Project | None:
    where = f"projects[{index}]"
    if not isinstance(raw, dict):
        warnings.append(f"{where} is not a table; skipping")
        return None
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        warnings.append(f"{where} has no `name`; skipping")
        return None
    where = f"project {name!r}"
    raw_path = raw.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        warnings.append(f"{where} has no `path`; skipping")
        return None
    path = _expand(raw_path)

    raw_services = raw.get("services", [])
    if not isinstance(raw_services, list):
        warnings.append(f"{where}: `services` must be an array of tables; ignoring")
        raw_services = []
    services: list[Service] = []
    for j, raw_service in enumerate(raw_services, start=1):
        service = _parse_service(raw_service, path, f"{where} services[{j}]", warnings)
        if service is None:
            continue
        if any(s.name == service.name for s in services):
            warnings.append(f"{where}: duplicate service {service.name!r}; skipping")
            continue
        services.append(service)

    return Project(name.strip(), path, tuple(services))


def _parse_service(raw: Any, project_path: Path, where: str, warnings: list[str]) -> Service | None:
    if not isinstance(raw, dict):
        warnings.append(f"{where} is not a table; skipping")
        return None
    name, command = raw.get("name"), raw.get("command")
    if not isinstance(name, str) or not name.strip():
        warnings.append(f"{where} has no `name`; skipping")
        return None
    if not isinstance(command, str) or not command.strip():
        warnings.append(f"{where} ({name}) has no `command`; skipping")
        return None

    port = raw.get("port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535):
        warnings.append(f"{where} ({name}): invalid port {port!r}; ignoring it")
        port = None

    cwd = project_path
    raw_cwd = raw.get("cwd")
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        cwd = project_path / _expand(raw_cwd)  # absolute cwd replaces project_path
    elif raw_cwd is not None:
        warnings.append(f"{where} ({name}): `cwd` must be a string; using project path")

    return Service(name.strip(), command.strip(), cwd, port)
