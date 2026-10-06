"""Build a Textual theme from the active Omarchy theme's colors.toml."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from textual.color import Color, ColorParseError
from textual.theme import Theme

OMARCHY_THEME_NAME = "omarchy"


def omarchy_colors_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or "~/.local/state"
    return Path(state).expanduser() / "omarchy" / "current" / "theme" / "colors.toml"


def colors_signature(path: Path | None = None) -> tuple[int, int] | None:
    """Changes whenever `omarchy theme set` swaps the theme directory."""
    try:
        st = (path or omarchy_colors_path()).stat()
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns)


def load_omarchy_theme(path: Path | None = None) -> Theme | None:
    try:
        data = tomllib.loads((path or omarchy_colors_path()).read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None

    def color(*keys: str) -> str | None:
        for key in keys:
            value = data.get(key)
            if isinstance(value, str):
                try:
                    return Color.parse(value).hex
                except ColorParseError:
                    continue
        return None

    accent = color("accent", "blue")
    background = color("background")
    foreground = color("foreground")
    if not (accent and background and foreground):
        return None

    variables = {}
    if muted := color("dark_foreground", "muted"):
        variables["foreground-muted"] = muted

    return Theme(
        name=OMARCHY_THEME_NAME,
        primary=accent,
        secondary=color("blue", "magenta") or accent,
        accent=accent,
        foreground=foreground,
        background=background,
        surface=color("lighter_background") or background,
        panel=color("lighter_background", "selection") or background,
        success=color("green") or "#4caf50",
        warning=color("yellow", "orange") or "#ffb300",
        error=color("red") or "#f44336",
        dark=data.get("mode") != "light",
        variables=variables,
    )
