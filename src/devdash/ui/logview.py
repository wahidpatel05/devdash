"""Live log view for one service, with error highlighting and search."""

from __future__ import annotations

import re

from rich.text import Text
from textual.binding import Binding
from textual.widgets import RichLog

from devdash.services import LogLine, ManagedService

ERROR_PATTERN = re.compile(r"error|exception|failed", re.IGNORECASE)


class LogView(RichLog):
    """One rendered line per LogLine (no wrapping), so line index == buffer index,
    which is what makes search jumps exact."""

    COMPONENT_CLASSES = {"logview--error", "logview--stderr", "logview--system", "logview--match"}
    BINDINGS = [
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
        Binding("G", "scroll_end", show=False),
    ]

    def __init__(self, max_lines: int, **kwargs) -> None:
        super().__init__(max_lines=max_lines, wrap=False, markup=False, highlight=False,
                         auto_scroll=False, **kwargs)
        self.service: ManagedService | None = None
        self.search: re.Pattern[str] | None = None
        self.search_text = ""
        self.match_index: int | None = None  # buffer index of the current match

    # --- rendering -------------------------------------------------------

    def _style(self, name: str):
        return self.get_component_rich_style(f"logview--{name}", partial=True)

    def _format(self, line: LogLine) -> Text:
        if line.stream == "sys":
            return Text(line.text, style=self._style("system"))
        text = Text.from_ansi(line.text) if "\x1b" in line.text else Text(line.text)
        text.no_wrap = True
        if ERROR_PATTERN.search(text.plain):
            text.stylize(self._style("error"))
        if line.stream == "err":
            text = Text.assemble(Text("▏", style=self._style("stderr")), text)
        else:
            text = Text.assemble(" ", text)
        if self.search:
            text.highlight_regex(self.search, style=self._style("match"))
        return text

    def show(self, service: ManagedService | None) -> None:
        self.service = service
        self.match_index = None
        self.rerender()
        self.scroll_end(animate=False, immediate=True)

    def rerender(self) -> None:
        y = self.scroll_y
        at_end = self.is_vertical_scroll_end
        self.clear()
        if self.service:
            for line in self.service.lines:
                self.write(self._format(line), scroll_end=False)
        if at_end:
            self.scroll_end(animate=False)
        else:
            self.scroll_to(y=y, animate=False)
        self._update_subtitle()

    def append(self, service: ManagedService, line: LogLine) -> None:
        if service is not self.service:
            return
        if self.match_index is not None and len(service.lines) == service.lines.maxlen:
            self.match_index = max(0, self.match_index - 1)  # oldest line was dropped
        self.write(self._format(line), scroll_end=self.is_vertical_scroll_end)

    # --- search ----------------------------------------------------------

    def set_search(self, query: str) -> None:
        self.search_text = query
        self.search = re.compile(re.escape(query), re.IGNORECASE) if query else None
        self.match_index = None
        self.rerender()
        if self.search:
            self.jump(-1)

    def _matches(self) -> list[int]:
        if not (self.service and self.search):
            return []
        return [i for i, line in enumerate(self.service.lines) if self.search.search(line.plain)]

    def jump(self, direction: int) -> None:
        """direction +1 = next (down), -1 = previous (up), from the current match."""
        matches = self._matches()
        if not matches:
            self.match_index = None
            self._update_subtitle()
            self.app.bell()
            return
        if self.match_index is None:
            target = matches[-1] if direction < 0 else matches[0]
        elif direction > 0:
            target = next((i for i in matches if i > self.match_index), matches[0])
        else:
            target = next((i for i in reversed(matches) if i < self.match_index), matches[-1])
        self.match_index = target
        self.scroll_to(y=max(0, target - self.scrollable_content_region.height // 2), animate=False)
        self._update_subtitle(matches)

    def _update_subtitle(self, matches: list[int] | None = None) -> None:
        if not self.search:
            self.border_subtitle = ""
            return
        matches = self._matches() if matches is None else matches
        if not matches:
            self.border_subtitle = f"/{self.search_text}  no matches"
        elif self.match_index in matches:
            pos = matches.index(self.match_index) + 1
            self.border_subtitle = f"/{self.search_text}  match {pos}/{len(matches)}"
        else:
            self.border_subtitle = f"/{self.search_text}  {len(matches)} matches"
