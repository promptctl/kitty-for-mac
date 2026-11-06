#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

# Find inside a Claude Code pane in tmux: Claude Code draws its own full-screen UI on the alternate
# screen, so the conversation is in neither kitty's nor tmux's history. The find bar drives Claude
# Code's own transcript search (Ctrl+o, /, n, N) with tmux send-keys, one key at a time: a key is
# sent from a view just read, and the next one only once the pane shows the view that key must
# produce. Every failure to do that ends in one place, which logs what decided it and reports the
# search as unavailable; a key is never sent blind.

import glob
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from typing import NamedTuple

from .child import environ_of_process
from .find_backends import FindCount, FindStatus
from .tmux_find import (
    PANE_FORMAT,
    Held,
    PaneRef,
    TmuxClient,
    TmuxError,
    TmuxOperation,
    parse_displayed_pane,
    query_argument,
)
from .utils import log_error

IDLE = FindStatus(0, 0, FindCount.exact)
UNAVAILABLE = FindStatus(0, 0, FindCount.unavailable)


class Cause(Enum):
    "Why a Claude find could not do what it was asked: what the log says and the bar's unavailable stands for"
    unrecognised = 'the pane shows something other than the views of Claude Code 2.1.299'
    refused = 'the query cannot be typed and checked'
    tmux = 'tmux could not be asked'
    timed_out = 'the operation ran out of its time awaiting the pane to show the view a key must produce, or awaiting ps'
    unlocatable = 'the transcript file of a session with matches cannot be found'
    undeterminable = 'whether the pane is Claude Code could not be told'
    not_claude_pane = 'the pane is not Claude Code'


class ClaudeFindFailure(Exception):
    def __init__(self, cause: Cause, detail: str) -> None:
        super().__init__(f'{cause.name}: {detail}')
        self.cause = cause
        self.detail = detail


# The views of a Claude Code pane, parsed from its screen at the border


@dataclass(frozen=True)
class Prompt:
    "The input box, where the person types to Claude"


@dataclass(frozen=True)
class Transcript:
    "Claude's transcript view with no search open, or a search that found nothing"


@dataclass(frozen=True)
class SearchInput:
    "The transcript's search field, holding what has been typed so far (trailing spaces are not shown by tmux)"

    query: str


@dataclass(frozen=True)
class Searching:
    "An accepted search: the match the transcript is on, of all matches in the conversation"

    current: int
    total: int


@dataclass(frozen=True)
class Unrecognised:
    "A screen that is none of the above: a dialog, a picker, a footer this code does not know"

    footer: str


View = Prompt | Transcript | SearchInput | Searching | Unrecognised
ReturnView = type[Prompt] | type[Transcript]

TRANSCRIPT_MARK = 'Showing detailed transcript'
SEARCHING_MARK = 'n/N to navigate'
# the field row: /query, and at the far end the live count of matches, or "no matches"
SEARCH_ROW = re.compile(r'^\s*/(?P<query>.*?)(?:\s{2,}(?:\d+/\d+|no matches))?\s*$')
COUNT = re.compile(r'(\d+)/(\d+)\s*$')
INPUT_ROW = re.compile(r'^\s*❯(?: |$)')
PICKER_ROW = re.compile(r'^\s*❯\s+\d+\.')
PROMPT_ROWS = 12  # the input box is within this many rows of the bottom


def is_rule(row: str) -> bool:
    row = row.strip()
    return len(row) >= 20 and set(row) == {'─'}


def parse_view(capture: str) -> View:
    "The view a pane shows, from capture-pane -p; every variant is recognised by what it shows, never by what it lacks"
    rows = [r.rstrip() for r in capture.splitlines()]
    while rows and not rows[-1]:
        rows.pop()
    if not rows:
        return Unrecognised('')
    footer = rows[-1]
    if len(rows) > 1 and is_rule(rows[-2]) and (m := SEARCH_ROW.match(footer)):
        return SearchInput(m['query'])
    if TRANSCRIPT_MARK in footer:
        if SEARCHING_MARK not in footer:
            return Transcript()
        return Searching(int(c[1]), int(c[2])) if (c := COUNT.search(footer)) else Unrecognised(footer)
    tail = rows[-PROMPT_ROWS:]
    if any(INPUT_ROW.match(r) for r in tail) and not any(PICKER_ROW.match(r) for r in tail):
        return Prompt()
    return Unrecognised(footer)


def return_view_of(start: View) -> ReturnView:
    "The view a hold gives back, from what the pane showed before any key of ours: only a prompt or a transcript is known to be the person's"
    match start:
        case Prompt():
            return Prompt
        case Transcript() | SearchInput() | Searching():
            return Transcript
        case Unrecognised(footer):
            raise ClaudeFindFailure(Cause.unrecognised, f'cannot begin a search from {footer!r}')


class Key(NamedTuple):
    "What to send: a tmux key name, or literal text"

    send: str
    literal: bool = False

    def argv(self, pane: str) -> list[str]:
        # tmux splits a command at an argument that ends in a semicolon, so literal text ending in one is escaped
        return ['send-keys', '-t', pane, '-l', '--', query_argument(self.send)] if self.literal else ['send-keys', '-t', pane, self.send]


# how long a step waits for the counter to move before it takes the counter as it is
STEP_PATIENCE = 0.3
CTRL_O = Key('C-o')  # toggles the transcript view
SLASH = Key('/', literal=True)  # as its own send: arriving with the text it is read as pasted text
ENTER = Key('Enter')
ESCAPE = Key('Escape')
NEXT = Key('n', literal=True)
PREVIOUS = Key('N', literal=True)


def trimmed(query: str) -> str:
    "The form a typed query and the field's echo are compared in: tmux does not report trailing spaces"
    return query.rstrip()


def refusal(query: str) -> str | None:
    "Why a query cannot be typed and checked, None when it can"
    if any(ord(c) < 32 or ord(c) == 127 for c in query):
        return f'the query holds a control character, which would be a key: {query!r}'
    if not trimmed(query):
        return 'a query of only spaces cannot be told from the empty field'
    return None


# Which panes are Claude Code, from what the machine says about them


class ClaudePane(NamedTuple):
    "A tmux pane that runs a live Claude Code session"

    pane_id: str
    session_id: str
    pid: int
    config_dir: str  # the claude process's own CLAUDE_CONFIG_DIR: where its session file and transcript are written


@dataclass(frozen=True)
class NotClaude:
    "A pane that positively is not Claude Code: not on the alternate screen, in a tmux mode, or without a claude process in it"


class Written(NamedTuple):
    bytes: int


@dataclass(frozen=True)
class NotYetWritten:
    "The session has no transcript file: nothing has been said in it"


TranscriptSize = Written | NotYetWritten


def is_claude_command(command: str) -> bool:
    exe = command.split(' ', 1)[0]
    return os.path.basename(exe) == 'claude' or '/claude/versions/' in exe


class ClaudeHost:
    "The machine Claude Code runs on: its session files, its transcripts and its processes. The one place they are read."

    def __init__(self, ps: Callable[[float], str] | None = None, environ_of: Callable[[int], Mapping[str, str]] = environ_of_process) -> None:
        self.ps = ps or self.run_ps
        self.environ_of = environ_of

    @staticmethod
    def run_ps(timeout: float) -> str:
        try:
            cp = subprocess.run(['ps', '-A', '-o', 'pid=,ppid=,command='], capture_output=True, encoding='utf-8', errors='replace', check=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ClaudeFindFailure(Cause.timed_out, f'ps did not answer within {timeout:.2f}s') from None
        except (OSError, subprocess.SubprocessError) as e:
            raise ClaudeFindFailure(Cause.undeterminable, f'cannot list processes: {e}') from e
        return cp.stdout

    def processes(self, op: TmuxOperation) -> dict[int, tuple[int, str]]:
        "pid -> (parent pid, command); the listing spends from the operation's budget like every other wait"
        if (remaining := op.remaining()) <= 0:  # as TmuxOperation.tmux: a spent budget asks nothing
            raise ClaudeFindFailure(Cause.timed_out, 'the find deadline was spent before ps was asked')
        ans: dict[int, tuple[int, str]] = {}
        for line in self.ps(remaining).splitlines():
            if m := re.match(r'\s*(\d+)\s+(\d+)\s+(.*)$', line):
                ans[int(m[1])] = (int(m[2]), m[3])
        return ans

    def config_dir_of(self, pid: int) -> str:
        "Where the claude process writes its session file and transcripts: its own CLAUDE_CONFIG_DIR, which kitty's environment need not share"
        try:
            env = self.environ_of(pid)
        except Exception as e:  # the C extension and /proc raise whatever the platform does; the one border where it becomes a typed failure
            raise ClaudeFindFailure(Cause.undeterminable, f'cannot read the environment of claude process {pid}: {e}') from e
        return env.get('CLAUDE_CONFIG_DIR') or os.path.expanduser('~/.claude')

    def sessions(self, config_dir: str) -> list[dict]:
        "The session files of running Claude Code sessions; a file that is not readable JSON is skipped, a directory that cannot be listed is a failure"
        d = os.path.join(config_dir, 'sessions')
        try:
            names = sorted(os.listdir(d))
        except OSError as e:
            raise ClaudeFindFailure(Cause.undeterminable, f'cannot list {d}: {e}') from e
        ans = []
        for name in names:
            if not name.endswith('.json'):
                continue
            try:
                with open(os.path.join(d, name), encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                ans.append(data)
        return ans

    def transcript_size(self, pane: ClaudePane) -> TranscriptSize:
        "The size of the session's transcript file; session ids are UUIDs, so the file is the one match under the projects directory"
        for path in glob.glob(os.path.join(glob.escape(pane.config_dir), 'projects', '*', glob.escape(pane.session_id) + '.jsonl')):
            try:
                return Written(os.stat(path).st_size)
            except OSError:
                continue
        return NotYetWritten()


def recognise(pane: PaneRef, host: ClaudeHost, op: TmuxOperation) -> ClaudePane | NotClaude:
    """The pane's kind. NotClaude only on positive evidence; a pane that has a claude process in it whose session
    file cannot be matched is a ClaudeFindFailure, because treating it as another program would search the visible rows."""
    if pane.in_mode or not pane.alternate:
        return NotClaude()
    table = host.processes(op)
    children: dict[int, list[int]] = {}
    for pid, (ppid, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    tree: set[int] = set()
    pending = [pane.pid] if pane.pid in table else []
    while pending:
        pid = pending.pop()
        if pid not in tree:
            tree.add(pid)
            pending.extend(children.get(pid, ()))
    claudes = sorted(pid for pid in tree if is_claude_command(table[pid][1]))
    if not claudes:
        return NotClaude()
    for config_dir in dict.fromkeys(host.config_dir_of(pid) for pid in claudes):
        for s in host.sessions(config_dir):
            session_id, session_pid = s.get('sessionId'), s.get('pid')
            if s.get('tmux') == pane.address and isinstance(session_id, str) and isinstance(session_pid, int) and session_pid in tree:
                return ClaudePane(pane.id, session_id, session_pid, config_dir)
    raise ClaudeFindFailure(Cause.undeterminable, f'pane {pane.address} runs claude but no session file names it and one of its processes')


class RanSearch(NamedTuple):
    "The search this backend ran, and the transcript it ran over"

    pane: str
    query: str
    size: TranscriptSize


class Hold(NamedTuple):
    "A pane put into the transcript view, and the person's own view of it, read once before any key of ours: clear gives that view back"

    at: Held
    return_to: ReturnView


class ClaudeFindState:
    "What a window owns of its Claude search: the pane it put into the transcript view, the search it ran there, and what the bar was last told"

    def __init__(self) -> None:
        self.held: Hold | None = None
        self.search: RanSearch | None = None
        self.status = IDLE

    def reporting(self) -> bool:
        "Whether the bar is told this state's status: a search holds a pane, or failed before it reached one"
        return self.held is not None or self.status.count is FindCount.unavailable

    def rest(self) -> None:
        "Hold nothing and report nothing"
        self.held = None
        self.search = None
        self.status = IDLE


class ClaudeFind:
    "Find through Claude Code's transcript search in a pane. A value built for one operation, on the budget of it."

    def __init__(
        self,
        client: TmuxClient,
        state: ClaudeFindState,
        op: TmuxOperation,
        host: ClaudeHost,
        pane: ClaudePane | ClaudeFindFailure,
    ) -> None:
        self.client = client
        self.state = state
        self.op = op
        self.host = host
        self.pane = pane  # a failure to tell what the pane is: raised when the search is asked to run

    @classmethod
    def holding(cls, state: ClaudeFindState, op: TmuxOperation, host: ClaudeHost) -> 'ClaudeFind | None':
        "The backend that ends what a window left in a pane's transcript view, whatever its foreground is now"
        if state.held is None:
            return None
        return cls(state.held.at.client, state, op, host, ClaudeFindFailure(Cause.not_claude_pane, 'built only to end a search'))

    # Reading and driving the pane

    def view(self, held: Held) -> View:
        return parse_view(self.op.tmux(held.client, 'capture-pane', '-p', '-t', held.pane))

    def press(self, held: Held, key: Key, ready: Callable[[View], bool], awaiting: str) -> View:
        "Send a key, and return the view once it is a recognised one that ready accepts. ready must be false of the view the key was sent from."
        self.op.tmux(held.client, *key.argv(held.pane))
        return self.op.wait_until(lambda: self.view(held), lambda v: not isinstance(v, Unrecognised) and ready(v), awaiting)

    def open_search(self, held: Held) -> None:
        "Bring the pane to an empty search field, from whatever recognised view it shows"
        view = self.view(held)
        while view != SearchInput(''):
            match view:
                case Prompt():
                    view = self.press(held, CTRL_O, lambda v: isinstance(v, Transcript), 'the transcript view')
                case Transcript() | Searching():
                    view = self.press(held, SLASH, lambda v: v == SearchInput(''), 'the empty search field')
                case SearchInput():  # holds a query: leave the field, which keeps the transcript view
                    view = self.press(held, ESCAPE, lambda v: isinstance(v, Transcript), 'the transcript view')
                case Unrecognised(footer):
                    raise ClaudeFindFailure(Cause.unrecognised, f'cannot open the search from {footer!r}')

    def run_search(self, held: Held, text: str) -> FindStatus:
        self.open_search(held)
        self.press(held, Key(text, literal=True), lambda v: isinstance(v, SearchInput) and trimmed(v.query) == trimmed(text), f'the field holding {text!r}')
        after = self.press(held, ENTER, lambda v: not isinstance(v, SearchInput), 'the search accepted')
        return status_of(after)

    # The FindBackend protocol

    def can_search(self) -> bool:
        return True

    def find(self, text: str) -> None:
        if not text:  # an empty query searches nothing, as the engine's does
            self.clear()
            return
        self.guarded(lambda pane: self.search(pane, text))

    def use_selection(self, text: str) -> None:
        self.find(text)

    def restore(self, text: str) -> None:
        self.find(text)

    def step(self, text: str, backwards: bool) -> None:
        if not text:
            self.clear()
            return
        self.guarded(lambda pane: self.advance(pane, text, backwards))

    def clear(self) -> None:
        if (held := self.state.held) is None:
            self.state.rest()  # a failure reported with no hold is over too
            return
        try:
            self.end(held)
            self.state.rest()
        except TmuxError as e:
            log_error(f'Find in Claude: could not leave the transcript view of {held.at.pane}: {e}')
            if not e.timed_out:  # the pane or its server is gone: there is nothing left to clear
                self.state.rest()
        except ClaudeFindFailure as e:
            log_error(f'Find in Claude: could not leave the transcript view of {held.at.pane}: {e}')  # the next clear tries again

    def searching(self) -> bool:
        return self.state.held is not None

    def status(self) -> FindStatus:
        return self.state.status

    def program_selection_text(self) -> str | None:
        return None  # a selection made in Claude Code is Cmd+E's own ticket

    # Operations

    def guarded(self, action: Callable[[ClaudePane], None]) -> None:
        "[LAW:single-enforcer] the one place a Claude find fails: log what decided it, report the search unavailable"
        try:
            if isinstance(self.pane, ClaudeFindFailure):
                raise self.pane
            action(self.pane)
        except ClaudeFindFailure as e:
            self.fail(e.cause, e.detail)
        except TmuxError as e:
            self.fail(Cause.timed_out if e.timed_out else Cause.tmux, str(e))

    def fail(self, cause: Cause, detail: str) -> None:
        log_error(f'Find in Claude: {cause.name}: {detail}')
        self.state.search = None
        self.state.status = UNAVAILABLE

    def hold(self, pane: str) -> Held:
        "One pane at a time, on record before the first key is sent so a failure after it still has something to end"
        here = Held(self.client, pane)
        if self.state.held is not None and self.state.held.at != here:
            self.clear()
            if self.state.held is not None:  # the previous pane could not be left; the next clear retries it
                raise TmuxError('the previous pane could not be taken out of the transcript view')
        if self.state.held is None:  # the start of a hold: later searches in it find the pane in our own views
            self.state.held = Hold(here, return_view_of(self.view(here)))  # an unrecognised start fails here, before a hold records a guess
        return here

    def search(self, pane: ClaudePane, text: str) -> None:
        if (why := refusal(text)) is not None:
            raise ClaudeFindFailure(Cause.refused, why)
        # sampled before the first key: a write that lands during the search leaves this smaller than the file
        size = self.host.transcript_size(pane)
        held = self.hold(pane.pane_id)
        self.state.search = None
        status = self.run_search(held, text)
        if status.total > 0 and size == NotYetWritten() and self.host.transcript_size(pane) == NotYetWritten():
            where = f'{pane.config_dir}/projects'
            raise ClaudeFindFailure(Cause.unlocatable, f'session {pane.session_id} has {status.total} matches and no transcript file under {where}')
        self.state.search = RanSearch(pane.pane_id, text, size)
        self.state.status = status

    def advance(self, pane: ClaudePane, text: str, backwards: bool) -> None:
        held = self.hold(pane.pane_id)
        view = self.view(held)
        ran = self.state.search
        fresh = ran is not None and ran.pane == pane.pane_id and ran.query == text and ran.size == self.host.transcript_size(pane)
        if not (fresh and isinstance(view, Searching)):  # no search of ours is on screen, or Claude's list of matches is out of date
            self.search(pane, text)
            return
        if view.total > 1:
            # Claude's counter is what it shows, not what a step should add: two matches in one row can be one stop. So the
            # step waits for the counter to move, and a counter that stays is the answer.
            self.op.tmux(held.client, *(PREVIOUS if backwards else NEXT).argv(held.pane))
            view = self.op.wait_for_change(lambda: self.view(held), view, STEP_PATIENCE)
            view = self.op.wait_until(lambda: self.view(held), lambda v: not isinstance(v, Unrecognised), 'a recognised view')
        self.state.status = status_of(view)

    def end(self, hold: Hold) -> None:
        "Bring the held pane back to the view it was in when the hold began, unless it no longer is Claude Code's to bring"
        held = hold.at
        ref = parse_displayed_pane(self.op.tmux(held.client, 'display-message', '-p', '-t', held.pane, PANE_FORMAT))
        if isinstance(recognise(ref, self.host, self.op), NotClaude):
            return  # Claude quit, or the person took the pane over: no key of ours belongs in it
        view = self.view(held)
        while not isinstance(view, hold.return_to):
            match view:
                case Unrecognised(footer):
                    raise ClaudeFindFailure(Cause.unrecognised, f'cannot leave the search from {footer!r}')
                case Prompt():  # Esc from a search lands here; the person's transcript view comes back with Ctrl+o
                    key = CTRL_O
                case _:
                    key = ESCAPE
            view = self.press(held, key, changed_from(view), 'the view to change')


def changed_from(before: View) -> Callable[[View], bool]:
    return lambda view: view != before


def status_of(view: View) -> FindStatus:
    "What the bar is told once a search is accepted: the match Claude is on, or none"
    match view:
        case Searching(current, total):
            return FindStatus(current, total, FindCount.exact)
        case Transcript():
            return FindStatus(0, 0, FindCount.exact)  # the search found nothing
        case _:
            raise ClaudeFindFailure(Cause.unrecognised, f'a search was accepted and the pane shows {view!r}')
