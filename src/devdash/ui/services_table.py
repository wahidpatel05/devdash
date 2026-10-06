"""The services table for the selected project."""

from __future__ import annotations

from rich.text import Text
from textual.binding import Binding
from textual.widgets import DataTable

from devdash.config import Project
from devdash.services import ManagedService, State

COLUMNS = ("Service", "Status", "PID", "Port", "Listening", "Uptime", "Command")
MAX_COMMAND_WIDTH = 48


def format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _truncate(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


class ServicesTable(DataTable):
    """Colours come from the component classes below, styled in devdash.tcss."""

    COMPONENT_CLASSES = {"services--good", "services--warn", "services--bad", "services--dim"}
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self, **kwargs) -> None:
        super().__init__(cursor_type="row", zebra_stripes=False, **kwargs)
        self._services: list[ManagedService] = []
        self.port_status: dict[tuple[str, str], bool] = {}

    def _ensure_columns(self) -> None:
        if not self.columns:
            for name in COLUMNS:
                self.add_column(name, key=name)

    def on_mount(self) -> None:
        self._ensure_columns()

    @property
    def selected(self) -> ManagedService | None:
        if not self._services or self.cursor_row < 0:
            return None
        return self._services[min(self.cursor_row, len(self._services) - 1)]

    def show_project(self, project: Project | None, services: list[ManagedService]) -> None:
        self._services = services
        self._ensure_columns()
        self.clear()
        for ms in services:
            self.add_row(*self._cells(ms), key=ms.service.name)
        if project and not services:
            self.border_subtitle = "no services configured"
        else:
            self.border_subtitle = ""
        if services:
            self.move_cursor(row=0)

    def update_service(self, ms: ManagedService) -> None:
        if ms not in self._services:
            return
        for column, value in zip(COLUMNS, self._cells(ms)):
            self.update_cell(ms.service.name, column, value, update_width=True)

    def refresh_all(self) -> None:
        for ms in self._services:
            self.update_service(ms)

    def _style(self, name: str):
        return self.get_component_rich_style(f"services--{name}", partial=True)

    def _text(self, text: str, style: str | None = None) -> Text:
        return Text(text, style=self._style(style) if style else "")

    def _cells(self, ms: ManagedService) -> tuple[Text, ...]:
        state = ms.state
        if state == State.RUNNING:
            status = self._text("● running", "good")
        elif state in (State.STARTING, State.STOPPING):
            status = self._text(f"◌ {state}", "warn")
        elif state == State.EXITED:
            ok = ms.exit_code == 0
            status = self._text(f"■ exited {ms.exit_code}", "dim" if ok else "bad")
        elif state == State.FAILED:
            status = self._text("✗ failed", "bad")
        else:
            status = self._text("○ stopped", "dim")

        port = ms.service.port
        listening = self.port_status.get(ms.key)
        if port is None:
            listen = self._text("—", "dim")
        elif listening is None:
            listen = self._text("…", "dim")
        elif listening and ms.active:
            listen = self._text("● up", "good")
        elif listening:
            listen = self._text("● in use (not ours)", "warn")
        elif ms.active:
            listen = self._text("○ waiting", "warn")
        else:
            listen = self._text("○ down", "dim")

        uptime = ms.uptime
        return (
            Text(ms.service.name, style="bold"),
            status,
            self._text(str(ms.pid) if ms.active and ms.pid else "—", "dim"),
            self._text(str(port) if port else "—", None if port else "dim"),
            listen,
            self._text(format_duration(uptime) if uptime is not None else "—", "dim"),
            self._text(_truncate(ms.service.command, MAX_COMMAND_WIDTH), "dim"),
        )
