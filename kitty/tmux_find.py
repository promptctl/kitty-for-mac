#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

# Find inside tmux: when a window's foreground process is a tmux client, the find bar drives the
# history search of the pane that client shows, with tmux's own match highlights and styles.
# Everything the bar asks is one or two spawns of the client's own tmux executable against its
# server, under one deadline, and touches only the one pane; the user's tmux config, key tables and
# paste buffers are left alone.

import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import NamedTuple, TypeVar

from .child import ProcessDesc, environ_of_process
from .find_backends import FindCount, FindQuery, FindStatus
from .utils import log_error

# One bound for everything a find operation asks of tmux: the pane lookup, the clears of other
# searches, the polling for a program to redraw and the commands. A healthy tmux answers in about
# 5 ms; a wedged one costs the person typing in the bar this much, once per operation.
FIND_DEADLINE = 1.0
# how often an operation looks again while it waits for a program in a pane to redraw
POLL_INTERVAL = 0.01
IDLE = FindStatus(0, 0, FindCount.exact)
UNAVAILABLE = FindStatus(0, 0, FindCount.unavailable)

# a search_count that is empty while a search is present: tmux withholds the count of an unchanged
# query after a live selection was cleared
COUNT_FORMAT = '#{search_present}|#{search_count}|#{search_count_partial}'
# a drag selection in copy mode stays live (the user's config keeps it) and a search would stretch it
LIVE_SELECTION = '#{||:#{selection_present},#{selection_active}}'
# a pane as the find backends need to tell it apart: the address is the one Claude Code records for
# its pane (~/.claude/sessions/<pid>.json), and it ends the line because a session name may hold spaces
PANE_FORMAT = '#{pane_id} #{pane_in_mode} #{alternate_on} #{pane_pid} #{session_name}:#{window_id}.#{pane_id}'
# the version is the server's, and rides on the one list-clients of an operation so asking it costs nothing
CLIENT_FORMAT = '#{client_pid} #{version} ' + PANE_FORMAT
T = TypeVar('T')
# a release (3.5a), a build from git on the way to a release (next-3.7) or a release candidate (3.6-rc2)
VERSION_SHAPE = re.compile(r'(next-)?(\d+)\.(\d+)[a-z]?(?:-rc\d*)?')


class TmuxError(Exception):
    "tmux could not be asked, or refused. timed_out: the deadline ended the wait, and tmux may still act on what it was sent."

    def __init__(self, message: str, timed_out: bool = False) -> None:
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class TmuxClient:
    "A tmux client process in a window's foreground, with what it takes to reach its server"
    pid: int
    exe: str  # as the client was started
    server_args: tuple[str, ...]  # ('-L', name) or ('-S', path), or () for the default socket
    tmpdir: str | None  # the client's TMUX_TMPDIR
    path: str | None  # the client's PATH, where exe is looked up


class TmuxVersion(NamedTuple):
    major: int
    minor: int
    released: bool  # a next-X.Y build is code from before X.Y, which may not have all of it yet, and sorts below it


# search_count and search_count_partial arrived in 3.5: an older server has no count for the bar
COUNTING = TmuxVersion(3, 5, True)
# 3.6 parses the options of copy-mode commands: before it, a -- is taken as the search text itself
COPY_MODE_OPTIONS = TmuxVersion(3, 6, True)


class PaneRef(NamedTuple):
    id: str
    in_mode: bool
    alternate: bool  # the program draws on the alternate screen, where tmux keeps no history of it
    pid: int  # the pane's own process
    address: str  # session_name:window_id.pane_id


class Counted(NamedTuple):
    present: bool
    total: int | None
    partial: bool


class ClientPane(NamedTuple):
    "What list-clients says of one client: the pane it shows, and its server's #{version} as tmux wrote it, parsed only where find needs it"
    pane: PaneRef
    version: str


class Held(NamedTuple):
    "The pane a window put into copy mode, and the client it reached it through"
    client: TmuxClient
    pane: str


# Parsing at the border: text the foreground processes and tmux gave us becomes a typed value or a TmuxError


def server_args_of(args: Sequence[str], cwd: str | None) -> tuple[str, ...]:
    "The -L or -S option naming the server, from the client's arguments before its command. A socket path is the client's cwd's."
    ans: tuple[str, ...] = ()
    it = iter(args)
    for arg in it:
        if not arg.startswith('-'):
            break  # the command starts here
        for i, flag in enumerate(arg[1:], 1):
            if flag in 'LSfcT':  # options that take a value, attached or as the next argument
                value = arg[i + 1:] or next(it, '')
                if flag in 'LS':
                    ans = (f'-{flag}', os.path.join(cwd or '', value) if flag == 'S' else value)
                break
    return ans


def tmux_client_of(
    processes: Iterable[ProcessDesc], environ_of: Callable[[int], Mapping[str, str]] = environ_of_process
) -> TmuxClient | None:
    "The tmux client among a window's foreground processes, None when it runs something else"
    for p in sorted(processes, key=lambda p: p['pid']):
        argv = list(p['cmdline'] or ())
        if argv and os.path.basename(argv[0]) == 'tmux':
            env: Mapping[str, str] = {}
            # a client we cannot read the environment of has not changed where its server is; if it
            # has, the lookup of its pid among the server's clients fails loudly
            with suppress(Exception):
                env = environ_of(p['pid'])
            return TmuxClient(p['pid'], argv[0], server_args_of(argv[1:], p['cwd']), env.get('TMUX_TMPDIR'), env.get('PATH'))
    return None


def find_version(text: str) -> TmuxVersion:
    "[LAW:single-enforcer] the server's version, from #{version}, where find can work with it: 3.5 or later, as tmux numbers it"
    if (m := VERSION_SHAPE.fullmatch(text)) is None or (version := TmuxVersion(int(m[2]), int(m[3]), m[1] is None)) < COUNTING:
        raise TmuxError(f'find in tmux needs tmux 3.5 or later for its match count; this server reports {text!r}')
    return version


def parse_pane_fields(fields: Sequence[str]) -> PaneRef:
    "A pane from the fields of PANE_FORMAT"
    pane_id, in_mode, alternate, pid, address = fields
    if in_mode not in ('0', '1') or alternate not in ('0', '1') or not pid.isdigit():
        raise TmuxError(f'tmux answered {" ".join(fields)!r} where a pane was expected')
    return PaneRef(pane_id, in_mode == '1', alternate == '1', int(pid), address)


def parse_pane(output: str, client_pid: int) -> ClientPane:
    "The pane the client shows and its server's version, from list-clients -F CLIENT_FORMAT"
    for line in output.splitlines():
        fields = line.split(' ', 6)
        if len(fields) == 7 and fields[0] == str(client_pid):
            return ClientPane(parse_pane_fields(fields[2:]), fields[1])
    raise TmuxError(f'tmux client {client_pid} is not among its server\'s clients')


def parse_displayed_pane(output: str) -> PaneRef:
    "A pane asked for by id, from display-message -p PANE_FORMAT"
    fields = output.strip('\n').split(' ', 4)
    if len(fields) != 5:
        raise TmuxError(f'tmux answered {output!r} where a pane was expected')
    return parse_pane_fields(fields)


def parse_count(output: str) -> Counted:
    "The search state of a pane, from display-message -p COUNT_FORMAT"
    fields = output.strip('\n').split('|')
    if len(fields) != 3 or not all(f.isdigit() or f == '' for f in fields):
        raise TmuxError(f'tmux answered {output!r} where a search count was expected')
    present, total, partial = fields
    return Counted(present == '1', int(total) if total else None, partial == '1')


def parse_selection(text: str) -> str | None:
    "The selected text as a query: one line, None when empty or spread over more"
    text = text.removesuffix('\n')
    return None if not text or '\n' in text else text


# Building commands: pure, so what is sent to tmux can be read and tested without a tmux


def query_argument(text: str) -> str:
    "Query text as one tmux argument: tmux splits a command at an argument that ends in a semicolon"
    return text[:-1] + '\\;' if text.endswith(';') else text


def batch(*commands: Sequence[str]) -> list[str]:
    "tmux commands as one argv; an argument that is only a semicolon separates them"
    ans: list[str] = []
    for i, command in enumerate(commands):
        if i:
            ans.append(';')
        ans.extend(command)
    return ans


def find_command(pane: str, text: str) -> list[str]:
    "Incremental search: the = restarts from where the search began, so each keystroke searches afresh"
    return ['send', '-t', pane, '-X', 'search-backward-incremental', query_argument('=' + text)]


def step_command(pane: str, text: str, backwards: bool, version: TmuxVersion) -> list[str]:
    "Later in the buffer is forward, earlier backward; where tmux parses options, the -- keeps a query like -n from being one"
    end_of_options = ['--'] if version >= COPY_MODE_OPTIONS else []
    return ['send', '-t', pane, '-X', 'search-backward-text' if backwards else 'search-forward-text', *end_of_options, query_argument(text)]


def search_batch(pane: str, search: Sequence[str]) -> list[str]:
    "Enter copy mode if the pane left it, end a live selection, search, and report the count"
    return batch(
        ['copy-mode', '-t', pane],
        ['if-shell', '-F', '-t', pane, LIVE_SELECTION, f'send -t {pane} -X clear-selection'],
        search,
        ['display-message', '-t', pane, '-p', COUNT_FORMAT],
    )


def cancel_batch(pane: str) -> list[str]:
    "Leave copy mode if the pane is still in it"
    return batch(['if-shell', '-F', '-t', pane, '#{pane_in_mode}', f'send -t {pane} -X cancel'])


def selection_batch(pane: str, pipe: str, channel: str) -> list[str]:
    # pipe, not copy-pipe: no paste buffer and no terminal clipboard write; no-clear: the selection stays.
    # The pipe is a job tmux runs on its own, so the wait-for is what returns when it is done.
    return batch(['send', '-t', pane, '-X', 'pipe-no-clear', pipe], ['wait-for', channel])


# The one effect: spawn tmux


def tmux_env(client: TmuxClient) -> dict[str, str]:
    "The environment the client had where it matters to tmux: with TMUX set tmux would use that socket instead"
    env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE', 'TMUX_TMPDIR')}
    if client.tmpdir is not None:
        env['TMUX_TMPDIR'] = client.tmpdir
    return env


def resolve_exe(client: TmuxClient) -> str:
    if (exe := shutil.which(client.exe, path=client.path)) is None:
        raise TmuxError(f'cannot find {client.exe!r} to ask tmux')
    return exe


# what sends one tmux command: its argv and environment, and the seconds it may take
Runner = Callable[[Sequence[str], Mapping[str, str], float], str]


def run_tmux(argv: Sequence[str], env: Mapping[str, str], timeout: float) -> str:
    try:
        cp = subprocess.run(argv, env=dict(env), capture_output=True, encoding='utf-8', errors='replace', timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise TmuxError(f'tmux did not answer within {timeout:.2f}s', timed_out=True) from None
    except OSError as e:
        raise TmuxError(f'cannot run tmux: {e}') from e
    if cp.returncode != 0:
        raise TmuxError(cp.stderr.strip() or f'tmux exited with {cp.returncode}')
    return cp.stdout


class TmuxOperation:
    """One find operation's budget of time, and the one place it is enforced: every tmux command and every
    wait for a pane to redraw is made through it, so the lookup of the pane, the clears of other searches
    and the search itself together cannot spend more than its deadline, FIND_DEADLINE by default. The runner,
    the clock and the sleep are what a test stands in for."""

    def __init__(
        self,
        run: Runner | None = None,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        deadline: float = FIND_DEADLINE,
    ) -> None:
        # looked up when the value is built, so a test can stand in for the one effect
        self.run = run_tmux if run is None else run
        self.now = now
        self.sleep = sleep
        self.expires_at = now() + deadline

    def remaining(self) -> float:
        return self.expires_at - self.now()

    def tmux(self, client: TmuxClient, *args: str) -> str:
        if (remaining := self.remaining()) <= 0:
            raise TmuxError('the find deadline was spent before tmux was asked', timed_out=True)
        return self.run([resolve_exe(client), *client.server_args, *args], tmux_env(client), remaining)

    def wait_until(self, read: Callable[[], T], ready: Callable[[T], bool], awaiting: str) -> T:
        "Read until ready accepts what was read, or the deadline passes: a TmuxError that says what was awaited and what was last seen"
        while True:
            value = read()
            if ready(value):
                return value
            if (remaining := self.remaining()) <= 0:
                raise TmuxError(f'timed out awaiting {awaiting}; last seen {value!r}', timed_out=True)
            self.sleep(min(POLL_INTERVAL, remaining))

    def wait_for_change(self, read: Callable[[], T], before: T, patience: float) -> T:
        "Read until the value is not `before`, or for at most `patience` and the budget: a value that stays the same is the answer, not an error"
        until = min(self.now() + patience, self.expires_at)
        while True:
            value = read()
            if value != before or (remaining := until - self.now()) <= 0:
                return value
            self.sleep(min(POLL_INTERVAL, remaining))


class TmuxFindState:
    "What a window owns of its tmux search: the pane it put in copy mode, and what the bar was last told"

    def __init__(self) -> None:
        self.held: Held | None = None
        self.status = IDLE
        # tmux searches a snapshot of the pane taken when copy mode starts, so the total of an
        # unchanged query cannot change: (pane, query, total)
        self.counted: tuple[str, str, int] | None = None

    def reporting(self) -> bool:
        "Whether the bar is told this state's status: a search holds a pane, or failed before it reached one"
        return self.held is not None or self.status.count is FindCount.unavailable

    def rest(self) -> None:
        "Hold nothing and report nothing"
        self.held = None
        self.status = IDLE
        self.counted = None


class TmuxFind:
    "Find through the tmux client in a window's foreground. A value built for one operation, on the budget of it, and for the one pane that operation finds."

    def __init__(self, client: TmuxClient, state: TmuxFindState, op: TmuxOperation) -> None:
        self.client = client
        self.state = state
        self.op = op
        self.client_pane: ClientPane | TmuxError | None = None  # not asked until something needs it

    @classmethod
    def holding(cls, state: TmuxFindState, op: TmuxOperation) -> 'TmuxFind | None':
        "The backend that cancels what a window left in copy mode, whatever its foreground is now"
        return cls(state.held.client, state, op) if state.held is not None else None

    def tmux(self, client: TmuxClient, *args: str) -> str:
        return self.op.tmux(client, *args)

    def listed(self) -> ClientPane | TmuxError:
        """
        [LAW:one-source-of-truth] the one list-clients of an operation: whoever routes on the pane and whoever
        searches it read this answer, so a pane switch inside tmux is followed by the next operation and never splits one
        """
        if self.client_pane is None:
            try:
                self.client_pane = parse_pane(self.tmux(self.client, 'list-clients', '-F', CLIENT_FORMAT), self.client.pid)
            except TmuxError as e:
                self.client_pane = e
        return self.client_pane

    def lookup(self) -> PaneRef | TmuxError:
        "The pane, whatever the server's version: routing to Claude's find never depends on it"
        listed = self.listed()
        return listed if isinstance(listed, TmuxError) else listed.pane

    def resolved(self) -> tuple[PaneRef, TmuxVersion]:
        "The pane, on a server find can work with, for whoever sends it copy-mode commands"
        listed = self.listed()
        if isinstance(listed, TmuxError):
            raise TmuxError(str(listed), listed.timed_out) from listed
        return listed.pane, find_version(listed.version)

    def can_search(self) -> bool:
        return True

    # tmux's text search is literal, ignoring case unless the text has an uppercase letter
    def find(self, query: FindQuery) -> None:
        self.search(query.text, lambda pane, version: find_command(pane, query.text))

    def use_selection(self, query: FindQuery) -> str:
        self.find(query)
        return query.text

    def restore(self, query: FindQuery) -> None:
        self.find(query)

    def step(self, query: FindQuery, backwards: bool) -> None:
        self.search(query.text, lambda pane, version: step_command(pane, query.text, backwards, version))

    def search(self, text: str, command: Callable[[str, TmuxVersion], Sequence[str]]) -> None:
        if not text:  # an empty query searches nothing, as the engine's does
            self.clear()
            return
        try:
            pane, version = self.resolved()
            self.move_to(pane.id)
            counted = parse_count(self.tmux(self.client, *search_batch(pane.id, command(pane.id, version))))
            self.state.status = self.status_of(pane.id, text, counted)
        except TmuxError as e:
            log_error(f'Find in tmux: {e}')
            self.state.status = UNAVAILABLE

    def move_to(self, pane: str) -> None:
        "One pane at a time: the pane a search left is cancelled, and the new one is on record before tmux is sent anything"
        here = Held(self.client, pane)
        if self.state.held is not None and self.state.held != here:
            self.clear()
            if self.state.held is not None:  # the cancel timed out; it may still run, and the next clear retries it
                raise TmuxError('the previous pane could not be taken out of copy mode')
        self.state.held = here

    def status_of(self, pane: str, text: str, counted: Counted) -> FindStatus:
        if not counted.present:  # tmux searched and found nothing
            return FindStatus(0, 0, FindCount.exact)
        total = counted.total
        if total is None:
            if self.state.counted is None or self.state.counted[:2] != (pane, text):
                raise TmuxError('tmux withheld the match count of a new query')
            total = self.state.counted[2]
        self.state.counted = (pane, text, total)
        return FindStatus(0, total, FindCount.partial if counted.partial else FindCount.exact)

    def clear(self) -> None:
        if (held := self.state.held) is not None:
            try:
                self.tmux(held.client, *cancel_batch(held.pane))
            except TmuxError as e:
                log_error(f'Find in tmux: could not leave copy mode of {held.pane}: {e}')
                if e.timed_out:
                    return  # tmux may still do it when it answers; the pair stays so the next clear tries again
        self.state.rest()

    def searching(self) -> bool:
        return self.state.held is not None

    def status(self) -> FindStatus:
        return self.state.status

    def program_selection_text(self) -> str | None:
        try:
            pane, _ = self.resolved()  # pipe-no-clear is in every server find can work with
            return parse_selection(self.read_selection(pane.id)) if pane.in_mode else None
        except TmuxError as e:
            log_error(f'Find in tmux: could not read the selection: {e}')
            return None

    def read_selection(self, pane: str) -> str:
        "The selection's text as tmux pipes it, which is what it would copy"
        with tempfile.TemporaryDirectory(prefix='kitty-find-') as d:
            out = os.path.join(d, 'selection')
            channel = f'kitty-find-{uuid.uuid4().hex}'
            signal = ' '.join(shlex.quote(a) for a in (resolve_exe(self.client), *self.client.server_args, 'wait-for', '-S', channel))
            self.tmux(self.client, *selection_batch(pane, f'cat > {shlex.quote(out)}; {signal}', channel))
            try:
                with open(out, encoding='utf-8', errors='replace') as f:
                    return f.read()
            except OSError as e:
                raise TmuxError(f'the selection tmux piped cannot be read: {e}') from e
