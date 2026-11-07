#!/usr/bin/env python
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from typing import Any
from unittest import mock

from kitty import claude_find as cf
from kitty.claude_find import (
    COPY_LATENCY,
    Cause,
    ClaudeFind,
    ClaudeFindFailure,
    ClaudeFindState,
    ClaudeHost,
    ClaudePane,
    Hold,
    Key,
    NotClaude,
    NotYetWritten,
    PasteBuffer,
    Prompt,
    Searching,
    SearchInput,
    Transcript,
    Unrecognised,
    Written,
    newest_automatic_buffer,
    parse_view,
    recognise,
    refusal,
)
from kitty.find_backends import FindCount, FindMode, FindQuery, FindStatus
from kitty.tmux_find import FIND_DEADLINE, PaneRef, TmuxError, TmuxOperation, run_tmux

from .base import BaseTest
from .fake_claude import DIALOG as FAKE_DIALOG
from .tmux_find import PATIENT_DEADLINE, PrivateTmux, Script, Sent, patient, private_tmux

RULE = '─' * 100
PYTHON = shutil.which('python3')
FAKE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fake_claude.py')


def default_query(text: str) -> FindQuery:
    "text as a query in the default mode: tmux and Claude Code search by their own rules"
    return FindQuery(text, FindMode())


def screen(*rows: str) -> str:
    return '\n'.join(rows) + '\n'


# the views as Claude Code 2.1.299 draws them, trimmed to the rows that decide the view
PROMPT = screen('⏺ some reply', '', RULE, '❯ ', RULE, '   🍫   polled  ~/c/repo (⊫ main)', '   ✱ Haiku 5.5', '  ⏵⏵ auto mode on (shift+tab to cycle)', '', '')
HINTS = '  Showing detailed transcript · ctrl+o to toggle · ↑↓ scroll · v to open in code · ? for shortcuts'
TRANSCRIPT = screen('⏺ some reply', '', RULE, HINTS + ' ' * 14 + 'verbose', '', '')
SEARCHING = screen('⏺ some reply', RULE, '  Showing detailed transcript · ctrl+o to toggle · n/N to navigate' + ' ' * 40 + '304/304', '')
SEARCH_FIELD = screen('⏺ some reply', RULE, '  /ZEBRAFISH-ALPHA' + ' ' * 80 + '2/2', '')
EMPTY_FIELD = screen('⏺ some reply', RULE, '  /', '')
DIALOG = screen('Do you want to proceed?', '❯ 1. Yes', '  2. No', '')


class ViewTest(BaseTest):
    def test_views_are_recognised_by_what_they_show(self):
        self.ae(parse_view(PROMPT), Prompt())
        self.ae(parse_view(TRANSCRIPT), Transcript())
        self.ae(parse_view(SEARCHING), Searching(304, 304))
        self.ae(parse_view(SEARCH_FIELD), SearchInput('ZEBRAFISH-ALPHA'))
        self.ae(parse_view(EMPTY_FIELD), SearchInput(''))
        self.ae(parse_view(screen('x', RULE, '  /two words ' + ' ' * 60 + '1/9')), SearchInput('two words'))
        self.ae(parse_view(screen('x', RULE, '  /1/2')), SearchInput('1/2'), 'a query is not a count')
        self.ae(parse_view(screen('x', RULE, '  /quokka' + ' ' * 70 + 'no matches')), SearchInput('quokka'), 'the field says so when nothing matches')

    def test_a_screen_that_is_none_of_them_is_not_a_prompt(self):
        for shown in (DIALOG, '', '\n\n', screen('plain shell $ '), screen('x', RULE, '  something else')):
            self.assertIsInstance(parse_view(shown), Unrecognised, shown)
        # an accepted search whose footer has lost its count is not one the keys can be trusted on
        self.assertIsInstance(parse_view(screen('x', RULE, '  Showing detailed transcript · n/N to navigate')), Unrecognised)

    def test_a_query_is_compared_without_trailing_spaces_and_refused_when_it_cannot_be_checked(self):
        self.assertIsNone(refusal('foo'))
        self.assertIsNone(refusal('foo '))
        self.assertIsNone(refusal('日本語 é'))
        for bad in ('', ' ', '   ', 'a\x1bb', 'a\nb', 'a\x7f', '\t'):
            self.assertIsNotNone(refusal(bad), repr(bad))


def write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        f.write(data if isinstance(data, str) else json.dumps(data))


class RecogniserTest(BaseTest):
    def setUp(self) -> None:
        super().setUp()
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.config = self.dir.name
        self.table = '  500     1 -zsh\n  600   500 /Users/x/.local/bin/claude --dangerously-skip-permissions\n  700     1 vim notes.txt\n'

    def host(self, table: str | None = None) -> ClaudeHost:
        text = self.table if table is None else table
        return ClaudeHost(ps=lambda timeout: text, environ_of=lambda pid: {'CLAUDE_CONFIG_DIR': self.config})

    def session(self, name: str = '600', **kw: Any) -> None:
        write_json(os.path.join(self.config, 'sessions', name + '.json'), {'pid': 600, 'sessionId': 'sid-1', 'tmux': 's:@1.%5', **kw})

    def pane(self, **kw: Any) -> PaneRef:
        return PaneRef(**{'id': '%5', 'in_mode': False, 'alternate': True, 'pid': 500, 'address': 's:@1.%5', **kw})

    def test_a_pane_running_claude_is_named_by_its_session_file(self):
        self.session()
        self.ae(recognise(self.pane(), self.host(), TmuxOperation()), ClaudePane('%5', 'sid-1', 600, self.config))
        # claude exec'd by the pane's own shell is the pane's process
        self.ae(recognise(self.pane(pid=600), self.host(), TmuxOperation()), ClaudePane('%5', 'sid-1', 600, self.config))

    def test_a_pane_that_is_positively_not_claude(self):
        self.session()
        self.ae(recognise(self.pane(alternate=False), self.host(), TmuxOperation()), NotClaude(), 'main screen: tmux has its history')
        self.ae(recognise(self.pane(in_mode=True), self.host(), TmuxOperation()), NotClaude(), 'copy mode: keys would reach the mode')
        # a vim whose pane has the address of a session on another tmux server: the pid is not in its tree
        self.ae(recognise(self.pane(pid=700), self.host(), TmuxOperation()), NotClaude())
        self.ae(recognise(self.pane(pid=999), self.host(), TmuxOperation()), NotClaude(), 'a pane whose process is gone')

    def test_could_not_tell_is_never_not_claude(self):
        # the session file names another pane: claude is in the tree and no file explains it
        self.session(tmux='s:@1.%9')
        with self.assertRaises(ClaudeFindFailure) as cm:
            recognise(self.pane(), self.host(), TmuxOperation())
        self.ae(cm.exception.cause, Cause.undeterminable)
        # no sessions directory at all
        shutil.rmtree(os.path.join(self.config, 'sessions'))
        with self.assertRaises(ClaudeFindFailure):
            recognise(self.pane(), self.host(), TmuxOperation())
        # a file that is not JSON among the others is skipped; alone it explains nothing
        self.session('bad', **{})
        write_json(os.path.join(self.config, 'sessions', 'torn.json'), '{"pid": 6')
        write_json(os.path.join(self.config, 'sessions', '600.json'), {'pid': 600, 'sessionId': 'sid-1', 'tmux': 's:@1.%5'})
        self.ae(recognise(self.pane(), self.host(), TmuxOperation()), ClaudePane('%5', 'sid-1', 600, self.config))
        os.remove(os.path.join(self.config, 'sessions', '600.json'))
        os.remove(os.path.join(self.config, 'sessions', 'bad.json'))
        with self.assertRaises(ClaudeFindFailure):
            recognise(self.pane(), self.host(), TmuxOperation())

    def test_a_ps_that_fails_is_a_failure(self):
        def boom(timeout: float) -> str:
            raise ClaudeFindFailure(Cause.undeterminable, 'cannot list processes')

        with self.assertRaises(ClaudeFindFailure):
            recognise(self.pane(), ClaudeHost(ps=boom), TmuxOperation())

    def test_the_process_listing_spends_from_the_operations_budget(self):
        self.session()
        asked: list[float] = []
        clock = [0.0]

        def ps(timeout: float) -> str:
            asked.append(timeout)
            return self.table

        op = TmuxOperation(now=lambda: clock[0])
        clock[0] = 0.75
        recognise(self.pane(), ClaudeHost(ps=ps, environ_of=lambda pid: {'CLAUDE_CONFIG_DIR': self.config}), op)
        self.ae(asked, [FIND_DEADLINE - 0.75])
        clock[0] = FIND_DEADLINE + 1
        with self.assertRaises(ClaudeFindFailure) as cm:
            recognise(self.pane(), ClaudeHost(ps=ps, environ_of=lambda pid: {}), op)
        self.ae(cm.exception.cause, Cause.timed_out)
        self.ae(len(asked), 1, 'a spent budget spawns no ps')

    def test_the_config_dir_is_the_claude_process_own_not_kittys(self):
        # a claude started with its own CLAUDE_CONFIG_DIR writes there; kitty's environment is not consulted
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/nonexistent/kittys'}):
            self.session()
            self.ae(recognise(self.pane(), self.host(), TmuxOperation()), ClaudePane('%5', 'sid-1', 600, self.config))
            with mock.patch.dict(os.environ, {'HOME': self.config}):
                home = ClaudeHost(ps=lambda timeout: self.table, environ_of=lambda pid: {})  # unset: the default directory
                write_json(os.path.join(self.config, '.claude', 'sessions', '600.json'), {'pid': 600, 'sessionId': 'sid-2', 'tmux': 's:@1.%5'})
                self.ae(recognise(self.pane(), home, TmuxOperation()), ClaudePane('%5', 'sid-2', 600, os.path.join(self.config, '.claude')))

    def test_an_environment_that_cannot_be_read_is_a_failure(self):
        def denied(pid: int) -> dict[str, str]:
            raise PermissionError('no')

        with self.assertRaises(ClaudeFindFailure) as cm:
            recognise(self.pane(), ClaudeHost(ps=lambda timeout: self.table, environ_of=denied), TmuxOperation())
        self.ae(cm.exception.cause, Cause.undeterminable)

    def test_the_transcript_of_a_session(self):
        host = self.host()
        pane = ClaudePane('%5', 'sid-1', 600, self.config)
        self.ae(host.transcript_size(pane), NotYetWritten())
        write_json(os.path.join(self.config, 'projects', '-Users-x-code-repo', 'sid-1.jsonl'), 'abc\n')
        self.ae(host.transcript_size(pane), Written(4))
        self.ae(host.transcript_size(pane._replace(session_id='sid-2')), NotYetWritten())


class FakeClaudeSession:
    "A fake Claude Code on a private tmux pane, with the config directory of its session"

    def __init__(self, t: PrivateTmux, directory: str) -> None:
        self.t = t
        self.dir = directory
        self.conversation = os.path.join(directory, 'conversation.txt')
        self.keylog = os.path.join(directory, 'keys.log')
        self.transcript = os.path.join(directory, 'projects', 'p', 'sid-1.jsonl')

    def say(self, *lines: str) -> None:
        with open(self.conversation, 'a') as f:
            f.write('\n'.join(lines) + '\n')
        # Claude Code writes a finished message to the transcript file
        os.makedirs(os.path.dirname(self.transcript), exist_ok=True)
        with open(self.transcript, 'a') as f:
            f.write('\n'.join(lines) + '\n')

    def keys(self) -> list[str]:
        try:
            with open(self.keylog) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []

    def view(self) -> Any:
        return parse_view(self.t.tmux('capture-pane', '-p', '-t', self.t.pane))

    def host(self) -> ClaudeHost:
        # the pane's process is claude as far as ps is concerned
        pane_pid = self.t.fmt('#{pane_pid}')
        write_json(
            os.path.join(self.dir, 'sessions', f'{pane_pid}.json'),
            {'pid': int(pane_pid), 'sessionId': 'sid-1', 'tmux': self.t.fmt('#{session_name}:#{window_id}.#{pane_id}')},
        )
        return ClaudeHost(ps=lambda timeout: f'{pane_pid} 1 /x/claude\n', environ_of=lambda pid: {'CLAUDE_CONFIG_DIR': self.dir})

    def pane(self) -> ClaudePane:
        return ClaudePane(self.t.pane, 'sid-1', int(self.t.fmt('#{pane_pid}')), self.dir)

    def finder(self, state: ClaudeFindState, op: TmuxOperation | None = None) -> ClaudeFind:
        return ClaudeFind(self.t.client(), state, op or patient(), self.host(), self.pane())

    def holding(self, state: ClaudeFindState, op: TmuxOperation | None = None, host: ClaudeHost | None = None) -> ClaudeFind:
        f = ClaudeFind.holding(state, op or patient(), host or self.host())
        if f is None:
            raise AssertionError('the window holds no Claude search')
        return f


def held(state: ClaudeFindState) -> Hold:
    if state.held is None:
        raise AssertionError('the window holds no Claude search')
    return state.held


# the view each mode of the fake draws when it starts
FIRST_VIEW = {'': Prompt(), 'sticky': Prompt(), 'dialog': Unrecognised(FAKE_DIALOG[-1])}


@contextmanager
def fake_claude(*initial: str, mode: str = '', then: str = 'sleep 600') -> Any:
    if PYTHON is None:
        raise AssertionError('python3 is not on PATH')
    with tempfile.TemporaryDirectory(prefix='kitty-claude-find-') as d:
        conversation, keylog = os.path.join(d, 'conversation.txt'), os.path.join(d, 'keys.log')
        with open(conversation, 'w') as f:
            f.write('\n'.join(initial) + '\n')
        with private_tmux(Script(f'{PYTHON} {FAKE} {conversation} {keylog} {mode}; {then}', '')) as t:
            session = FakeClaudeSession(t, d)
            os.makedirs(os.path.dirname(session.transcript))
            with open(session.transcript, 'w') as f:  # Claude Code has written the conversation so far
                f.write('\n'.join(initial) + '\n')
            # the fake draws a whole view after it puts the tty in raw mode, and tmux can be read part way through one
            deadline = time.monotonic() + 10
            while (view := session.view()) != FIRST_VIEW[mode]:
                if time.monotonic() > deadline:
                    raise AssertionError(f'the fake never drew {FIRST_VIEW[mode]}: the pane shows {view}')
                time.sleep(0.02)
            yield session


CONVERSATION = [f'line {i} filler' for i in range(1, 120)]
CONVERSATION[10] = 'the zebra is first'
CONVERSATION[60] = 'a Zebra in the middle'
CONVERSATION[100] = 'zebra last'


class ClaudeFindIntegrationTest(BaseTest):
    "ClaudeFind driving a fake Claude Code on a real tmux pane: the keys it sends and the views it waits for"

    def setUp(self) -> None:
        super().setUp()
        if not (PYTHON and cf.__file__ and shutil.which('tmux')):
            self.skipTest('needs python3 and tmux')

    def test_find_steps_with_wrap_and_clear_returns_the_prompt(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            f = c.finder(state)
            self.ae(c.view(), Prompt())
            f.find(default_query('zebra'))
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))
            self.ae(c.view(), Searching(1, 3))
            self.assertIsNotNone(state.held, 'the pane is on record')
            c.finder(state).step(default_query('zebra'), False)
            c.finder(state).step(default_query('zebra'), False)
            self.ae(state.status, FindStatus(3, 3, FindCount.exact))
            c.finder(state).step(default_query('zebra'), False)
            self.ae(state.status, FindStatus(1, 3, FindCount.exact), 'forward wraps')
            c.finder(state).step(default_query('zebra'), True)
            self.ae(state.status, FindStatus(3, 3, FindCount.exact), 'backward wraps')
            c.holding(state).clear()
            self.assertIsNone(state.held)
            self.ae(c.view(), Prompt(), 'the person gets the prompt back')
            self.ae(c.keys().count("b'\\x1b'"), 1, 'one Esc from an accepted search reaches the prompt, and none after')

    def person_opens_transcript(self, c: FakeClaudeSession) -> None:
        c.t.tmux('send-keys', '-t', c.t.pane, 'C-o')  # the person's own Ctrl+o
        deadline = time.monotonic() + 5
        while not isinstance(c.view(), Transcript) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.ae(c.view(), Transcript())

    def test_clear_returns_a_pane_that_was_in_the_transcript_to_the_transcript(self):
        with fake_claude(*CONVERSATION) as c:
            self.person_opens_transcript(c)
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            self.ae(c.view(), Searching(1, 3))
            c.holding(state).clear()
            self.assertIsNone(state.held)
            self.ae(c.view(), Transcript(), 'the person gets the view they were reading back')

    def test_the_starting_view_is_recorded_once_per_hold(self):
        with fake_claude(*CONVERSATION) as c:
            self.person_opens_transcript(c)
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))  # leaves the pane in Claude's accepted search
            self.assertIs(held(state).return_to, Transcript)
            c.finder(state).find(default_query('filler'))  # the pane shows our search now, which reads as the transcript either way
            c.finder(state).step(default_query('filler'), False)
            self.assertIs(held(state).return_to, Transcript)
            c.holding(state).clear()
            self.ae(c.view(), Transcript())

    def test_a_pane_that_began_at_the_prompt_ends_at_the_prompt_after_several_searches(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            c.finder(state).find(default_query('filler'))
            self.assertIs(held(state).return_to, Prompt)
            c.holding(state).clear()
            self.ae(c.view(), Prompt())
            c.finder(state).find(default_query('zebra'))  # a new hold reads the view again
            self.assertIs(held(state).return_to, Prompt)

    def test_a_pane_that_began_in_the_persons_own_search_ends_in_the_transcript(self):
        with fake_claude(*CONVERSATION) as c:
            self.person_opens_transcript(c)
            c.t.tmux('send-keys', '-t', c.t.pane, '/')
            deadline = time.monotonic() + 5
            while c.view() != SearchInput('') and time.monotonic() < deadline:
                time.sleep(0.02)
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            c.holding(state).clear()
            self.ae(c.view(), Transcript())

    def test_a_clear_that_timed_out_after_leaving_the_search_finishes_the_way_back(self):
        with fake_claude(*CONVERSATION) as c:
            self.person_opens_transcript(c)
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            c.t.tmux('send-keys', '-t', c.t.pane, 'Escape')  # a first clear got as far as the prompt before it ran out of time
            deadline = time.monotonic() + 5
            while not isinstance(c.view(), Prompt) and time.monotonic() < deadline:
                time.sleep(0.02)
            c.holding(state).clear()
            self.ae(c.view(), Transcript())
            self.assertIsNone(state.held)

    def test_a_search_with_no_match_is_zero_and_still_cleared(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('quokka'))
            self.ae(state.status, FindStatus(0, 0, FindCount.exact))
            self.ae(c.view(), Transcript())
            c.holding(state).clear()
            self.ae(c.view(), Prompt())

    def test_a_new_query_replaces_the_search_from_any_view(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            c.finder(state).find(default_query('line 5 '))  # from an accepted search: '/' opens an empty field
            self.ae(state.status.total, 1)
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))
            # from a field that holds a query: leave it first
            f = c.finder(state)
            held = f.hold(c.t.pane)
            f.open_search(held)
            f.press(held, cf.Key('half', literal=True), lambda v: isinstance(v, SearchInput) and v.query == 'half', 'the field')
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))
            # and from a plain transcript
            c.holding(state).clear()
            f = c.finder(state)
            held = f.hold(c.t.pane)
            f.press(held, cf.CTRL_O, lambda v: isinstance(v, Transcript), 'the transcript')
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))

    def test_a_query_ending_in_a_space_is_found_and_a_space_only_query_is_refused(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra '))
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))
            c.holding(state).clear()
            for bad in (' ', 'a\x1bb'):
                c.finder(state).find(default_query(bad))
                self.ae(state.status, FindStatus(0, 0, FindCount.unavailable), repr(bad))
                self.assertIsNone(state.held, 'it was refused before a key was sent')
                self.assertTrue(state.reporting())
            self.ae(c.keys().count("b'\\x0f'"), 1, 'only the first search opened the transcript')

    def test_a_query_ending_in_a_semicolon_reaches_the_field_whole(self):
        # tmux ends a command at an argument that ends in a semicolon, and the escape for it is the key's own
        self.ae(Key('a;', literal=True).argv('%1')[-1], 'a\\;')
        self.ae(Key('Enter').argv('%1'), ['send-keys', '-t', '%1', 'Enter'])
        with fake_claude('first', 'return x; done', 'x; last', 'a\\; b') as c:
            state = ClaudeFindState()
            for query, total in (('x;', 2), (';', 3), ('a\\;', 1)):  # a backslash before the semicolon is the query's own
                c.finder(state).find(default_query(query))
                self.ae(state.status, FindStatus(1, total, FindCount.exact), query)
                c.holding(state).clear()

    def test_stepping_re_searches_when_the_conversation_grew(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status.total, 3)
            c.finder(state).step(default_query('zebra'), False)
            self.ae(state.status, FindStatus(2, 3, FindCount.exact))
            c.say('another zebra', 'and one more zebra')
            c.finder(state).step(default_query('zebra'), False)
            self.ae(state.status.total, 5, 'Claude\'s list is a snapshot, so the new matches needed a new search')
            # nothing new: a plain step again
            keys = len(c.keys())
            c.finder(state).step(default_query('zebra'), False)
            self.ae(c.keys()[keys:], ["b'n'"])

    def test_a_counter_that_does_not_move_is_the_answer_not_a_failure(self):
        # matches in one row can be one stop for n: the step waits a moment for the counter and takes it as it is
        with fake_claude(*CONVERSATION, mode='sticky') as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            start = time.monotonic()
            c.finder(state).step(default_query('zebra'), False)
            self.assertLess(time.monotonic() - start, PATIENT_DEADLINE / 2, 'a moment, not the budget')
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))
            self.ae(c.keys()[-1], "b'n'", 'the key was sent')
            c.holding(state).clear()
            self.ae(c.view(), Prompt())

    def test_stepping_with_no_search_of_ours_on_record_searches(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            f = c.finder(state)
            held = f.hold(c.t.pane)
            f.run_search(held, 'filler')  # a search Claude is on that this backend did not record
            f.clear()
            state.rest()
            c.finder(state).step(default_query('zebra'), False)  # the prompt: nothing is open, so it searches
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))
            state.search = None
            c.finder(state).step(default_query('zebra'), False)  # a search of ours is not on record: search again, not n
            self.ae(state.status, FindStatus(1, 3, FindCount.exact))

    def test_an_unrecognised_screen_gets_no_key(self):
        with fake_claude(*CONVERSATION, mode='dialog') as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))
            self.ae(c.keys(), [], 'a key sent into a dialog could answer it')
            self.assertTrue(state.reporting())
            self.assertIsNone(state.held, 'a start that is not the persons prompt or transcript is not recorded as one')

    def test_a_session_with_matches_and_no_transcript_file_is_unavailable(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status.count, FindCount.exact, 'the file is where the host looks')
            c.holding(state).clear()
            os.remove(c.transcript)
            c.finder(state).find(default_query('zebra'))
            self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))

    def test_a_search_that_times_out_keeps_the_hold_and_clear_retries(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            clock = [0.0]
            # Ctrl+o goes out, then the clock runs past the deadline while the pane is awaited
            def run(argv, env, timeout):
                out = run_tmux(argv, env, timeout)
                if 'send-keys' in argv:
                    clock[0] += FIND_DEADLINE * 2
                return out

            op = TmuxOperation(run, now=lambda: clock[0], sleep=lambda s: None)
            c.finder(state, op).find(default_query('zebra'))
            self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))
            self.assertIsNotNone(state.held, 'the pane was on record before the first key')
            c.holding(state).clear()  # a fresh budget: the pane is in the transcript view and gets its way back
            self.assertIsNone(state.held)
            self.ae(c.view(), Prompt())

    def test_clear_with_the_pane_already_at_the_prompt_sends_no_key(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            c.t.tmux('send-keys', '-t', c.t.pane, 'Escape')  # the person left the transcript themselves
            deadline = time.monotonic() + 5
            while not isinstance(c.view(), Prompt) and time.monotonic() < deadline:
                time.sleep(0.02)
            keys = len(c.keys())
            c.holding(state).clear()
            self.assertIsNone(state.held)
            self.ae(len(c.keys()), keys, 'Esc at the prompt would be Claude\'s rewind')

    def test_claude_quitting_to_a_shell_releases_the_hold_without_keys(self):
        with fake_claude(*CONVERSATION, then='stty -echo; sleep 600') as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            pane_pid = c.t.fmt('#{pane_pid}')
            c.t.tmux('send-keys', '-t', c.t.pane, 'C-c')  # the fake exits; the pane stays on a main-screen sleep
            deadline = time.monotonic() + 5
            while c.t.fmt('#{alternate_on}') != '0' and time.monotonic() < deadline:
                time.sleep(0.02)
            keys = len(c.keys())
            f = c.holding(state, host=ClaudeHost(ps=lambda timeout: f'{pane_pid} 1 /bin/sh\n', environ_of=lambda pid: {'CLAUDE_CONFIG_DIR': c.dir}))
            f.clear()
            self.assertIsNone(state.held)
            self.ae(len(c.keys()), keys)

    def test_a_pane_the_person_put_in_copy_mode_gets_no_key(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            c.t.tmux('copy-mode', '-t', c.t.pane)
            keys = len(c.keys())
            c.holding(state).clear()
            self.assertIsNone(state.held, 'the person took the pane over')
            self.ae(len(c.keys()), keys)
            self.ae(c.t.fmt('#{pane_in_mode}'), '1', 'their copy mode is theirs')

    def test_a_pane_that_is_gone_releases_the_hold(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            f = c.holding(state)  # built while the server is there to ask
            c.t.tmux('kill-session', '-t', 'p', check=False)  # the private server exits with its only session
            f.clear()
            self.assertIsNone(state.held, 'nothing is left to clear')

    def test_a_find_in_another_pane_ends_the_previous_one(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()
            c.finder(state).find(default_query('zebra'))
            other = c.t.tmux('split-window', '-d', '-P', '-F', '#{pane_id}', "printf '❯ \\n'; sleep 600")  # a prompt row: a view of Claude Code
            deadline = time.monotonic() + 5
            while not isinstance(parse_view(c.t.tmux('capture-pane', '-p', '-t', other)), Prompt) and time.monotonic() < deadline:
                time.sleep(0.02)
            f = ClaudeFind(c.t.client(), state, patient(), c.host(), ClaudePane(other, 'sid-2', 1, c.dir))
            f.guarded(lambda pane: f.hold(pane.pane_id))
            self.ae(c.view(), Prompt(), 'the first pane was brought back')
            self.ae(held(state).at.pane, other)

    def test_every_cause_is_logged_once_and_sets_unavailable(self):
        with fake_claude(*CONVERSATION) as c:
            for cause, action in (
                (Cause.refused, lambda f: f.find(default_query(' '))),
                (Cause.not_claude_pane, lambda f: setattr(f, 'pane', ClaudeFindFailure(Cause.not_claude_pane, 'x')) or f.find(default_query('zebra'))),
                (Cause.undeterminable, lambda f: setattr(f, 'pane', ClaudeFindFailure(Cause.undeterminable, 'x')) or f.find(default_query('zebra'))),
            ):
                state = ClaudeFindState()
                f = c.finder(state)
                with mock.patch.object(cf, 'log_error') as log:
                    action(f)
                self.ae(log.call_count, 1, cause)
                self.assertIn(cause.name, log.call_args[0][0])
                self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))

    def test_tmux_errors_are_told_apart_from_a_timeout(self):
        with fake_claude(*CONVERSATION) as c:
            state = ClaudeFindState()

            def run(argv, env, timeout):
                raise TmuxError('no server running')

            f = c.finder(state, TmuxOperation(run))
            with mock.patch.object(cf, 'log_error') as log:
                f.find(default_query('zebra'))
            self.assertIn(Cause.tmux.name, log.call_args[0][0])
            self.ae(state.status, FindStatus(0, 0, FindCount.unavailable))


class SelectionTest(BaseTest):
    "Cmd+E in a Claude pane: the person's drag is Claude's, copied by Claude at the release into tmux's newest automatic paste buffer"

    def test_the_newest_automatic_buffer_is_the_one_tmux_shows_by_default(self):
        listing = '100 buffer9\n103 mine\n101 buffer10\n99 buffer2\n'
        self.ae(newest_automatic_buffer(listing), PasteBuffer('buffer10', 101), 'by number, not by name order, and never a named one')
        self.assertIsNone(newest_automatic_buffer(''))
        self.assertIsNone(newest_automatic_buffer('100 mine\n'))
        for bad in ('buffer1\n', 'x buffer1\n', '100\n'):
            with self.assertRaises(TmuxError, msg=bad):
                newest_automatic_buffer(bad)

    def test_a_copy_is_the_selection_when_it_was_made_just_after_the_release(self):
        released = 1000.9
        for created, expected, why in (
            (999, False, 'an old copy'),
            (1000, True, 'the same second as the release'),
            (1001, True, 'Claude copied just after the second turned'),
            (1002, True, 'the slack tmux\'s whole seconds need'),
            (1003, False, 'a later copy: /copy, or a yank in copy mode'),
        ):
            self.ae(PasteBuffer('buffer1', created).copied_at_release(released), expected, why)

    def claude(self, t: PrivateTmux, state: ClaudeFindState) -> ClaudeFind:
        return ClaudeFind(t.client(), state, patient(), ClaudeHost(), ClaudePane(t.pane, 'sid-1', 1, '/x'))

    def test_what_tmux_holds_is_the_selection_only_when_made_by_the_release(self):
        if not shutil.which('tmux'):
            self.skipTest('needs tmux')
        with private_tmux(Script('sleep 600', '')) as t:
            state = ClaudeFindState()

            def newest_copy_made_at() -> float:
                "When tmux says its newest buffer was made: the release is placed against tmux's own clock, not the test's"
                return float(max(int(c) for c in t.tmux('list-buffers', '-F', '#{buffer_created}').split()))

            def selection() -> str | None:  # a find is built for one operation, on the budget of it: each read gets its own
                return self.claude(t, state).program_selection_text()

            t.tmux('set-buffer', 'an old copy')
            self.assertIsNone(selection(), 'no drag has been made in this window')
            state.released_at = newest_copy_made_at() + 1
            self.assertIsNone(selection(), 'the newest buffer was made before the release')
            t.tmux('set-buffer', 'needle')  # Claude copies at the release
            state.released_at = newest_copy_made_at()
            self.ae(selection(), 'needle')
            t.tmux('set-buffer', '-b', 'mine', 'a named buffer')
            self.ae(selection(), 'needle', 'a named buffer is not a copy')
            t.tmux('set-buffer', 'two\nlines')
            self.assertIsNone(selection(), 'a query is one line')
            t.tmux('set-buffer', 'late')
            state.released_at = newest_copy_made_at() - COPY_LATENCY - 1
            self.assertIsNone(selection(), 'a copy long after the release is some other copy')
            state.released_at = newest_copy_made_at()
            t.tmux('delete-buffer', '-b', 'mine')
            while t.tmux('list-buffers'):
                t.tmux('delete-buffer')
            self.assertIsNone(selection(), 'nothing copied')

    def test_a_pane_that_cannot_be_told_has_no_selection_to_read(self):
        state = ClaudeFindState()
        state.released_at = time.time()
        failed = ClaudeFindFailure(Cause.undeterminable, 'no session file')
        f = ClaudeFind(mock.Mock(), state, mock.Mock(), ClaudeHost(), failed)
        self.assertIsNone(f.program_selection_text())
        f.op.tmux.assert_not_called()


class OperationTest(BaseTest):
    def test_one_budget_across_commands_and_waits(self):
        clock = [0.0]

        def run(argv, env, timeout):
            clock[0] += 0.4
            return ''

        sent = Sent(run, clock=lambda: clock[0])
        op = sent.operation(sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        client = mock.Mock(exe='sh', server_args=(), tmpdir=None, path=os.environ.get('PATH'))
        op.tmux(client, 'a')
        op.tmux(client, 'b')
        with self.assertRaises(TmuxError) as cm:
            op.tmux(client, 'c')
            op.tmux(client, 'd')
        self.assertTrue(cm.exception.timed_out)
        self.ae(len(sent.commands), 3, 'no command is run once the budget is spent')
        self.ae(sent.overspent(op), [], 'each command gets exactly what is left')

    def test_a_value_that_stays_is_returned_after_the_patience(self):
        clock = [0.0]
        op = TmuxOperation(lambda *a: '', now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.ae(op.wait_for_change(lambda: 'same', 'same', 0.3), 'same')
        self.assertLessEqual(clock[0], 0.31)
        self.ae(op.wait_for_change(lambda: 'moved', 'same', 0.3), 'moved')
        clock[0] = 0.0
        op = TmuxOperation(lambda *a: '', now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        self.ae(op.wait_for_change(lambda: 'same', 'same', 99), 'same')
        self.assertLessEqual(clock[0], FIND_DEADLINE + 0.01, 'the budget bounds the patience')

    def test_waiting_reports_what_was_awaited_and_what_was_last_seen(self):
        clock = [0.0]
        op = TmuxOperation(lambda *a: '', now=lambda: clock[0], sleep=lambda s: clock.__setitem__(0, clock[0] + s))
        seen = iter(range(10**6))
        self.ae(op.wait_until(lambda: next(seen), lambda v: v == 3, 'three'), 3)
        with self.assertRaises(TmuxError) as cm:
            op.wait_until(lambda: 'stuck', lambda v: False, 'something else')
        self.assertTrue(cm.exception.timed_out)
        self.assertIn("awaiting something else; last seen 'stuck'", str(cm.exception))
        self.assertLessEqual(clock[0], FIND_DEADLINE + 0.1, 'the wait ended at the deadline')
