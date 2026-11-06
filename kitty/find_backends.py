#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

# Find has two places to search, and the find bar drives whichever sees what the user is looking at:
# kitty's own engine over the window's buffer, or the history search of the tmux pane the window shows.

from enum import IntEnum
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from .window import Window


class FindCount(IntEnum):
    "How the find bar reads a search's total. The values reach Swift as KittyFindCount."
    exact = 0
    partial = 1  # the backend is still counting: the total is a lower bound
    unavailable = 2  # the backend could not be asked: there is no total, which is not the same as no matches


class FindStatus(NamedTuple):
    current: int  # the current match's place among the matches, 0 when none is current
    total: int
    count: FindCount


class FindBackend(Protocol):
    def can_search(self) -> bool:
        "False when this window has nothing to search: the find keys then go to the program"

    def find(self, text: str) -> None:
        "Search for text and land on the first match"

    def restore(self, text: str) -> None:
        "Bring the search in line with text after the bar followed focus here"

    def step(self, text: str, backwards: bool) -> None:
        "Move to the match later in the buffer, or earlier when backwards, wrapping at the ends"

    def clear(self) -> None:
        "End the search: no highlights, no selection made by find"

    def searching(self) -> bool:
        "Whether this backend holds a search that is on screen"

    def status(self) -> FindStatus:
        "What the find bar is told about the search"

    def program_selection_text(self) -> str | None:
        "The selection of the program in the window (tmux's copy mode) as one line, None when it has none"


class EngineFind:
    "Find through kitty's engine, over the buffer of the window's Screen"

    def __init__(self, window: 'Window') -> None:
        self.window = window

    def can_search(self) -> bool:
        # the alternate screen shows none of the buffer, so there is nothing to search or to show
        return self.window.screen.is_main_linebuf()

    def find(self, text: str) -> None:
        self.restore(text)
        self.step_to_match(False)

    def restore(self, text: str) -> None:
        "Search the whole buffer for text, case-insensitively unless it has an uppercase letter"
        self.window.screen.find_set_query(text, text == text.lower())

    def step(self, text: str, backwards: bool) -> None:
        # Esc ended the engine's search but not the query: stepping searches for it again
        if text and not self.searching():
            self.restore(text)
        self.step_to_match(backwards)

    def step_to_match(self, backwards: bool) -> None:
        w = self.window
        w.finish_scroll_animation()
        # with nothing to move to there is nothing new to show, so a paused frame stays paused
        if w.screen.find_step(backwards, w.find_bar_top_inset()) is not None:
            w.screen.pause_rendering(False)

    def clear(self) -> None:
        self.window.screen.find_clear()

    def searching(self) -> bool:
        return bool(self.window.screen.find_status()[1])

    def status(self) -> FindStatus:
        # [LAW:one-source-of-truth] the read the window tells its bar from: it also records what the
        # bar was told, so the frame stops reporting a change this window has handled
        current, total, _ = self.window.screen.find_status_for_bar()
        return FindStatus(current, total, FindCount.exact)

    def program_selection_text(self) -> str | None:
        # the buffer is kitty's, so the selection is kitty's too
        return None
