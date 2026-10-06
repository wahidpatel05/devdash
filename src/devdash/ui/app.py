from __future__ import annotations

import asyncio
import dataclasses
import signal
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.css.stylesheet import Stylesheet
from textual.containers import Center, Horizontal, Middle, Vertical
from textual.widgets import DataTable, Footer, Input, Label, ListView

from devdash import git
from devdash.config import DEFAULT_THEME, Config, ConfigError, Project, default_config_path, load_config
from devdash.git import GitResult
from devdash.services import LogLine, ManagedService, ServiceManager, State, port_open
from devdash.theme import OMARCHY_THEME_NAME, colors_signature, load_omarchy_theme
from devdash.ui.logview import LogView
from devdash.ui.services_table import ServicesTable
from devdash.ui.widgets import ProjectList, RepoPanel

BUILTIN_CSS = Path(__file__).with_name("devdash.tcss")
TICK = 1.0  # seconds: uptime column, port checks every 2 ticks, theme check every 2 ticks


def _css_error_text(error: Exception) -> str:
    rules = getattr(getattr(error, "errors", None), "rules", None)
    if not rules:
        return str(error)
    problems = []
    for rule in rules:
        for token, message in rule.errors:
            line, col = token.location
            summary = getattr(message, "summary", message)
            problems.append(f"line {line + 1}:{col + 1} {summary}")
    return "; ".join(problems) or "invalid stylesheet"


class DevDashApp(App):
    TITLE = "devdash"

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("r", "restart", "Restart"),
        Binding("slash", "search", "Search"),
        Binding("n", "search_next", "Next match"),
        Binding("N", "search_prev", "Prev match"),
        Binding("escape", "cancel_search", show=False),
        Binding("c", "clear_log", "Clear log"),
        Binding("g", "refresh_git", "Git refresh"),
        Binding("R", "rescan", "Rescan"),
        Binding("f", "fetch", "Fetch"),
    ]

    def __init__(self, config: Config | None, config_error: str | None = None) -> None:
        super().__init__(css_path=[BUILTIN_CSS])
        self._css_warning: str | None = None
        user_css = config.user_css_path if config else default_config_path().parent / "devdash.tcss"
        if user_css.is_file():
            self._add_user_css(user_css)
        self.config = config
        self.config_error = config_error
        self.statuses: dict[str, GitResult] = {}
        self._git_inflight: set[str] = set()
        self._ports_inflight = False
        self._ticks = 0
        self._shutting_down = False
        self._theme_sig: tuple[int, int] | None = None
        self.manager: ServiceManager | None = None
        if config:
            self.manager = ServiceManager(config.projects, self._on_service_line,
                                          self._on_service_state, config.log_lines)

    def _add_user_css(self, path: Path) -> None:
        """Load the user's override stylesheet, unless it's broken: then warn instead
        of letting Textual abort startup (which would just close a floating window)."""
        try:
            sheet = Stylesheet(variables=self.get_css_variables())
            sheet.read(path)
            sheet.parse()
        except Exception as e:  # tokenizer/parse/IO errors don't share a base class
            self._css_warning = f"Ignoring {path} (using built-in styles): {_css_error_text(e)}"
            return
        self.css_path.append(path)

    @property
    def projects(self) -> tuple[Project, ...]:
        return self.config.projects if self.config else ()

    # --- layout ----------------------------------------------------------

    def compose(self) -> ComposeResult:
        if self.config_error:
            with Middle(), Center():
                yield Label(self.config_error, id="config-error", markup=False)
            yield Footer()
            return

        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield ProjectList(self.projects, id="projects")
                yield RepoPanel(id="repo")
            with Vertical(id="right"):
                yield ServicesTable(id="services")
                yield LogView(self.config.log_lines if self.config else 5000, id="logs")
                yield Input(placeholder="search logs, Enter to find, Esc to cancel", id="search")
        yield Footer()

    def on_mount(self) -> None:
        self._apply_theme(self.config.theme if self.config else DEFAULT_THEME)
        if self._css_warning:
            self.notify(self._css_warning, title="Stylesheet", severity="error", timeout=15)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGHUP, signal.SIGTERM):  # terminal closed / killed: still clean up
            try:
                loop.add_signal_handler(sig, self.exit)
            except (NotImplementedError, RuntimeError):
                pass

        if self.config_error or self.config is None:
            return
        for title, selector in (("Projects", "#projects"), ("Git", "#repo"),
                                ("Services", "#services"), ("Logs", "#logs")):
            self.query_one(selector).border_title = title
        self.query_one("#search").display = False
        for warning in self.config.warnings:
            self.notify(warning, title="Config", severity="warning", timeout=10)

        self.query_one(ProjectList).focus()
        self.refresh_git()
        self.set_interval(self.config.refresh_interval, self.refresh_git)
        self.set_interval(TICK, self._tick)
        self.check_ports()

    # --- theme -----------------------------------------------------------

    def _apply_theme(self, name: str) -> None:
        if name == OMARCHY_THEME_NAME:
            theme = load_omarchy_theme()
            self._theme_sig = colors_signature()
            if theme is None:
                return  # not on Omarchy (or no theme set): keep Textual's default
            self.register_theme(theme)
            if self.theme == OMARCHY_THEME_NAME:
                self.refresh_css()  # same name, new colours
            else:
                self.theme = OMARCHY_THEME_NAME
        elif name in self.available_themes:
            self.theme = name
        else:
            self.notify(f"Unknown theme {name!r}; try ctrl+p → Change theme", severity="warning")

    def watch_theme(self) -> None:
        # Rich-rendered cells/lines bake their colours in, so redraw them.
        if self.config_error:
            return
        self.call_after_refresh(self._restyle)

    def _restyle(self) -> None:
        try:
            self.query_one(ServicesTable).refresh_all()
            self.query_one(LogView).rerender()
        except Exception:
            pass

    def _check_theme_changed(self) -> None:
        if self.config and self.config.theme == OMARCHY_THEME_NAME and self.theme == OMARCHY_THEME_NAME:
            if colors_signature() != self._theme_sig:
                self._apply_theme(OMARCHY_THEME_NAME)

    # --- periodic work ---------------------------------------------------

    def _tick(self) -> None:
        self._ticks += 1
        self.query_one(ServicesTable).refresh_all()  # uptime
        if self._ticks % 2 == 0:
            self.check_ports()
            self._check_theme_changed()

    @work(group="ports")
    async def check_ports(self) -> None:
        if self._ports_inflight or self.manager is None:
            return
        self._ports_inflight = True
        try:
            with_ports = [ms for ms in self.manager.services.values() if ms.service.port]
            results = await asyncio.gather(*(port_open(ms.service.port) for ms in with_ports))
            table = self.query_one(ServicesTable)
            table.port_status.update({ms.key: up for ms, up in zip(with_ports, results)})
            table.refresh_all()
        finally:
            self._ports_inflight = False

    # --- selection -------------------------------------------------------

    @property
    def selected_project(self) -> Project | None:
        return self.query_one(ProjectList).selected_project

    @property
    def selected_service(self) -> ManagedService | None:
        if self.config_error:
            return None
        return self.query_one(ServicesTable).selected

    @on(ListView.Highlighted, "#projects")
    def _project_changed(self) -> None:
        project = self.selected_project
        self.query_one(RepoPanel).show(project, self.statuses.get(project.name) if project else None)
        services = self.manager.for_project(project) if project and self.manager else []
        self.query_one(ServicesTable).show_project(project, services)
        self._show_logs_for_selection()
        if project:
            self.refresh_git((project,))

    @on(DataTable.RowHighlighted, "#services")
    def _service_changed(self) -> None:
        self._show_logs_for_selection()

    def _show_logs_for_selection(self) -> None:
        logs = self.query_one(LogView)
        ms = self.selected_service
        if ms is not logs.service:
            logs.show(ms)
        logs.border_title = f"Logs · {ms.service.name}" if ms else "Logs"
        self.refresh_bindings()

    # --- service events (called by ServiceManager) -----------------------

    def _on_service_line(self, ms: ManagedService, line: LogLine) -> None:
        try:
            self.query_one(LogView).append(ms, line)
        except NoMatches:  # output arriving while the UI is being torn down
            pass

    def _on_service_state(self, ms: ManagedService) -> None:
        try:
            self._show_service_state(ms)
        except NoMatches:
            pass

    def _show_service_state(self, ms: ManagedService) -> None:
        self.query_one(ServicesTable).update_service(ms)
        if self.manager:
            item = self.query_one(ProjectList).project_items.get(ms.project.name)
            if item:
                item.set_running(len(self.manager.running(ms.project)))
        if ms.state == State.EXITED and ms.exit_code != 0:
            self.notify(f"{ms.service.name} exited with code {ms.exit_code}",
                        title=ms.project.name, severity="error")
        elif ms.state == State.FAILED:
            self.notify(f"{ms.service.name} failed to start; see its log",
                        title=ms.project.name, severity="error")
        self.refresh_bindings()

    # --- service actions -------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if self.config_error:
            return action == "quit"
        try:
            ms = self.selected_service
            logs = self.query_one(LogView)
        except NoMatches:  # footer asks before the widgets are mounted
            return True
        if action == "start":
            return True if ms and not ms.active else None
        if action == "stop":
            return True if ms and ms.active else None
        if action in ("restart", "clear_log", "search"):
            return True if ms else None
        if action in ("search_next", "search_prev"):
            return logs.search is not None  # hidden until a search is active
        return True

    @work(group="services")
    async def _run(self, op: str, ms: ManagedService) -> None:
        if self.manager is None:
            return
        try:
            if op in ("start", "restart") and ms.service.port and not ms.active:
                if await port_open(ms.service.port):
                    self.notify(f"Port {ms.service.port} is already in use by another process",
                                title=ms.service.name, severity="warning")
            await getattr(self.manager, op)(ms)
        except Exception as e:  # keep the dashboard alive no matter what
            self.notify(f"{op} failed: {e}", title=ms.service.name, severity="error")
        self.check_ports()

    def action_start(self) -> None:
        if ms := self.selected_service:
            self._run("start", ms)

    def action_stop(self) -> None:
        if ms := self.selected_service:
            self._run("stop", ms)

    def action_restart(self) -> None:
        if ms := self.selected_service:
            self._run("restart", ms)

    def action_clear_log(self) -> None:
        if ms := self.selected_service:
            ms.lines.clear()
            self.query_one(LogView).rerender()

    # --- log search ------------------------------------------------------

    def action_search(self) -> None:
        search = self.query_one("#search", Input)
        search.value = self.query_one(LogView).search_text
        search.display = True
        search.focus()

    @on(Input.Submitted, "#search")
    def _search_submitted(self, event: Input.Submitted) -> None:
        event.input.display = False
        logs = self.query_one(LogView)
        logs.set_search(event.value.strip())
        logs.focus()
        self.refresh_bindings()

    def action_cancel_search(self) -> None:
        search = self.query_one("#search", Input)
        logs = self.query_one(LogView)
        if search.display:
            search.display = False
            logs.focus()
        elif logs.search:
            logs.set_search("")
            self.refresh_bindings()

    def action_search_next(self) -> None:
        self.query_one(LogView).jump(+1)

    def action_search_prev(self) -> None:
        self.query_one(LogView).jump(-1)

    # --- git -------------------------------------------------------------

    def _render_project(self, project: Project) -> None:
        status = self.statuses.get(project.name)
        self.query_one(ProjectList).project_items[project.name].set_status(status)
        if project == self.selected_project:
            self.query_one(RepoPanel).show(project, status)

    @work(group="git")
    async def refresh_git(self, projects: tuple[Project, ...] | None = None) -> None:
        """Refresh git status concurrently. Projects already being queried are skipped,
        so a slow repo never piles up overlapping git processes."""
        todo = [p for p in (projects or self.projects) if p.name not in self._git_inflight]
        self._git_inflight.update(p.name for p in todo)
        try:
            async def one(project: Project) -> None:
                try:
                    self.statuses[project.name] = await git.get_status(project.path)
                except Exception as e:  # never let one repo kill the refresh loop
                    self.statuses[project.name] = git.RepoError(f"error: {e}")
                self._render_project(project)

            await asyncio.gather(*(one(p) for p in todo))
        finally:
            self._git_inflight.difference_update(p.name for p in todo)

    def action_refresh_git(self) -> None:
        self.refresh_git()

    @work(group="fetch")
    async def action_fetch(self) -> None:
        project = self.selected_project
        if project is None:
            return
        self.notify(f"Fetching {project.name}…", timeout=2)
        error = await git.fetch(project.path)
        if error:
            self.notify(error.message, title=f"Fetch failed: {project.name}", severity="error")
        else:
            self.notify(f"{project.name} fetched", timeout=2)
        self.refresh_git((project,))

    # --- rescan ----------------------------------------------------------

    @work(group="rescan", exclusive=True)
    async def action_rescan(self) -> None:
        """Re-read config.toml and re-scan scan_dirs without restarting."""
        if self.config is None or self.manager is None:
            return
        try:
            new = load_config(self.config.path)
        except ConfigError as e:  # keep running on the old config
            self.notify(str(e), title="Rescan failed", severity="error", timeout=10)
            return

        # A project that vanished but still has running services stays until they stop.
        names = {p.name for p in new.projects}
        kept = tuple(p for p in self.projects
                     if p.name not in names and self.manager.running(p))
        projects = new.projects + kept
        old_names = {p.name for p in self.projects}
        added = [p.name for p in projects if p.name not in old_names]
        removed = [n for n in old_names if n not in {p.name for p in projects}]

        self.config = dataclasses.replace(new, projects=projects)
        self.manager.update_projects(projects)
        self.statuses = {n: st for n, st in self.statuses.items()
                         if n in names or n in {p.name for p in kept}}

        project_list = self.query_one(ProjectList)
        await project_list.set_projects(projects)
        for project in projects:
            item = project_list.project_items[project.name]
            item.set_status(self.statuses.get(project.name))
            item.set_running(len(self.manager.running(project)))
        self._project_changed()  # services/logs for the (possibly new) selection
        self.refresh_git()

        for warning in new.warnings:
            self.notify(warning, title="Config", severity="warning", timeout=10)
        parts = []
        if added:
            parts.append("added " + ", ".join(added))
        if removed:
            parts.append("removed " + ", ".join(removed))
        if kept:
            parts.append(f"kept {', '.join(p.name for p in kept)} (services still running)")
        self.notify("; ".join(parts) if parts else f"No changes ({len(projects)} projects)",
                    title="Rescanned", timeout=6)

    # --- quit ------------------------------------------------------------

    async def action_quit(self) -> None:
        running = self.manager.running() if self.manager else []
        if not running or self._shutting_down:
            self.exit()  # second q while stopping = don't wait (cli still kills leftovers)
            return
        self._shutting_down = True
        self.notify(f"Stopping {len(running)} service(s)… press q again to force quit", timeout=10)
        self._stop_services_and_exit()

    @work(group="shutdown")
    async def _stop_services_and_exit(self) -> None:
        assert self.manager
        await self.manager.stop_all()
        self.exit()
