import json
from pathlib import Path

from devdash.config import parse_config
from devdash.discover import detect_services, guess_port


def make_repo(root: Path, name: str, *, git_file: bool = False) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    if git_file:  # worktrees have a .git file instead of a directory
        (repo / ".git").write_text("gitdir: /elsewhere")
    else:
        (repo / ".git").mkdir()
    return repo


def package(directory: Path, dev: str | None, **files: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    scripts = {"dev": dev} if dev else {"build": "x"}
    (directory / "package.json").write_text(json.dumps({"scripts": scripts}))
    for name, content in files.items():
        (directory / name.replace("_", ".")).write_text(content)


def test_guess_port() -> None:
    assert guess_port("vite") == 5173
    assert guess_port("next dev") == 3000
    assert guess_port("vite --port 8080") == 8080
    assert guess_port("NODE_ENV=dev astro dev") == 4321
    assert guess_port("node server.js") is None


def test_root_dev_script_wins(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "app")
    package(repo, "next dev")
    package(repo / "web", "vite")
    (repo / "pnpm-lock.yaml").write_text("")
    assert [(s.name, s.command, s.port) for s in detect_services(repo)] == [("dev", "pnpm dev", 3000)]


def test_subfolder_and_monorepo_services(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, "app")
    package(repo / "frontend", "vite")
    package(repo / "backend", "node --watch server.js",
            server_js="const PORT = process.env.PORT || 5000;\napp.listen(PORT)")
    package(repo / "apps" / "web", "vite --port 3001")
    package(repo / "node_modules" / "junk", "vite")
    package(repo / "docs", None)
    services = {s.name: (s.cwd.relative_to(repo).as_posix(), s.port) for s in detect_services(repo)}
    assert services == {"backend": ("backend", 5000), "frontend": ("frontend", 5173),
                        "web": ("apps/web", 3001)}


def test_scan_dirs(tmp_path: Path) -> None:
    projects_dir = tmp_path / "Projects"
    package(make_repo(projects_dir, "site"), "vite")
    make_repo(projects_dir, "worktree", git_file=True)
    (projects_dir / "not-a-repo").mkdir()
    explicit = make_repo(projects_dir, "mine")

    config = parse_config({
        "scan_dirs": [str(projects_dir), str(tmp_path / "missing")],
        "projects": [{"name": "custom", "path": str(explicit),
                      "services": [{"name": "x", "command": "make run"}]}],
    }, tmp_path / "c.toml")

    assert [p.name for p in config.projects] == ["custom", "site", "worktree"]
    assert config.projects[0].services[0].command == "make run"  # explicit entry not duplicated
    assert config.projects[1].services[0].port == 5173
    assert config.projects[2].services == ()
    assert any("missing" in w for w in config.warnings)
