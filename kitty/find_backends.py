#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

# Find has two places to search, and the find bar drives whichever sees what the user is looking at:
# kitty's own engine over the window's buffer, or the history search of the tmux pane the window shows.

from enum import IntEnum
from typing import TYPE_CHECKING, NamedTuple, Protocol

if TYPE_CHECKING:
    from .fast_data_types import FindOrigin
    from .window import Window


# The values of the three enums below are kitty/kitty_rs.h's and kitty/swift/KittyFindBar.swift's.
class FindCaseRule(IntEnum):
    "How a query's case decides a match"

    smart = 0  # ignore case unless the query has an uppercase letter of its own
    ignore = 1
    match = 2


class FindSyntax(IntEnum):
    literal = 0
    regex = 1


class FindMode(NamedTuple):
    "How a query matches: what the find bar's menu sets, for kitty's own search"

    case_rule: FindCaseRule = FindCaseRule.smart
    syntax: FindSyntax = FindSyntax.literal


class FindQuery(NamedTuple):
    text: str
    mode: FindMode


class FindQueryChange(IntEnum):
    "What setting the engine's query or mode did"

    unchanged = 0
    changed = 1
    rejected = 2  # a new query that cannot be searched for, such as a regular expression that does not parse


class FindQueryState(IntEnum):
    empty = 0
    searching = 1
    rejected = 2


class FindCount(IntEnum):
    "How the find bar reads a search's total. The values reach Swift as KittyFindCount."

    exact = 0
    partial = 1  # the backend is still counting: the total is a lower bound
    unavailable = 2  # the backend could not be asked: there is no total, which is not the same as no matches
    pending = 3  # the query is typed but not searched yet: no total belongs to it
    rejected = 4  # the query cannot be searched for: there is no total, and the pattern is what to fix


class FindStatus(NamedTuple):
    current: int  # the current match's place among the matches, 0 when none is current
    total: int
    count: FindCount


class FindBackend(Protocol):
    def can_search(self) -> bool:
        "False when this window has nothing to search: the find keys then go to the program"

    # A query's mode is kitty's own search's: tmux and Claude Code search by their own rules, so
    # their backends use its text alone, and the bar's menu offers no mode while they search.
    def find(self, query: FindQuery) -> None:
        "Search for the query and land on the first match"

    def use_selection(self, query: FindQuery) -> str:
        "Search for the text of the selection the user points at, and land on it; the query text searched for"

    def restore(self, query: FindQuery) -> None:
        "Bring the search in line with the query after the bar followed focus here"

    def step(self, query: FindQuery, backwards: bool) -> None:
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

    def find(self, query: FindQuery) -> None:
        # typing the query that is already searched for presses nothing, so the match stays
        if self.set_query(query, users_selection=False):
            self.press(False)

    def use_selection(self, query: FindQuery) -> str:
        from .fast_data_types import find_escape  # the engine is macOS only

        # a regular expression is seeded with the pattern that matches the selection as written
        text = find_escape(query.text) if query.mode.syntax is FindSyntax.regex else query.text
        # the selection may be find's current match: a new query lands on the text the user points
        # at, and the same query is a Next from it
        self.set_query(query._replace(text=text), users_selection=True)
        self.press(False)
        return text

    def restore(self, query: FindQuery) -> None:
        self.set_query(query, users_selection=False)

    def set_query(self, query: FindQuery, users_selection: bool) -> bool:
        "Search the whole buffer for the query; True when the query changed"
        return self.search_changed(FindQueryChange(self.window.screen.find_set_query(query.text, *query.mode, users_selection)))

    def set_mode(self, mode: FindMode) -> None:
        "Search for the query already set again, in mode"
        self.search_changed(FindQueryChange(self.window.screen.find_set_mode(*mode)))

    # [LAW:single-enforcer] a new search, of a new query or mode, ends the steps waiting for the old one
    def search_changed(self, change: FindQueryChange) -> bool:
        if change is not FindQueryChange.unchanged:
            self.drop_waiting()
        return change is not FindQueryChange.unchanged

    def step(self, query: FindQuery, backwards: bool) -> None:
        # Esc ended the engine's search but not the query: stepping searches for it again, and an
        # identical query changes nothing
        self.restore(query)
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
        # a landing scrolls the view and replaces the selection, so it waits for the user's drag or
        # scroll to end, and for the main screen, the only one it can show a match on
        if not self.waiting or not s.is_main_linebuf() or s.selection_in_progress() or w.scroll_animation.timer:
            return
        while self.waiting:
            head = self.waiting[0]
            result = s.find_step(head.backwards, head.origin, w.find_bar_top_inset())
            if result[0] == 'waiting':
                s.find_set_want(result[1])
                return
            del self.waiting[0]
            if result[0] == 'landed':
                # a new match to show, so a paused frame shows it, as the key would
                s.pause_rendering(False)
                # "next" pressed after a "next" is the one after it, unless the user moved in between
                if self.waiting and self.waiting[0].user_selection_changes == head.user_selection_changes:
                    self.waiting[0] = self.waiting[0]._replace(origin=(True, result[1], (0, 0), (0, 0)))
        s.find_set_want(None)

    def drop_waiting(self) -> None:
        self.waiting.clear()
        self.window.screen.find_set_want(None)

    def clear(self) -> None:
        self.drop_waiting()
        self.window.screen.find_clear()

    def searching(self) -> bool:
        _, total, _, unsearched, _ = self.window.screen.find_status()
        return bool(total or unsearched)

    def status(self) -> FindStatus:
        # [LAW:one-source-of-truth] the read the window tells its bar from: it also records what the
        # bar was told, so the frame stops reporting a change this window has handled
        current, total, _, unsearched, query = self.window.screen.find_status_for_bar()
        # a rejected query has no matches, so it has neither a current match nor a total
        count = FindCount.rejected if query == FindQueryState.rejected else FindCount.partial if unsearched else FindCount.exact
        return FindStatus(current, total, count)

    def program_selection_text(self) -> str | None:
        # the buffer is kitty's, so the selection is kitty's too
        return None
