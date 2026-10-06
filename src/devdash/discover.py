"""Find git repos under `scan_dirs` and guess their dev servers from package.json.

The guesses are deliberately conservative: only a `dev` script counts as a
service. Anything smarter belongs in an explicit [[projects]] entry, which
always overrides a scanned one.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from devdash.config import Project, Service

MAX_SERVICES = 6
SKIP_DIRS = {"node_modules", "dist", "build", "out", "target", "vendor", "venv"}
MONOREPO_DIRS = ("apps", "packages", "services")

# Default dev-server ports, by the tool the script starts with.
DEFAULT_PORTS = {"vite": 5173, "next": 3000, "nuxt": 3000, "nuxi": 3000, "astro": 4321,
                 "remix": 3000, "react-scripts": 3000, "ng": 4200, "svelte-kit": 5173}
_PORT_FLAG = re.compile(r"(?:--port[= ]|-p |\bPORT=)(\d{2,5})\b")
# `const PORT = process.env.PORT || 5000` / `app.listen(4000)` in a node entry file
_SOURCE_PORT = re.compile(r"PORT\s*(?:\|\||\?\?)\s*(\d{2,5})\b|\.listen\(\s*(\d{2,5})\b")


def find_repos(scan_dir: Path) -> list[Path]:
    """Immediate children of scan_dir that are git repos (or worktrees)."""
    repos = []
    for child in scan_dir.iterdir():
        if child.name.startswith(".") or not child.is_dir():
            continue
        if (child / ".git").exists():
            repos.append(child)
    return sorted(repos, key=lambda p: p.name.lower())


def _dev_script(package_dir: Path) -> str | None:
    try:
        data = json.loads((package_dir / "package.json").read_text())
    except (OSError, ValueError):
        return None
    scripts = data.get("scripts") if isinstance(data, dict) else None
    dev = scripts.get("dev") if isinstance(scripts, dict) else None
    return dev if isinstance(dev, str) and dev.strip() else None


def _runner(package_dir: Path, repo: Path) -> str:
    for directory in (package_dir, repo):
        if (directory / "pnpm-lock.yaml").exists():
            return "pnpm dev"
        if (directory / "yarn.lock").exists():
            return "yarn dev"
        if (directory / "bun.lock").exists() or (directory / "bun.lockb").exists():
            return "bun run dev"
    return "npm run dev"


def _valid(port: int) -> int | None:
    return port if 1 <= port <= 65535 else None


def guess_port(script: str, package_dir: Path | None = None) -> int | None:
    if match := _PORT_FLAG.search(script):
        return _valid(int(match.group(1)))
    words = [w for w in script.split() if "=" not in w]  # skip env assignments like NODE_ENV=dev
    if words and words[0] in DEFAULT_PORTS:
        return DEFAULT_PORTS[words[0]]
    if package_dir is not None:  # node/nodemon/tsx server.js: look inside the entry file
        for word in words[1:]:
            if not word.endswith((".js", ".mjs", ".cjs", ".ts")):
                continue
            try:
                source = (package_dir / word).read_text(errors="replace")[:200_000]
            except OSError:
                continue
            if match := _SOURCE_PORT.search(source):
                return _valid(int(match.group(1) or match.group(2)))
    return None


def _candidate_dirs(repo: Path) -> list[Path]:
    dirs = []
    for child in sorted(repo.iterdir()):
        if not child.is_dir() or child.name.startswith(".") or child.name in SKIP_DIRS:
            continue
        if child.name in MONOREPO_DIRS:
            dirs.extend(sorted(d for d in child.iterdir() if d.is_dir() and not d.name.startswith(".")))
        else:
            dirs.append(child)
    return dirs


def detect_services(repo: Path) -> tuple[Service, ...]:
    """A root `dev` script wins (monorepo roots usually start everything);
    otherwise one service per sub-package that has its own `dev` script."""
    if script := _dev_script(repo):
        return (Service("dev", _runner(repo, repo), repo, guess_port(script, repo)),)

    services: list[Service] = []
    names: set[str] = set()
    for directory in _candidate_dirs(repo):
        script = _dev_script(directory)
        if not script:
            continue
        name = directory.name if directory.name not in names else str(directory.relative_to(repo))
        names.add(name)
        services.append(Service(name, _runner(directory, repo), directory, guess_port(script, directory)))
        if len(services) == MAX_SERVICES:
            break
    return tuple(services)


def scan(scan_dirs: list[Path], taken_paths: set[Path], taken_names: set[str],
         warnings: list[str]) -> list[Project]:
    projects: list[Project] = []
    names = set(taken_names)
    for scan_dir in scan_dirs:
        try:
            repos = find_repos(scan_dir)
        except FileNotFoundError:
            warnings.append(f"scan_dirs: {scan_dir} does not exist")
            continue
        except OSError as e:
            warnings.append(f"scan_dirs: can't read {scan_dir}: {e.strerror}")
            continue
        for repo in repos:
            if repo.resolve() in taken_paths:
                continue  # an explicit [[projects]] entry already covers it
            name = repo.name if repo.name not in names else f"{scan_dir.name}/{repo.name}"
            if name in names:
                continue
            try:
                services = detect_services(repo)
            except OSError:
                services = ()
            names.add(name)
            taken_paths.add(repo.resolve())
            projects.append(Project(name, repo, services))
    return projects
