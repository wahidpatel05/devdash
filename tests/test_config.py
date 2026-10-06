from pathlib import Path

import pytest

from devdash.config import ConfigError, example_config_text, load_config, parse_config


def test_example_config_parses_cleanly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))  # example scans ~/Projects
    (tmp_path / "Projects").mkdir()
    path = tmp_path / "config.toml"
    path.write_text(example_config_text())
    config = load_config(path)
    assert [p.name for p in config.projects] == ["hackathon-app", "discord-bot"]
    assert config.warnings == ()
    frontend = config.projects[0].services[0]
    assert frontend.port == 5173
    assert frontend.cwd == tmp_path / "Projects/hackathon-app/web"


def test_missing_and_invalid_files_raise(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="--init-config"):
        load_config(tmp_path / "nope.toml")
    bad = tmp_path / "bad.toml"
    bad.write_text("projects = [")
    with pytest.raises(ConfigError, match="Invalid TOML"):
        load_config(bad)


def test_bad_entries_are_skipped_with_warnings() -> None:
    config = parse_config({
        "refresh_interval": "fast",
        "projects": [
            {"name": "ok", "path": "/tmp", "services": [
                {"name": "web", "command": "x", "port": 99999},
                {"name": "nocmd"},
                {"name": "web", "command": "dupe"},
                {"name": "abs", "command": "x", "cwd": "/srv"},
            ]},
            {"name": "nopath"},
            {"name": "ok", "path": "/elsewhere"},
            "garbage",
        ],
    }, Path("c.toml"))
    assert [p.name for p in config.projects] == ["ok"]
    services = config.projects[0].services
    assert [s.name for s in services] == ["web", "abs"]
    assert services[0].port is None  # invalid port dropped, service kept
    assert services[1].cwd == Path("/srv")
    assert len(config.warnings) == 7
