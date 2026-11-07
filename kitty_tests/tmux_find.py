#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import fcntl
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import time
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest import mock

from kitty import tmux_find as tf
from kitty import window as window_module
from kitty.boss import Boss
from kitty.child import ProcessDesc
from kitty.claude_find import ClaudeFind, ClaudeFindState, ClaudeHost
from kitty.constants import is_macos
from kitty.find_backends import EngineFind, FindCount, FindMode, FindQuery, FindStatus
from kitty.tmux_find import (
    COPY_MODE_OPTIONS,
    FIND_DEADLINE,
    ClientPane,
    Counted,
    PaneRef,
    Runner,
    TmuxClient,
    TmuxError,
    TmuxFind,
    TmuxFindState,
    TmuxOperation,
    TmuxVersion,
    batch,
    cancel_batch,
    find_command,
    find_version,
    parse_count,
    parse_displayed_pane,
    parse_pane,
    parse_selection,
    query_argument,
    search_batch,
    server_args_of,
    step_command,
    tmux_client_of,
)
from kitty.window import Window

from .base import BaseTest

TMUX = shutil.which('tmux')
# A test driving a real tmux asks what the keys did, not how fast: a loaded machine (the full suite, a build)
# can spend FIND_DEADLINE on a few tmux spawns, and the find then stops the pane part way. Only a wedged test
# reaches this; a test of the deadline itself builds its operation with FIND_DEADLINE.
PATIENT_DEADLINE = 30.0
# A test of the deadline itself bounds its wall time by the operation's own deadline plus what the machine takes to
# kill and reap the tmux client cut off at it: at most 25 ms under 3x CPU saturation, but a scheduler stall on a
# machine busy with other builds has added 0.7 s
CUT_OFF_ALLOWANCE = 1.0


def patient() -> TmuxOperation:
    return TmuxOperation(deadline=PATIENT_DEADLINE)


class Command(NamedTuple):
    "One command a deadline test's operation sent: the clock reading its timeout was worked out from, the timeout, and whether it was cut off at it"
    at: float
    timeout: float
    cut_off: bool


class Sent:
    "The runner and the clock a deadline test gives its operation: it keeps each Command it was asked to send"

    def __init__(self, run: Runner, clock: Callable[[], float] = time.monotonic) -> None:
        self.run, self.clock = run, clock
        self.read_at = clock()
        self.commands: list[Command] = []

    def now(self) -> float:
        self.read_at = self.clock()
        return self.read_at

    def overspent(self, op: TmuxOperation) -> list[Command]:
        "The commands that started at or past op's deadline, or were given other than exactly the time left to it"
        return [c for c in self.commands if c.at >= op.expires_at or c.timeout != op.expires_at - c.at]

    def __call__(self, argv: Sequence[str], env: Mapping[str, str], timeout: float) -> str:
        # the operation reads its clock, then sends with what is left: the last reading is this command's
        at, cut_off = self.read_at, False
        try:
            return self.run(argv, env, timeout)
        except TmuxError as e:
            cut_off = e.timed_out
            raise
        finally:
            self.commands.append(Command(at, timeout, cut_off))


def default_query(text: str) -> FindQuery:
    "text as a query in the default mode: tmux and Claude Code search by their own rules"
    return FindQuery(text, FindMode())


def proc(pid: int, *cmdline: str) -> ProcessDesc:
    return {'pid': pid, 'cwd': None, 'cmdline': list(cmdline)}


def holding(state: TmuxFindState, op: TmuxOperation | None = None) -> TmuxFind:
    f = TmuxFind.holding(state, op or patient())
    assert f is not None, 'the window holds no tmux search'
    return f


def held_pane(state: TmuxFindState) -> str:
    assert state.held is not None, 'the window holds no tmux search'
    return state.held.pane


class TmuxFindPureTest(BaseTest):
    "What is sent to tmux and what is read back from it, without a tmux"

    def test_server_args(self):
        for args, expected in {
            (): (),
            ('new', '-s', 'x'): (),
            ('-L', 'work'): ('-L', 'work'),
            ('-Lwork', 'attach'): ('-L', 'work'),
            ('-S', '/tmp/sock', 'attach', '-L', 'ignored'): ('-S', '/tmp/sock'),
            ('-2u', '-f', '/my/tmux.conf', '-L', 'a'): ('-L', 'a'),
            ('-f', '-L', 'a'): (),  # -L is the value of -f
            ('-uSpath', 'a'): ('-S', '/work/path'),
            ('-S', './sock', 'attach'): ('-S', '/work/./sock'),  # the client's cwd, not kitty's, is what a relative socket is relative to
        }.items():
            self.ae(server_args_of(args, '/work'), expected, args)

    def test_tmux_client_of(self):
        env = {'TMUX_TMPDIR': '/var/t', 'PATH': '/opt/bin'}

        def of(*procs):
            return tmux_client_of(procs, lambda pid: env)

        self.ae(of(proc(5, 'tmux', 'attach')), TmuxClient(5, 'tmux', (), '/var/t', '/opt/bin'))
        self.ae(of(proc(5, '/opt/homebrew/bin/tmux', '-L', 'w', 'a')), TmuxClient(5, '/opt/homebrew/bin/tmux', ('-L', 'w'), '/var/t', '/opt/bin'))
        for not_tmux in (proc(5, 'vim', 'tmux'), proc(5, 'zsh'), proc(5, '/usr/bin/tmuxinator', 'start'), proc(5)):
            self.assertIsNone(of(not_tmux), not_tmux)
        self.assertIsNone(of())
        # the lowest pid among tmux processes, and an unreadable environment is no environment
        self.ae(of(proc(9, 'tmux'), proc(7, 'tmux')).pid, 7)

        def boom(pid):
            raise PermissionError

        self.ae(tmux_client_of([proc(5, 'tmux')], boom), TmuxClient(5, 'tmux', (), None, None))

    def test_parsing(self):
        # the version is passed on as the server wrote it: a pane on a server find cannot use is still a pane
        out = '12 3.5a %3 0 0 400 w:@1.%3\n99 openbsd-7.6 %7 1 1 500 a session:@4.%7\n'
        self.ae(parse_pane(out, 99), ClientPane(PaneRef('%7', True, True, 500, 'a session:@4.%7'), 'openbsd-7.6'))
        self.ae(parse_pane(out, 12), ClientPane(PaneRef('%3', False, False, 400, 'w:@1.%3'), '3.5a'))
        self.ae(parse_pane('12  %3 0 0 400 w:@1.%3', 12).version, '', 'a server too old to expand #{version}')
        self.ae(parse_displayed_pane('%3 0 1 400 w:@1.%3\n'), PaneRef('%3', False, True, 400, 'w:@1.%3'))
        for bad in ('', '12 3.5 %3 0 0 400', '12 3.5 %3 2 0 400 w:@1.%3', '12 3.5 %3 0 0 x w:@1.%3'):
            with self.assertRaises(TmuxError, msg=bad):
                parse_pane(bad, 12)
        for bad in ('', '%3 0 1 400', '%3 0 1 x w:@1.%3', '%3 2 1 400 w:@1.%3'):
            with self.assertRaises(TmuxError, msg=bad):
                parse_displayed_pane(bad)
        with self.assertRaises(TmuxError):
            parse_pane(out, 5)
        with self.assertRaises(TmuxError):
            parse_pane('', 5)
        for text, version in (
            ('3.5a', (3, 5, True)), ('3.6', (3, 6, True)), ('3.6-rc2', (3, 6, True)), ('3.10', (3, 10, True)), ('next-3.7', (3, 7, False)),
        ):
            self.ae(find_version(text), TmuxVersion(*version), text)
        # next-3.6 is code on the way to 3.6, which may not parse copy-mode options yet
        self.assertLess(find_version('next-3.6'), COPY_MODE_OPTIONS)
        # unknown, and known but older, are one answer that names what find needs and what the server said
        for bad in ('', '3', 'master', 'openbsd-7.6', '3.5 ', 'tmux 3.5', '3.4', 'next-3.5'):
            with self.assertRaisesRegex(TmuxError, f'needs tmux 3.5 or later.*{re.escape(repr(bad))}', msg=bad):
                find_version(bad)
        self.ae(parse_count('1|111|0\n'), Counted(True, 111, False))
        self.ae(parse_count('1|42|1'), Counted(True, 42, True))
        self.ae(parse_count('0||\n'), Counted(False, None, False))
        self.ae(parse_count('1||'), Counted(True, None, False))
        for bad in ('', 'x', '1|2', '1|a|0', 'error'):
            with self.assertRaises(TmuxError, msg=bad):
                parse_count(bad)
        self.ae(parse_selection('need\n'), 'need')
        self.ae(parse_selection('need le'), 'need le')
        for nothing in ('', '\n', 'a\nb', 'a\nb\n'):
            self.assertIsNone(parse_selection(nothing), nothing)

    def test_a_server_with_no_match_count_is_unavailable_and_never_searched(self):
        fake = FakeTmux()
        fake.version = '3.4'
        state = TmuxFindState()
        f = TmuxFind(TmuxClient(fake.client_pid, '/bin/sh', (), None, None), state, TmuxOperation(fake))
        f.find(default_query('needle'))
        self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))
        self.assertIsNone(state.held)
        self.assertIsNone(f.program_selection_text())
        self.ae([c for c in fake.calls if 'list-clients' not in c], [])
        # the pane is still known, so routing to Claude's transcript search does not depend on the version
        self.ae(f.lookup(), PaneRef('%1', True, False, 4242, 's:@1.%1'))

    def test_commands(self):
        # a semicolon ends a tmux command unless it is escaped, and an option cannot be told from a query
        for text, arg in {'foo': 'foo', 'foo;': 'foo\\;', ';': '\\;', 'a;b': 'a;b', '-n': '-n', '#{x}': '#{x}', 'é日': 'é日', '': ''}.items():
            self.ae(query_argument(text), arg, text)
        self.ae(find_command('%1', 'foo;'), ['send', '-t', '%1', '-X', 'search-backward-incremental', '=foo\\;'])
        # from 3.6 tmux parses a copy-mode command's options, and before it takes every argument as text
        self.ae(step_command('%1', '-n', False, TmuxVersion(3, 6, True)), ['send', '-t', '%1', '-X', 'search-forward-text', '--', '-n'])
        self.ae(step_command('%1', '-n', True, TmuxVersion(3, 8, True))[4:], ['search-backward-text', '--', '-n'])
        self.ae(step_command('%1', '-n', True, TmuxVersion(3, 5, True))[4:], ['search-backward-text', '-n'])
        self.ae(step_command('%1', '-n', True, TmuxVersion(3, 6, False))[4:], ['search-backward-text', '-n'])
        self.ae(batch(['a', 'b'], ['c']), ['a', 'b', ';', 'c'])
        self.ae(batch(['a']), ['a'])
        b = search_batch('%1', ['SEARCH'])
        self.ae(b[0:3], ['copy-mode', '-t', '%1'])
        self.assertLess(b.index('if-shell'), b.index('SEARCH'))
        self.assertLess(b.index('SEARCH'), b.index('display-message'))
        self.ae(b.count(';'), 3)
        self.assertIn('send -t %1 -X cancel', cancel_batch('%1'))


# A tmux server of our own with a real client attached on a pty, so that pane lookup runs through list-clients
class PrivateTmux:
    def __init__(self, directory: str) -> None:
        assert TMUX
        self.exe: str = TMUX
        self.socket = os.path.join(directory, 'sock')
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 100, 0, 0))
        self.output = bytearray()
        self.slave = slave
        # a client needs a terminal type, and wide characters a server that knows it speaks UTF-8
        self.env = {**os.environ, 'TERM': 'xterm-256color', 'LC_ALL': 'en_US.UTF-8'}
        self.client_proc: subprocess.Popen | None = None
        threading.Thread(target=self.drain, daemon=True).start()

    def drain(self) -> None:
        with suppress(OSError):
            while data := os.read(self.master, 65536):
                self.output += data

    def tmux(self, *args: str, check: bool = True) -> str:
        cp = subprocess.run([self.exe, '-u', '-S', self.socket, *args], capture_output=True, text=True, timeout=10, env=self.env)
        if check and cp.returncode:
            raise AssertionError(f'tmux {args} failed: {cp.stderr}')
        return cp.stdout.strip('\n')

    def start(self, script: str, history_limit: int = 5000) -> None:
        self.tmux('-f', '/dev/null', 'new-session', '-d', '-x', '100', '-y', '23', '-s', 'p', f'sh -c {script!r}')
        for opt, val in (('mode-keys', 'vi'), ('history-limit', str(history_limit)), ('set-clipboard', 'on'), ('status', 'off')):
            self.tmux('set', '-g', opt, val)
        self.client_proc = subprocess.Popen(
            [self.exe, '-u', '-S', self.socket, 'attach', '-t', 'p'],
            stdin=self.slave, stdout=self.slave, stderr=self.slave, start_new_session=True, env=self.env,
        )
        deadline = time.monotonic() + 10
        while not self.tmux('list-clients', check=False) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.pane = self.tmux('display', '-p', '-t', 'p', '#{pane_id}')

    def drawn(self, script: 'Script', pane: str | None = None) -> None:
        deadline = time.monotonic() + 10
        while script.last not in self.tmux('capture-pane', '-p', '-S', '-', '-t', pane or self.pane):
            if time.monotonic() > deadline:  # the launcher runs python -O, which drops assert statements
                raise AssertionError(f'the pane never printed {script.last!r}')
            time.sleep(0.02)

    def fmt(self, f: str, pane: str | None = None) -> str:
        return self.tmux('display', '-p', '-t', pane or self.pane, f)

    def stop(self) -> None:
        # the private server exits with its only session
        with suppress(Exception):
            self.tmux('kill-session', '-t', 'p', check=False)
        if self.client_proc is not None:
            with suppress(Exception):
                self.client_proc.kill()
                self.client_proc.wait(5)
        os.close(self.slave)
        with suppress(OSError):
            os.close(self.master)

    def client(self) -> TmuxClient:
        assert self.client_proc is not None
        return TmuxClient(self.client_proc.pid, self.exe, ('-S', self.socket), None, os.environ.get('PATH'))

    def finder(self, state: TmuxFindState, op: TmuxOperation | None = None) -> TmuxFind:
        return TmuxFind(self.client(), state, op or patient())


class Script(NamedTuple):
    "What the pane runs, and the last line it prints before it waits: a search before that line is drawn searches part of it"
    command: str
    last: str


@contextmanager
def private_tmux(script: Script, **kw: Any) -> Generator[PrivateTmux, None, None]:
    with tempfile.TemporaryDirectory(prefix='kitty-find-test-') as d:
        t = PrivateTmux(d)
        try:
            t.start(script.command, **kw)
            t.drawn(script)
            yield t
        finally:
            t.stop()


NEEDLES = Script('i=1; while [ $i -le 300 ]; do echo "line $i needle-$i"; i=$((i+1)); done; sleep 600', 'line 300 needle-300')
AWKWARD = Script(
    'for i in 1 2 3; do echo "alpha foo; x$i"; done; for i in 1 2 3 4 5 6 7; do echo "alpha foo y$i"; done;'
    ' for i in 1 2 3; do echo "beta -n z$i"; done; echo "back\\\\slash"; echo "日本語テキスト and é"; sleep 600',
    '日本語テキスト and é',
)


class TmuxFindIntegrationTest(BaseTest):
    "The batches the bar sends, run against a real tmux server and its attached client"

    def setUp(self) -> None:
        super().setUp()
        if not TMUX:
            self.skipTest('tmux is not installed')

    def in_mode(self, t: PrivateTmux, pane: str | None = None) -> bool:
        return t.fmt('#{pane_in_mode}', pane) == '1'

    def test_find_step_clear(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            f = t.finder(state)
            f.find(default_query('needle-5'))
            self.ae(state.status, FindStatus(0, 11, FindCount.exact))
            self.assertTrue(self.in_mode(t))
            self.assertTrue(f.searching())
            # the incremental search lands on the match nearest below the end of the buffer
            line = t.fmt('#{copy_cursor_line}')
            self.assertIn('needle-5', line)
            # later in the buffer is forward and earlier is backward, and both wrap
            n = lambda: int(t.fmt('#{copy_cursor_line}').split()[1])  # noqa: E731
            at = n()
            t.finder(state).step(default_query('needle-5'), True)
            self.assertLess(n(), at)
            t.finder(state).step(default_query('needle-5'), False)
            self.ae(n(), at)
            self.ae(state.status, FindStatus(0, 11, FindCount.exact))
            for _ in range(11):
                t.finder(state).step(default_query('needle-5'), False)
            self.ae(n(), at, 'eleven matches later is the same match: the search wraps')
            self.ae(state.status.total, 11)
            holding(state).clear()
            self.assertFalse(self.in_mode(t))
            self.assertIsNone(state.held)
            self.ae(state.status, FindStatus(0, 0, FindCount.exact))
            self.assertIsNone(TmuxFind.holding(state, TmuxOperation()))

    def test_no_match_is_zero_not_unavailable(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            t.finder(state).find(default_query('no-such-text'))
            self.ae(state.status, FindStatus(0, 0, FindCount.exact))
            t.finder(state).find(default_query('needle-29'))
            self.ae(state.status.total, 11)

    def test_awkward_queries_search_what_was_typed(self):
        with private_tmux(AWKWARD) as t:
            counts = {'foo;': 3, 'foo': 10, ';': 3, '-n': 3, '--': 0, 'z1': 1, '=': 0, 'back\\slash': 1, '#{pane_id}': 0, '日本語': 1, 'é': 1, '"': 0, "'": 0}
            for query, total in counts.items():
                state = TmuxFindState()
                f = t.finder(state)
                f.find(default_query(query))
                self.ae(state.status, FindStatus(0, total, FindCount.exact), query)
                # stepping with the same awkward text finds the same matches
                t.finder(state).step(default_query(query), False)
                self.ae(state.status.total, total, query)
                holding(state).clear()
            # the match is where the text is
            state = TmuxFindState()
            t.finder(state).find(default_query('foo;'))
            self.assertIn('foo; x', t.fmt('#{copy_cursor_line}'))
            t.finder(state).find(default_query('-n'))
            self.assertIn('beta -n', t.fmt('#{copy_cursor_line}'))

    def test_empty_query_clears(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            t.finder(state).find(default_query('needle'))
            self.assertTrue(self.in_mode(t))
            t.finder(state).find(default_query(''))
            self.assertFalse(self.in_mode(t))
            self.assertIsNone(state.held)

    def test_leaving_copy_mode_by_hand_is_entered_again(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            t.finder(state).find(default_query('needle-5'))
            t.tmux('send', '-t', t.pane, '-X', 'cancel')
            self.assertFalse(self.in_mode(t))
            t.finder(state).step(default_query('needle-5'), True)
            self.assertTrue(self.in_mode(t))
            self.ae(state.status, FindStatus(0, 11, FindCount.exact))

    def test_one_pane_at_a_time(self):
        with private_tmux(NEEDLES) as t:
            a = t.pane
            t.tmux('split-window', '-d', '-t', a, 'sh -c ' + repr(NEEDLES.command))
            b = [p for p in t.tmux('list-panes', '-F', '#{pane_id}').split() if p != a][0]
            t.drawn(NEEDLES, b)
            state = TmuxFindState()
            t.finder(state).find(default_query('needle-5'))
            self.assertTrue(self.in_mode(t, a))
            self.ae(held_pane(state), a)
            # the user switches pane inside tmux: the client's active pane is now the other one
            t.tmux('select-pane', '-t', b)
            time.sleep(0.2)
            t.finder(state).find(default_query('needle-5'))
            self.assertFalse(self.in_mode(t, a), 'the pane the search left is out of copy mode')
            self.assertTrue(self.in_mode(t, b))
            self.ae(held_pane(state), b)
            holding(state).clear()
            self.assertFalse(self.in_mode(t, a) or self.in_mode(t, b))

    def test_live_selection_is_not_stretched_by_the_search(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            t.finder(state).find(default_query('needle-5'))
            t.tmux('send', '-t', t.pane, '-X', 'begin-selection')
            for _ in range(5):
                t.tmux('send', '-t', t.pane, '-X', 'cursor-right')
            self.assertEqual(t.fmt('#{selection_present}'), '1')
            t.finder(state).find(default_query('needle-2'))
            self.ae(t.fmt('#{selection_present}'), '0')
            self.ae(t.fmt('#{selection_active}'), '0')
            self.ae(state.status, FindStatus(0, 111, FindCount.exact))
            # a new drag, then a step of the unchanged query: tmux withholds the count, the state has it
            t.tmux('send', '-t', t.pane, '-X', 'begin-selection')
            t.tmux('send', '-t', t.pane, '-X', 'cursor-right')
            t.finder(state).step(default_query('needle-2'), False)
            self.ae(t.fmt('#{selection_present}'), '0')
            self.ae(state.status, FindStatus(0, 111, FindCount.exact))
            t.tmux('send', '-t', t.pane, '-X', 'begin-selection')
            t.finder(state).step(default_query('needle-2'), True)
            self.ae(state.status, FindStatus(0, 111, FindCount.exact))

    def selection_made(self, t: PrivateTmux, search: str, right: int = 5, left: bool = False) -> None:
        t.tmux('copy-mode', '-t', t.pane)
        t.tmux('send', '-t', t.pane, '-X', 'search-backward-text', search)
        t.tmux('send', '-t', t.pane, '-X', 'begin-selection')
        for _ in range(right):
            t.tmux('send', '-t', t.pane, '-X', 'cursor-left' if left else 'cursor-right')

    def test_selection_text(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            f = lambda: t.finder(state).program_selection_text()  # noqa: E731
            self.assertIsNone(f(), 'a pane not in a mode has no selection')
            t.tmux('copy-mode', '-t', t.pane)
            self.assertIsNone(f(), 'copy mode without a selection')
            self.selection_made(t, 'needle-59')
            self.ae(f(), 'needle')
            self.ae(f(), 'needle', 'reading it leaves it')
            self.ae(t.fmt('#{selection_present}'), '1')
            self.ae(t.tmux('list-buffers'), '', 'no paste buffer was made')
            t.tmux('send', '-t', t.pane, '-X', 'clear-selection')
            self.selection_made(t, 'needle-59', right=4, left=True)
            self.ae(len(f() or ''), 5, 'a selection made backward')
            t.tmux('send', '-t', t.pane, '-X', 'cursor-down')
            self.assertIsNone(f(), 'a selection over two rows is no one-line query')
            self.ae(t.tmux('list-buffers'), '')
            # nothing was written for the terminal's clipboard either: tmux does that as an OSC 52 to the client
            time.sleep(0.2)
            self.assertNotIn(b'\x1b]52', bytes(t.output))
            self.assertIsNone(TmuxFind.holding(state, TmuxOperation()), 'reading a selection holds no search')

    def test_selection_text_of_wide_characters(self):
        with private_tmux(AWKWARD) as t:
            state = TmuxFindState()
            t.tmux('copy-mode', '-t', t.pane)
            t.tmux('send', '-t', t.pane, '-X', 'search-backward-text', '日本語')
            t.tmux('send', '-t', t.pane, '-X', 'begin-selection')
            for _ in range(5):
                t.tmux('send', '-t', t.pane, '-X', 'cursor-right')
            # the character under the cursor and five to its right
            self.ae(t.finder(state).program_selection_text(), '日本語テキス')

    def test_selection_text_on_a_pane_that_keeps_printing(self):
        # rows of a pane move under tmux's coordinates as it prints and evicts: the text is tmux's own
        script = (
            'i=1; while [ $i -le 400 ]; do echo "row $i needle-$i"; i=$((i+1)); done; sleep 1;'
            ' j=0; while [ $j -lt 2000 ]; do echo "stream $j"; j=$((j+1)); sleep 0.01; done; sleep 600'
        )
        with private_tmux(Script(script, 'row 400 needle-400'), history_limit=60) as t:
            state = TmuxFindState()
            t.tmux('copy-mode', '-t', t.pane)
            t.tmux('send', '-t', t.pane, '-X', 'search-backward-text', 'row ')
            t.tmux('send', '-t', t.pane, '-X', 'begin-selection')
            for _ in range(5):
                t.tmux('send', '-t', t.pane, '-X', 'cursor-right')
            expected = t.finder(state).program_selection_text()
            self.assertTrue(expected and expected.startswith('row '))
            for _ in range(30):
                time.sleep(0.05)
                self.ae(t.finder(state).program_selection_text(), expected)
            # from 3.6 scrolling leaves the selection where it is; in 3.5 it can drag the cursor and stretch the
            # selection over more rows, which is no single-line query. Either way what is read is what tmux copies,
            # compared before parsing so that two selections no query is made from are still told apart.
            stays = find_version(t.tmux('display', '-p', '#{version}').strip()) >= COPY_MODE_OPTIONS
            for _ in range(5):
                t.tmux('send', '-t', t.pane, '-X', 'scroll-up')
                t.tmux('send', '-t', t.pane, '-X', 'copy-selection-no-clear')
                copied = t.tmux('show-buffer')
                t.tmux('delete-buffer')
                if stays:
                    self.ae(copied, expected)
                self.ae(t.finder(state).read_selection(t.pane), copied)

    def assert_ended_by_its_deadline(self, op: TmuxOperation, sent: Sent) -> None:
        """Each command was given the time left, the deadline is what ended the operation, and tmux honoured it: the
        operation returned at its deadline, give or take killing the command it cut off"""
        returned_at = op.now()
        self.ae(sent.overspent(op), [])
        self.assertTrue(sent.commands and sent.commands[-1].cut_off, f'the last command was not cut off at the deadline: {sent.commands}')
        self.assertLessEqual(returned_at, op.expires_at + CUT_OFF_ALLOWANCE)

    def test_a_pipe_that_cannot_run_ends_in_the_deadline_with_a_typed_error(self):
        with private_tmux(NEEDLES) as t:
            self.selection_made(t, 'needle-59')
            state = TmuxFindState()
            client = t.client()

            def run(argv, env, timeout):
                # the real tmux, but the job that would signal the channel is told to do something else
                argv = [a.replace('wait-for -S', 'display-message -p') if 'wait-for -S' in a else a for a in argv]
                return tf.run_tmux(argv, env, timeout)

            sent = Sent(run)
            op = TmuxOperation(sent, now=sent.now)
            self.assertIsNone(TmuxFind(client, state, op).program_selection_text())
            self.assert_ended_by_its_deadline(op, sent)
            # and the next read works
            self.ae(t.finder(TmuxFindState()).program_selection_text(), 'needle')

    def test_a_wedged_server_ends_in_the_deadline_and_the_pane_is_cancelled_when_it_wakes(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            t.finder(state).find(default_query('needle-5'))
            server = int(t.fmt('#{pid}'))
            os.kill(server, signal.SIGSTOP)
            try:
                sent = Sent(tf.run_tmux)
                op = TmuxOperation(sent, now=sent.now)
                t.finder(state, op).step(default_query('needle-5'), False)
                self.assert_ended_by_its_deadline(op, sent)
                self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))
                self.assertIsNotNone(state.held, 'the pane is on record')
                holding(state, TmuxOperation()).clear()
                self.assertIsNotNone(state.held, 'a clear that timed out is tried again')
            finally:
                os.kill(server, signal.SIGCONT)
            time.sleep(0.3)
            holding(state).clear()
            self.assertIsNone(state.held)
            self.assertFalse(self.in_mode(t))

    def test_find_in_a_pane_that_is_gone_is_unavailable_and_loud(self):
        with private_tmux(NEEDLES) as t:
            state = TmuxFindState()
            client = t.client()
            dead = TmuxClient(2**22 + 5, client.exe, client.server_args, None, client.path)
            TmuxFind(dead, state, patient()).find(default_query('needle'))
            self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))
            self.assertIsNone(state.held, 'no pane was reached')


class FakeTmux:
    "Stands in for the one effect: answers what the bar's batches ask, and records what was sent"

    def __init__(self, pane: str = '%1', counted: str = '1|5|0', selection: str = 'sel') -> None:
        self.pane, self.counted, self.selection = pane, counted, selection
        self.calls: list[list[str]] = []
        self.client_pid = 77
        self.timeout_cancel = False
        self.unreachable = False
        # how list-clients describes the pane: in copy mode, on the main screen, its process
        self.in_mode, self.alternate, self.pane_pid = '1', '0', 4242
        self.version = '3.8'
        # the server's paste buffers: (created, name, text)
        self.buffers: list[tuple[int, str, str]] = []

    def __call__(self, argv: Any, env: Any, timeout: float) -> str:
        self.calls.append(list(argv))
        if self.unreachable:
            raise TmuxError('no server running')
        if 'list-clients' in argv:
            return f'{self.client_pid} {self.version} {self.pane} {self.in_mode} {self.alternate} {self.pane_pid} s:@1.{self.pane}\n'
        if 'list-buffers' in argv:
            return ''.join(f'{created} {name}\n' for created, name, _ in self.buffers)
        if 'show-buffer' in argv:
            return next(text for _, name, text in self.buffers if name == argv[argv.index('-b') + 1])
        if 'display-message' in argv:
            return self.counted + '\n'
        if 'pipe-no-clear' in argv:
            out = argv[argv.index('pipe-no-clear') + 1].split('>')[1].split(';')[0].strip().strip("'")
            with open(out, 'w') as f:
                f.write(self.selection)
        if self.timeout_cancel and any('send -t' in a and 'cancel' in a for a in argv):
            raise TmuxError('no answer', timed_out=True)
        return ''

    def cancelled(self) -> list[list[str]]:
        return [c for c in self.calls if any('-X cancel' in a for a in c)]


class TmuxFindWindowTest(BaseTest):
    "The window's find, with the foreground changing under a search"

    def setUp(self) -> None:
        super().setUp()
        if not is_macos:
            self.skipTest('The Rust find engine is linked on macOS only')
        self.dir = tempfile.TemporaryDirectory()
        self.tmux_exe = os.path.join(self.dir.name, 'tmux')
        with open(self.tmux_exe, 'w') as f:
            f.write('#!/bin/sh\n')
        os.chmod(self.tmux_exe, 0o755)
        self.addCleanup(self.dir.cleanup)

    def make_window(self, s: Any, fake: FakeTmux) -> SimpleNamespace:
        foreground: list[dict[str, Any]] = []
        w: Any = SimpleNamespace(
            screen=s, find_query='', os_window_id=1, text_for_selection=lambda: s.callbacks.kitty_selection, geometry=None,
            child=SimpleNamespace(foreground_processes=foreground), tmux_find_state=TmuxFindState(), claude_find_state=ClaudeFindState(),
            claude_host=ClaudeHost(), find_reporting=None,
            finish_scroll_animation=lambda: None, scroll_animation=SimpleNamespace(timer=0), find_bar_top_inset=lambda: 0, find_mode=lambda: FindMode(),
            pending_find_timer=0,
        )
        w.foreground = foreground
        w.engine_find = EngineFind(w)
        for name in (
            'tmux_client', 'find_pane_backend', 'find_status', 'end_pane_finds', 'find_backend', 'find_holder', 'end_finds', 'find_backend_to_search',
            'find_step', 'set_find_query', 'find', 'push_find_bar',
            'find_selection_text', 'find_clear', 'find_status_changed', 'use_selection_for_find', 'settle_find', 'cancel_pending_find',
            'find_backend_for', 'selection_text_in', 'begin_find_in',
        ):
            setattr(w, name, getattr(Window, name).__get__(w))
        w.pushed = []
        tab_manager = SimpleNamespace(active_window=w)
        boss = SimpleNamespace(os_window_map={1: tab_manager}, find_bar_window=lambda osw: tab_manager.active_window)
        patches = [
            mock.patch.object(window_module, 'is_macos', True),
            mock.patch.object(window_module, 'get_boss', lambda: boss),
            mock.patch.object(window_module, 'cocoa_update_find_bar', lambda *a: w.pushed.append(a)),
            mock.patch.object(tf, 'run_tmux', fake),
            mock.patch.object(tf, 'environ_of_process', lambda pid: {}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        w.boss = boss
        return w

    def run_in_tmux(self, w: SimpleNamespace, pid: int = 77) -> None:
        w.foreground[:] = [{'pid': pid, 'cwd': None, 'cmdline': [self.tmux_exe, '-S', '/x/sock', 'attach']}]

    def screen_with_matches(self) -> Any:
        s = self.create_screen(cols=10, lines=4, scrollback=20)
        s.callbacks.kitty_selection = ''
        for t in ('x0', 'x1', 'x2', 'x3', 'x4'):
            s.draw(t), s.carriage_return(), s.linefeed()
        return s

    def reports(self, s: Any) -> int:
        before = s.callbacks.find_status_changes
        s.find_report_status_change()
        return s.callbacks.find_status_changes - before

    def test_a_bare_window_searches_in_the_engine(self):
        s = self.screen_with_matches()
        fake = FakeTmux()
        w = self.make_window(s, fake)
        w.find('x')
        self.ae(s.find_status()[1], 5)
        current = s.find_status()[0]
        self.assertGreater(current, 0)
        self.ae(w.pushed[-1][1:5], ('x', current, 5, 0))
        self.ae(fake.calls, [], 'tmux is not asked about a window that does not run it')
        self.assertIsNone(w.find_step(False))
        w.find_clear()
        self.ae(s.find_status()[1], 0)
        self.ae(w.pushed[-1][3:5], (0, 0))

    def test_tmux_starting_under_an_engine_search_takes_it_over(self):
        s = self.screen_with_matches()
        fake = FakeTmux(counted='1|42|0')
        w = self.make_window(s, fake)
        w.find('x')
        self.ae(self.reports(s), 0, 'the bar has been told')
        self.run_in_tmux(w)
        w.find('x')
        self.ae(s.find_status()[1], 0, 'the engine search ended: a window holds one search')
        self.ae(w.pushed[-1][1:5], ('x', 0, 42, 0), 'the bar shows what tmux found')
        # the screen's record of what the bar was told moved with it, so no frame keeps reporting the old total
        self.ae(self.reports(s), 0)
        self.ae(self.reports(s), 0)
        self.assertTrue(w.find_holder().searching())
        self.assertIsNot(w.find_holder(), w.engine_find)

    def test_tmux_ending_under_its_search_still_cancels_it(self):
        s = self.screen_with_matches()
        fake = FakeTmux()
        w = self.make_window(s, fake)
        self.run_in_tmux(w)
        w.find('x')
        self.ae(fake.cancelled(), [])
        w.foreground[:] = []  # tmux exited, or the shell has a different program now
        w.find_clear()
        self.ae(len(fake.cancelled()), 1, 'the pane this window put in copy mode is cancelled through the client it was reached with')
        self.assertIn('%1', ' '.join(fake.cancelled()[0]))
        self.assertIsNone(w.tmux_find_state.held)
        self.ae(w.pushed[-1][3:5], (0, 0))

    def test_an_engine_search_after_a_clear_that_timed_out_is_what_the_bar_shows(self):
        s = self.screen_with_matches()
        fake = FakeTmux()
        w = self.make_window(s, fake)
        self.run_in_tmux(w)
        w.find('x')
        fake.timeout_cancel = True
        w.foreground[:] = []
        w.find('x')  # the engine searches; tmux's pane could not be cancelled and stays on record
        self.assertIsNotNone(w.tmux_find_state.held)
        self.ae(s.find_status()[1], 5)
        self.assertIs(w.find_holder(), w.engine_find)
        self.ae(w.pushed[-1][1:5], ('x', s.find_status()[0], 5, 0), 'the engine\'s matches are painted, so the bar says so')

    def test_a_search_that_never_reached_a_pane_tells_the_bar_it_is_unavailable_until_it_ends(self):
        s = self.screen_with_matches()
        fake = FakeTmux()
        fake.unreachable = True
        w = self.make_window(s, fake)
        self.run_in_tmux(w)
        w.find('x')
        self.assertIsNone(w.tmux_find_state.held, 'no pane was reached')
        self.ae(w.pushed[-1][1:5], ('x', 0, 0, int(FindCount.unavailable)), 'not 0 matches: the search could not be made')
        w.find_clear()
        self.ae(w.pushed[-1][3:5], (0, int(FindCount.exact)))
        self.ae(w.tmux_find_state.status, FindStatus(0, 0, FindCount.exact))

    def test_closing_a_window_that_holds_a_tmux_search_cancels_it(self):
        s = self.screen_with_matches()
        fake = FakeTmux()
        w = self.make_window(s, fake)
        self.run_in_tmux(w)
        w.find('x')
        closing: Any = SimpleNamespace(
            tmux_find_state=w.tmux_find_state, claude_find_state=w.claude_find_state, claude_host=w.claude_host,
            call_watchers=lambda *a: None, watchers=SimpleNamespace(on_close=None), id=1,
            clear_progress_timer=0, cancel_pending_find=lambda: None, clipboard_request_manager=SimpleNamespace(close=lambda: None),
            kitten_result_processors=[],
        )
        closing.end_pane_finds = Window.end_pane_finds.__get__(closing)
        Window.destroy(closing)
        self.ae(len(fake.cancelled()), 1)
        self.assertIsNone(w.tmux_find_state.held)

    def test_the_selection_cmd_e_seeds_from(self):
        s = self.screen_with_matches()
        fake = FakeTmux(selection='from tmux')
        w = self.make_window(s, fake)
        s.callbacks.kitty_selection = 'from kitty'
        self.ae(w.find_selection_text(), 'from kitty', 'a bare window uses kitty\'s selection')
        self.run_in_tmux(w)
        self.ae(w.find_selection_text(), 'from tmux', 'tmux\'s selection is the one the user made in a pane')
        fake.selection = ''
        self.ae(w.find_selection_text(), 'from kitty', 'with none in tmux, kitty\'s')
        s.callbacks.kitty_selection = ''
        self.assertIsNone(w.find_selection_text())
        # the menu asks the same function the key does
        has = Boss.find_has_selection.__get__(w.boss)
        self.assertFalse(has(1))
        fake.selection = 'from tmux'
        self.assertTrue(has(1))
        self.assertTrue(Boss.find_can_search.__get__(w.boss)(1))

    def test_the_menu_and_the_key_agree_on_the_alternate_screen(self):
        s = self.screen_with_matches()
        s.callbacks.kitty_selection = 'sel'
        w = self.make_window(s, FakeTmux())
        can = Boss.find_can_search.__get__(w.boss)
        s.toggle_alt_screen()
        self.assertFalse(can(1), 'a full-screen program in a bare window: the keys are the program\'s')
        self.assertTrue(w.find_step(False))
        self.assertTrue(w.use_selection_for_find(), 'the key goes to the program')
        self.assertFalse(Boss.find_has_selection.__get__(w.boss)(1))
        self.run_in_tmux(w)
        self.assertTrue(can(1), 'tmux draws on the alternate screen and can still be searched')
        self.assertIsNone(w.find_step(False))
        self.assertTrue(Boss.find_has_selection.__get__(w.boss)(1))
        self.assertIsNone(w.use_selection_for_find(), 'the selection seeds the search')

    # Which backend a tmux window's search goes to: Claude Code's transcript search or tmux's history search

    def claude_host_with(self, ps: str, session: dict[str, Any] | None = None) -> ClaudeHost:
        config = os.path.join(self.dir.name, 'claude')
        os.makedirs(os.path.join(config, 'sessions'), exist_ok=True)
        if session is not None:
            with open(os.path.join(config, 'sessions', 'one.json'), 'w') as f:
                json.dump(session, f)
        return ClaudeHost(ps=lambda timeout: ps, environ_of=lambda pid: {'CLAUDE_CONFIG_DIR': config})

    def routed(self, fake: FakeTmux, host: ClaudeHost) -> tuple[SimpleNamespace, Any]:
        w = self.make_window(self.screen_with_matches(), fake)
        w.claude_host = host
        self.run_in_tmux(w)
        return w, w.find_backend_to_search(TmuxOperation())

    def test_a_claude_pane_goes_to_claude_and_everything_else_to_tmux(self):
        claude = '4242 1 /Users/x/.local/bin/claude\n'
        session = {'pid': 4242, 'sessionId': 'sid', 'tmux': 's:@1.%1'}
        fake = FakeTmux()
        fake.in_mode, fake.alternate = '0', '1'
        w, backend = self.routed(fake, self.claude_host_with(claude, session))
        self.assertIsInstance(backend, ClaudeFind)
        self.ae(backend.pane.session_id, 'sid')
        for name, tweak, ps in (
            ('a pane in copy mode', lambda f: setattr(f, 'in_mode', '1'), claude),
            ('a main-screen pane', lambda f: setattr(f, 'alternate', '0'), claude),
            ('another full-screen program', lambda f: None, '4242 1 vim\n'),
            ('a tmux that cannot be asked', lambda f: setattr(f, 'unreachable', True), claude),
        ):
            fake = FakeTmux()
            fake.in_mode, fake.alternate = '0', '1'
            tweak(fake)
            _, backend = self.routed(fake, self.claude_host_with(ps, session))
            self.assertIsInstance(backend, TmuxFind, name)

    # Cmd+E in a pane running Claude Code: the drag is Claude's, copied by Claude when the button is released

    def claude_window(self, fake: FakeTmux) -> SimpleNamespace:
        fake.in_mode, fake.alternate = '0', '1'
        session = {'pid': 4242, 'sessionId': 'sid', 'tmux': 's:@1.%1'}
        w = self.make_window(self.screen_with_matches(), fake)
        w.claude_host = self.claude_host_with('4242 1 /Users/x/.local/bin/claude\n', session)
        w.on_mouse_event = Window.on_mouse_event.__get__(w)
        self.run_in_tmux(w)
        return w

    def release(self, w: SimpleNamespace, **kw: Any) -> None:
        w.on_mouse_event({'button': 0, 'repeat_count': -1, 'mods': 0, 'grabbed': True, **kw})

    def test_cmd_e_in_a_claude_pane_seeds_from_the_copy_of_the_drag(self):
        fake = FakeTmux()
        w = self.claude_window(fake)
        fake.buffers = [(1, 'buffer0', 'an old copy')]
        self.assertIsNone(w.find_selection_text(), 'no drag yet: an old copy is not his selection')
        self.release(w)
        fake.buffers.append((int(w.claude_find_state.released_at), 'buffer1', 'needle'))
        self.ae(w.find_selection_text(), 'needle')
        self.assertTrue(Boss.find_has_selection.__get__(w.boss)(1), 'the menu asks the same function')
        # a click that selects nothing: the copy of the drag before it is no longer his selection
        w.claude_find_state.released_at += 60
        self.assertIsNone(w.find_selection_text())
        self.assertTrue(w.use_selection_for_find(), 'nothing selected: the key goes to the program')

    def test_only_a_release_the_program_was_given_marks_a_drag(self):
        w = self.claude_window(FakeTmux())
        self.release(w, grabbed=False)  # kitty's own selection: its own text
        self.release(w, repeat_count=1)  # a press
        self.release(w, button=1)  # the right button
        self.assertIsNone(w.claude_find_state.released_at)
        self.release(w)
        self.assertIsNotNone(w.claude_find_state.released_at)

    def test_a_claude_pane_that_cannot_be_told_is_unavailable_and_never_searched_as_tmux(self):
        fake = FakeTmux()
        fake.in_mode, fake.alternate = '0', '1'
        w, backend = self.routed(fake, self.claude_host_with('4242 1 /x/claude\n', None))  # claude runs, no session file names it
        self.assertIsInstance(backend, ClaudeFind)
        w.find('x')
        self.ae(w.pushed[-1][3:5], (0, int(FindCount.unavailable)))
        self.assertFalse([c for c in fake.calls if 'copy-mode' in c], 'the visible rows of a full-screen program are not an answer')
        self.assertTrue(w.claude_find_state.reporting())

    def test_asking_whether_a_window_can_search_asks_tmux_nothing(self):
        fake = FakeTmux()
        w = self.make_window(self.screen_with_matches(), fake)
        self.run_in_tmux(w)
        self.assertTrue(Boss.find_can_search.__get__(w.boss)(1))
        self.assertIsInstance(w.find_backend(), TmuxFind)
        self.ae(fake.calls, [])

    def test_a_pane_that_changes_between_lookups_cannot_split_routing_from_searching(self):
        # routing read pane %1 (a main-screen pane: tmux's history); tmux then reports %2. The search must go to %1 and ask list-clients once.
        fake = FakeTmux(pane='%1')
        fake.in_mode, fake.alternate = '0', '0'
        w = self.make_window(self.screen_with_matches(), fake)
        self.run_in_tmux(w)
        backend = w.find_pane_backend(w.tmux_client(), TmuxOperation())
        fake.pane = '%2'
        backend.find(default_query('x'))
        self.ae(len([c for c in fake.calls if 'list-clients' in c]), 1)
        self.ae(w.tmux_find_state.held.pane, '%1')
        self.assertTrue(all('%2' not in arg for call in fake.calls for arg in call), 'nothing was sent to the pane tmux switched to')

    def test_a_lookup_that_failed_is_what_the_search_reports(self):
        fake = FakeTmux()
        fake.unreachable = True
        w = self.make_window(self.screen_with_matches(), fake)
        self.run_in_tmux(w)
        backend = w.find_pane_backend(w.tmux_client(), TmuxOperation())
        fake.unreachable = False
        backend.find(default_query('x'))
        self.ae(w.tmux_find_state.status, FindStatus(0, 0, FindCount.unavailable))
        self.ae(len([c for c in fake.calls if 'list-clients' in c]), 1)

    def test_the_lookup_the_clears_and_the_search_spend_one_budget(self):
        fake = FakeTmux()
        w = self.make_window(self.screen_with_matches(), fake)
        self.run_in_tmux(w)
        clock = [0.0]

        def run(argv, env, timeout):
            clock[0] += FIND_DEADLINE / 3 + 0.01
            return fake(argv, env, timeout)

        sent = Sent(run, clock=lambda: clock[0])
        op = TmuxOperation(sent, now=sent.now, sleep=lambda s: None)
        backend = w.find_backend_to_search(op)
        backend.find(default_query('x'))
        self.assertGreater(len(sent.commands), 1)
        self.ae(sent.overspent(op), [], 'each command is given the time left, and none starts after it is spent')
        self.assertLessEqual(len(sent.commands), 4, 'the budget ended the operation; it did not start a second one')
