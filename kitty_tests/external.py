#!/usr/bin/env python
# License: GPLv3 Copyright: 2021, Kovid Goyal <kovid at kovidgoyal.net>

# Test suites that run as an external process (go test, cargo test): one process type,
# with a parser per tool that turns its output into per-test records.

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from tempfile import mkdtemp
from typing import Any, Protocol


class Outcome(Enum):
    passed = 'pass'
    failed = 'fail'
    skipped = 'skip'


@dataclass(frozen=True)
class TestRecord:
    id: str
    outcome: Outcome
    elapsed: float | None  # None: the tool does not report per-test durations
    output: str


@dataclass(frozen=True)
class UnitFailure:
    # A unit (go package, cargo test binary) that failed without a failing test to
    # show for it: it did not build, or it crashed before reporting.
    unit: str
    output: str


type Record = TestRecord | UnitFailure


class OutputParser(Protocol):
    def feed(self, line: str) -> list[Record]: ...

    # Records still pending at end of stream, and the text that belongs to no unit.
    def finish(self) -> tuple[list[Record], str]: ...


class GoJsonParser:
    # Parses `go test -json` events.

    def __init__(self) -> None:
        self.test_output: dict[str, list[str]] = {}
        self.pkg_output: dict[str, list[str]] = {}
        # build-output events name their package by ImportPath ("pkg [pkg.test]"),
        # and the fail event of every package it broke points back at it with FailedBuild.
        self.build_output: dict[str, list[str]] = {}
        self.placed_builds: set[str] = set()
        self.pkgs_with_failed_tests: set[str] = set()
        self.unplaced: list[str] = []

    def feed(self, line: str) -> list[Record]:
        if not line:
            return []
        try:
            evt: Any = json.loads(line)
        except json.JSONDecodeError:
            evt = None
        # a test2json event is a JSON object; any other line is text a program wrote around them
        if not isinstance(evt, dict):
            self.unplaced.append(line + '\n')
            return []
        action = evt.get('Action', '')
        if action == 'build-output':
            self.build_output.setdefault(evt.get('ImportPath', ''), []).append(evt.get('Output', ''))
            return []
        test_name = evt.get('Test', '')
        pkg = evt.get('Package', '')
        if test_name:
            full = f'{pkg}.{test_name}' if pkg else test_name
            if action == 'run':
                self.test_output[full] = []
            elif action == 'output':
                self.test_output.setdefault(full, []).append(evt.get('Output', ''))
            elif action in ('pass', 'fail', 'skip'):
                output = ''.join(self.test_output.pop(full, []))
                outcome = Outcome(action)
                if outcome is Outcome.failed:
                    self.pkgs_with_failed_tests.add(pkg)
                return [TestRecord(full, outcome, float(evt.get('Elapsed') or 0.0), output if outcome is Outcome.failed else '')]
            return []
        if action == 'output':
            self.pkg_output.setdefault(pkg, []).append(evt.get('Output', ''))
            return []
        failed_build = evt.get('FailedBuild', '')
        self.placed_builds.add(failed_build)
        output = ''.join(self.build_output.get(failed_build, []) + self.pkg_output.pop(pkg, []))
        # [LAW:no-silent-failure] decided per package: a package that failed with no
        # failing test of its own (a build error, a crash in TestMain) is its own failure.
        if action == 'fail' and pkg not in self.pkgs_with_failed_tests:
            return [UnitFailure(pkg, output)]
        return []

    def finish(self) -> tuple[list[Record], str]:
        # build output no failed package claimed, and output of packages that never finished
        leftover = [lines for path, lines in self.build_output.items() if path not in self.placed_builds] + list(self.pkg_output.values())
        return [], ''.join(self.unplaced) + ''.join(''.join(lines) for lines in leftover)


@dataclass
class _CargoSection:
    # One test binary's stretch of `cargo test` output.
    target: str
    lines: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    failure_output: dict[str, list[str]] = field(default_factory=dict)
    in_failure_block: str = ''
    reached_result: bool = False

    def close(self) -> list[Record]:
        records: list[Record] = [TestRecord(f'{self.target}::{path}', Outcome.failed, None, ''.join(self.failure_output.get(path, []))) for path in self.failed]
        # [LAW:no-silent-failure] a binary is complete only once libtest printed its
        # result line. Anything else is a crash, whether or not a test already failed:
        # libtest prints the failed tests' output only at the end, so it is in lines.
        if not self.reached_result:
            records.append(UnitFailure(self.target, ''.join(self.lines)))
        return records


class CargoTextParser:
    # Parses libtest's stable plain-text output as printed by `cargo test`. libtest's
    # JSON format and its per-test durations need a nightly compiler.
    running = re.compile(r'^\s+Running (.+?) \(.+\)$')
    result_line = re.compile(r'^test (\S+)(?: - should panic)? \.\.\. (ok|FAILED|ignored)')
    failure_header = re.compile(r'^---- (\S+) stdout ----$')

    def __init__(self) -> None:
        self.section: _CargoSection | None = None
        self.preamble: list[str] = []

    def feed(self, line: str) -> list[Record]:
        if m := self.running.match(line):
            records = self.section.close() if self.section is not None else []
            self.section = _CargoSection(m.group(1))
            return records
        s = self.section
        if s is None:
            self.preamble.append(line + '\n')
            return []
        s.lines.append(line + '\n')
        if m := self.failure_header.match(line):
            s.in_failure_block = m.group(1)
            s.failure_output[s.in_failure_block] = []
        elif line == 'failures:':
            s.in_failure_block = ''
        elif s.in_failure_block:
            s.failure_output[s.in_failure_block].append(line + '\n')
        elif line.startswith('test result: '):
            s.reached_result = True
        elif m := self.result_line.match(line):
            path, status = m.group(1, 2)
            if status == 'FAILED':
                s.failed.append(path)
            else:
                return [TestRecord(f'{s.target}::{path}', Outcome.passed if status == 'ok' else Outcome.skipped, None, '')]
        return []

    def finish(self) -> tuple[list[Record], str]:
        preamble = ''.join(self.preamble)
        if self.section is not None:
            # cargo reached a test binary, so the preamble is a build that succeeded
            return self.section.close(), ''
        # A built crate always has its unittests binary to run, so a stream that never
        # reached one is cargo failing before any test: the build, usually.
        return [UnitFailure('cargo build', preamble)], ''


class ExternalTestProc:
    def __init__(self, label: str, cmd: list[str], parser: OutputParser, env_overrides: dict[str, str]):
        from kitty.constants import kitty_exe

        self.label = label
        self.parser = parser
        env = os.environ.copy()
        env.update(env_overrides)
        env['KITTY_PATH_TO_KITTY_EXE'] = kitty_exe()
        self.start_time = time.monotonic()
        self.end_time: float = 0.0
        self.tdir = mkdtemp(prefix=f'kitty-{label.lower()}-tests-')
        env['HOME'] = self.tdir
        env['XDG_CONFIG_HOME'] = self.tdir + '/conf'
        os.mkdir(env['XDG_CONFIG_HOME'])
        env['XDG_CACHE_HOME'] = self.tdir + '/cache'
        os.mkdir(env['XDG_CACHE_HOME'])
        # start_new_session so that the tests cannot touch the terminal the test
        # suite is being run from, see detach_from_controlling_terminal()
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, stdin=subprocess.DEVNULL, start_new_session=True)
        if self.proc.stdout is None:
            raise AssertionError(f'{label}: Popen with stdout=PIPE returned no stdout pipe')
        self.stdout = self.proc.stdout
        self.stdout_fd = self.stdout.fileno()
        self.buffer = b''
        self.active = True
        self.run = 0
        self.failures: list[tuple[str, str]] = []
        self.timings: list[tuple[float, str]] = []
        self.unplaced = ''

    @property
    def runtime(self) -> float:
        return self.end_time - self.start_time

    @property
    def returncode(self) -> int:
        return self.proc.returncode

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.failures

    def take(self, records: list[Record]) -> None:
        for r in records:
            match r:
                case TestRecord():
                    self.run += 1
                    if r.elapsed is not None:
                        self.timings.append((r.elapsed, f'[{self.label}] {r.id}'))
                    if r.outcome is Outcome.failed:
                        self.failures.append((r.id, r.output))
                case UnitFailure():
                    self.failures.append((r.unit, r.output))

    def read_available(self) -> None:
        """Consume what the pipe has; at end of stream, reap the process."""
        try:
            chunk = os.read(self.stdout_fd, 65536)
        except OSError:
            chunk = b''
        if chunk:
            self.buffer += chunk
            *lines, self.buffer = self.buffer.split(b'\n')
            for raw in lines:
                self.take(self.parser.feed(raw.decode('utf-8', errors='replace')))
            return
        if self.buffer:
            self.take(self.parser.feed(self.buffer.decode('utf-8', errors='replace')))
            self.buffer = b''
        records, self.unplaced = self.parser.finish()
        self.take(records)
        self.active = False
        self.finish()

    def finish(self) -> None:
        """Reap the process after its stdout has been fully drained."""
        self.stdout.close()
        self.proc.wait()
        self.end_time = time.monotonic()
        shutil.rmtree(self.tdir, ignore_errors=True)

    def kill(self) -> None:
        # The process leads its own group (start_new_session), so this also kills the test
        # binaries go and cargo spawned. A process whose output ended was reaped by read_available.
        if self.active:
            os.killpg(self.proc.pid, signal.SIGKILL)
            self.active = False
            self.finish()


# go {{{


def find_testable_go_packages() -> tuple[set[str], dict[str, list[str]]]:
    test_functions: dict[str, list[str]] = {}
    ans = set()
    base = os.getcwd()
    pat = re.compile(r'^func Test([A-Z]\w+)', re.MULTILINE)
    for dirpath, dirnames, filenames in os.walk(base):
        if 'b' in dirnames and os.path.basename(dirpath) == 'bypy':
            dirnames.remove('b')
        if dirpath == base and 'build' in dirnames:
            dirnames.remove('build')
        for f in filenames:
            if f.endswith('_test.go'):
                q = os.path.relpath(dirpath, base)
                ans.add(q)
                with open(os.path.join(dirpath, f)) as s:
                    raw = s.read()
                for m in pat.finditer(raw):
                    test_functions.setdefault(m.group(1), []).append(q)
    return ans, test_functions


@lru_cache
def go_exe() -> str:
    return shutil.which('go') or ''


def go_env() -> dict[str, str]:
    # HOME is redirected for the tests, so pin go's caches to where they really are.
    ans: dict[str, str] = {}
    if not os.environ.get('GOCACHE') or not os.environ.get('GOMODCACHE'):
        try:
            r = subprocess.run([go_exe(), 'env', 'GOCACHE', 'GOMODCACHE'], capture_output=True, text=True, timeout=10)
            if r.returncode == 0:
                gop, gomodp = r.stdout.strip().splitlines()
                if gop and not os.environ.get('GOCACHE') and os.path.isdir(gop):
                    ans['GOCACHE'] = gop
                if gomodp and not os.environ.get('GOMODCACHE') and os.path.isdir(gomodp):
                    ans['GOMODCACHE'] = gomodp
        except (OSError, subprocess.TimeoutExpired):
            pass
    return ans


def run_go(packages: set[str], names: Sequence[str]) -> ExternalTestProc:
    go_pkg_args = [f'github.com/kovidgoyal/kitty/{x}' for x in packages]
    cmd = [go_exe(), 'test', '--tags', 'testing', '-v', '-json']
    if names:
        cmd.extend(('-run', '^Test(?:' + '|'.join(re.escape(name) for name in names) + ')$'))
    cmd += go_pkg_args
    return ExternalTestProc('Go', cmd, GoJsonParser(), go_env())


def reduce_go_pkgs(module: str, names: Sequence[str]) -> set[str]:
    if not go_exe():
        raise SystemExit('go executable not found, current path: ' + repr(os.environ.get('PATH', '')))
    go_packages, go_functions = find_testable_go_packages()
    if module:
        go_packages &= {module}
    if names:
        pkgs = set()
        for name in names:
            pkgs |= set(go_functions.get(name, []))
        go_packages &= pkgs
    return go_packages


# }}}

# rust {{{

RUST_MODULE = 'rust'


def cargo_test_cmd() -> list[str]:
    cargo = shutil.which('cargo')
    if not cargo:
        raise SystemExit('cargo not found (install rustup: https://rustup.rs), current path: ' + repr(os.environ.get('PATH', '')))
    # rust-toolchain.toml at the repo root pins the toolchain; it is found from the
    # current directory, which is the repo root like it is for go. CargoTextParser reads
    # uncoloured text, whatever colour the user's cargo config asks for.
    return [cargo, 'test', '--locked', '--color', 'never', '--manifest-path', 'rust/Cargo.toml', '--target-dir', 'build/rust/test']


def cargo_env() -> dict[str, str]:
    # HOME is redirected for the tests; rustup and cargo find their installs through it.
    home = os.path.expanduser('~')
    return {
        'CARGO_HOME': os.environ.get('CARGO_HOME') or os.path.join(home, '.cargo'),
        'RUSTUP_HOME': os.environ.get('RUSTUP_HOME') or os.path.join(home, '.rustup'),
    }


def rust_tests_selected(module: str) -> bool:
    return sys.platform == 'darwin' and module in ('', RUST_MODULE)


def resolve_rust_tests(cargo_cmd: list[str], names: Sequence[str]) -> list[str]:
    # A name means what it means for Python and go tests, an exact match: it selects the
    # Rust test whose last path segment it equals (counts_ascii -> tests::counts_ascii).
    r = subprocess.run(cargo_cmd + ['--', '--list'], capture_output=True, text=True, env=os.environ | cargo_env())
    if r.returncode != 0:
        # [LAW:no-silent-failure] a list that cannot build is not "no matching tests".
        raise SystemExit(f'Listing the Rust tests failed, they do not build:\n{r.stdout}{r.stderr}')
    wanted = frozenset(names)
    return [path for path, kind in (x.rpartition(': ')[::2] for x in r.stdout.splitlines()) if kind == 'test' and path.rpartition('::')[2] in wanted]


def run_cargo(cargo_cmd: list[str], exact_paths: Sequence[str]) -> ExternalTestProc:
    cmd = cargo_cmd + ['--no-fail-fast']
    if exact_paths:
        cmd += ['--', '--exact', *exact_paths]
    return ExternalTestProc('Rust', cmd, CargoTextParser(), cargo_env())


# }}}
