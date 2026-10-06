"""Widgets for the dashboard. All colours come from classes styled in devdash.tcss."""

from __future__ import annotations

from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Label, ListItem, ListView

from devdash.config import Project, short_path
from devdash.git import GitResult, RepoError, RepoStatus

STATE_CLASSES = ("-good", "-warn", "-bad", "-dim")


def _set_state(widget: Label, state: str | None) -> None:
    widget.remove_class(*STATE_CLASSES)
    if state:
        widget.add_class(state)


def relative_time(when: datetime, now: datetime | None = None) -> str:
    seconds = int(((now or datetime.now(timezone.utc)) - when).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("year", 31_536_000), ("month", 2_592_000), ("week", 604_800),
                       ("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


class ProjectItem(ListItem):
    """One row in the sidebar: a status dot plus the project name."""

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self._dot_label = Label("○", classes="dot -dim")
        self._running_label = Label("", classes="running -good")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield self._dot_label
            yield Label(self.project.name, classes="name", markup=False)
            yield self._running_label

    def set_running(self, count: int) -> None:
        self._running_label.update(f"▶ {count}" if count else "")

    def set_status(self, status: GitResult | None) -> None:
        if status is None:
            self._dot_label.update("○")
            _set_state(self._dot_label, "-dim")
        elif isinstance(status, RepoError):
            self._dot_label.update("✗")
            _set_state(self._dot_label, "-bad")
        else:
            self._dot_label.update("●")
            _set_state(self._dot_label, "-warn" if status.dirty else "-good")


class ProjectList(ListView):
    BINDINGS = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    def __init__(self, projects: tuple[Project, ...], **kwargs) -> None:
        self.project_items = {p.name: ProjectItem(p) for p in projects}
        super().__init__(*self.project_items.values(), **kwargs)

    async def set_projects(self, projects: tuple[Project, ...]) -> None:
        """Replace the list, keeping the same project selected if it still exists."""
        selected = self.selected_project
        await self.clear()
        self.project_items = {p.name: ProjectItem(p) for p in projects}
        await self.extend(self.project_items.values())
        names = list(self.project_items)
        self.index = names.index(selected.name) if selected and selected.name in names else 0

    @property
    def selected_project(self) -> Project | None:
        item = self.highlighted_child
        return item.project if isinstance(item, ProjectItem) else None


class _Row(Horizontal):
    def __init__(self, key: str, value: Label) -> None:
        super().__init__(classes="row")
        self._key, self.value = key, value

    def compose(self) -> ComposeResult:
        yield Label(self._key, classes="key")
        yield self.value


class RepoPanel(VerticalScroll):
    """Git details for the selected project."""

    can_focus = False

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._values = {
            key: Label("", classes="value", markup=False)
            for key in ("branch", "remote", "changes", "untracked", "commit", "when", "path")
        }
        self._rows: dict[str, _Row] = {}

    def compose(self) -> ComposeResult:
        titles = {"branch": "Branch", "remote": "Remote", "changes": "Changes",
                  "untracked": "Untracked", "commit": "Commit", "when": "", "path": "Path"}
        for key, label in self._values.items():
            self._rows[key] = row = _Row(titles[key], label)
            yield row
        self._message = Label("", classes="message", markup=False)
        yield self._message

    def _set(self, key: str, text: str, state: str | None = None) -> None:
        label = self._values[key]
        label.update(text)
        _set_state(label, state)
        self._rows[key].display = True

    def show(self, project: Project | None, status: GitResult | None) -> None:
        for row in self._rows.values():
            row.display = False
        self._message.display = False
        if project is None:
            self._show_message("No project selected", "-dim")
            return

        self.border_subtitle = project.name
        self._set("path", short_path(project.path), "-dim")

        if status is None:
            self._show_message("Loading…", "-dim")
        elif isinstance(status, RepoError):
            self._show_message(status.message, "-bad")
        else:
            self._show_status(status)

    def _show_message(self, text: str, state: str) -> None:
        self._message.update(text)
        _set_state(self._message, state)
        self._message.display = True

    def _show_status(self, s: RepoStatus) -> None:
        if s.branch:
            self._set("branch", s.branch, "-good")
        else:
            self._set("branch", f"detached @ {s.head or '?'}", "-warn")

        if not s.upstream:
            self._set("remote", "no upstream", "-dim")
        elif s.ahead or s.behind:
            self._set("remote", f"{s.upstream}  ↑{s.ahead} ↓{s.behind}", "-warn")
        else:
            self._set("remote", f"{s.upstream}  in sync", "-good")

        parts = [f"{n} {label}" for n, label in ((s.staged, "staged"), (s.modified, "modified"),
                                                  (s.conflicts, "conflicted")) if n]
        if parts:
            self._set("changes", ", ".join(parts), "-bad" if s.conflicts else "-warn")
        else:
            self._set("changes", "clean", "-good")
        self._set("untracked", str(s.untracked), "-warn" if s.untracked else "-dim")

        if s.last_commit_subject is None:
            self._set("commit", "no commits yet", "-dim")
        else:
            self._set("commit", s.last_commit_subject)
            if s.last_commit_time:
                self._set("when", relative_time(s.last_commit_time), "-dim")
