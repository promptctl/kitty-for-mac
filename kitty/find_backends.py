#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

# Find has two places to search, and the find bar drives whichever sees what the user is looking at:
# kitty's own engine over the window's buffer, or the history search of the tmux pane the window shows.

from enum import IntEnum
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from .fast_data_types import FindOrigin
    from .window import Window


class FindCount(IntEnum):
    "How the find bar reads a search's total. The values reach Swift as KittyFindCount."
    exact = 0
    partial = 1  # the backend is still counting: the total is a lower bound
    unavailable = 2  # the backend could not be asked: there is no total, which is not the same as no matches
    pending = 3  # the query is typed but not searched yet: no total belongs to it


class FindStatus(NamedTuple):
    current: int  # the current match's place among the matches, 0 when none is current
    total: int
    count: FindCount


class FindBackend(Protocol):
    def can_search(self) -> bool:
        "False when this window has nothing to search: the find keys then go to the program"

    def find(self, text: str) -> None:
        "Search for text and land on the first match"

    def use_selection(self, text: str) -> None:
        "Search for the text of the selection the user points at, and land on it"

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


class WaitingStep(NamedTuple):
    "A step pressed before the search reached its answer"
    backwards: bool
    origin: 'FindOrigin'  # where the user was when they pressed it
    user_selection_changes: int  # the screen's count of the user's selection changes at the press


class EngineFind:
    "Find through kitty's engine, over the buffer of the window's Screen"

    def __init__(self, window: 'Window') -> None:
        self.window = window
        # [LAW:one-source-of-truth] the steps pressed whose answers are not searched yet, in press
        # order. The frame searches toward the first one's want, and each report tries them again.
        self.waiting: list[WaitingStep] = []

    def can_search(self) -> bool:
        # the alternate screen shows none of the buffer, so there is nothing to search or to show
        return self.window.screen.is_main_linebuf()

    def find(self, text: str) -> None:
        # typing the query that is already searched for presses nothing, so the match stays
        if self.set_query(text, users_selection=False):
            self.press(False)

    def use_selection(self, text: str) -> None:
        # the selection may be find's current match: a new query lands on the text the user points
        # at, and the same query is a Next from it
        self.set_query(text, users_selection=True)
        self.press(False)

    def restore(self, text: str) -> None:
        self.set_query(text, users_selection=False)

    def set_query(self, text: str, users_selection: bool) -> bool:
        "Search the whole buffer for text, case-insensitively unless it has an uppercase letter; True when the query changed"
        changed = self.window.screen.find_set_query(text, text == text.lower(), users_selection)
        if changed:
            # the steps waiting were for the query that ended
            self.drop_waiting()
        return changed

    def step(self, text: str, backwards: bool) -> None:
        # Esc ended the engine's search but not the query: stepping searches for it again, and an
        # identical query changes nothing
        self.restore(text)
        self.press(backwards)

    def press(self, backwards: bool) -> None:
        w = self.window
        w.finish_scroll_animation()
        s = w.screen
        self.waiting.append(WaitingStep(backwards, s.find_origin(w.find_bar_top_inset()), s.user_selection_changes()))
        self.resolve()

    def resolve(self) -> None:
        "Land the waiting steps, in press order, as far as the search has answered them"
        w = self.window
        s = w.screen
        # a landing scrolls the view, so it waits for the user's scroll to end, and for the main
        # screen, the only one it can show a match on
        if not self.waiting or not s.is_main_linebuf() or w.scroll_animation.timer:
            return
        while self.waiting:
            head = self.waiting[0]
            if head.user_selection_changes != s.user_selection_changes():
                # the user selected since pressing it: a landing would replace their selection
                del self.waiting[0]
                continue
            result = s.find_step(head.backwards, head.origin, w.find_bar_top_inset())
            if result[0] == 'waiting':
                s.find_set_want(result[1])
                return
            del self.waiting[0]
            if result[0] == 'landed':
                # a new match to show, so a paused frame shows it, as the key would
                s.pause_rendering(False)
                # "next" pressed after a "next" is the one after it; a step pressed after the user
                # moved is dropped above, since the landing left their move behind
                if self.waiting:
                    self.waiting[0] = self.waiting[0]._replace(origin=(True, result[1], (0, 0), (0, 0)))
        s.find_set_want(None)

    def drop_waiting(self) -> None:
        self.waiting.clear()
        self.window.screen.find_set_want(None)

    def clear(self) -> None:
        self.drop_waiting()
        self.window.screen.find_clear()

    def searching(self) -> bool:
        _, total, _, unsearched = self.window.screen.find_status()
        return bool(total or unsearched)

    def status(self) -> FindStatus:
        # [LAW:one-source-of-truth] the read the window tells its bar from: it also records what the
        # bar was told, so the frame stops reporting a change this window has handled
        current, total, _, unsearched = self.window.screen.find_status_for_bar()
        return FindStatus(current, total, FindCount.partial if unsearched else FindCount.exact)

    def program_selection_text(self) -> str | None:
        # the buffer is kitty's, so the selection is kitty's too
        return None
