#!/usr/bin/env python
# License: GPLv3 Copyright: 2021, Kovid Goyal <kovid at kovidgoyal.net>

import importlib
import json
import os
import select
import shutil
import subprocess
import sys
import time
import unittest
from collections.abc import Callable, Generator, Iterator, Sequence
from contextlib import contextmanager, suppress
from tempfile import TemporaryDirectory
from typing import (
    Any,
    NoReturn,
    Optional,
)

from kitty.constants import list_kitty_resources

from . import is_ci
from .external import RUST_MODULE, ExternalTestProc, cargo_test_cmd, go_exe, reduce_go_pkgs, resolve_rust_tests, run_cargo, run_go, rust_tests_selected

PARALLEL_THRESHOLD = 20


def itertests(suite: unittest.TestSuite) -> Generator[unittest.TestCase, None, None]:
    stack = [suite]
    while stack:
        suite = stack.pop()
        for test in suite:
            if isinstance(test, unittest.TestSuite):
                stack.append(test)
                continue
            if test.__class__.__name__ == 'ModuleImportFailure':
                raise Exception('Failed to import a test module: %s' % test)
            yield test


def find_all_tests(package: str = '', excludes: Sequence[str] = ('main', 'gr')) -> unittest.TestSuite:
    suits = []
    if not package:
        package = __name__.rpartition('.')[0] if '.' in __name__ else 'kitty_tests'
    for x in sorted(list_kitty_resources(package)):
        name, ext = os.path.splitext(x)
        if ext in ('.py', '.pyc') and name not in excludes:
            m = importlib.import_module(package + '.' + x.partition('.')[0])
            suits.append(unittest.defaultTestLoader.loadTestsFromModule(m))
    return unittest.TestSuite(suits)


def filter_tests(suite: unittest.TestSuite, test_ok: Callable[[unittest.TestCase], bool]) -> unittest.TestSuite:
    ans = unittest.TestSuite()
    added: set[unittest.TestCase] = set()
    for test in itertests(suite):
        if test_ok(test) and test not in added:
            ans.addTest(test)
            added.add(test)
    return ans


def filter_tests_by_name(suite: unittest.TestSuite, *names: str) -> unittest.TestSuite:
    names_ = {x if x.startswith('test_') else 'test_' + x for x in names}

    def q(test: unittest.TestCase) -> bool:
        return test._testMethodName in names_

    return filter_tests(suite, q)


def filter_tests_by_module(suite: unittest.TestSuite, *names: str) -> unittest.TestSuite:
    names_ = frozenset(names)

    def q(test: unittest.TestCase) -> bool:
        m = test.__class__.__module__.rpartition('.')[-1]
        return m in names_

    return filter_tests(suite, q)


def type_check() -> NoReturn:
    from kitty.cli_stub import generate_stub  # type:ignore

    generate_stub()
    from kittens.tui.operations_stub import generate_stub  # type: ignore

    generate_stub()
    os.execlp('ty', 'ty', 'check')


def run_cli(suite: unittest.TestSuite, verbosity: int = 4) -> bool:
    r = unittest.TextTestRunner
    r.resultclass = unittest.TextTestResult
    runner = r(verbosity=verbosity)
    runner.tb_locals = True  # type: ignore
    from .base import forwardable_stdio

    with forwardable_stdio():
        result = runner.run(suite)
    sys.stdout.flush()
    sys.stderr.flush()
    return result.wasSuccessful()


@contextmanager
def env_vars(**kw: str) -> Iterator[None]:
    originals = {k: os.environ.get(k) for k in kw}
    os.environ.update(kw)
    try:
        yield
    finally:
        for k, v in originals.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def path_for_python_tests() -> str:
    """The PATH used by tests: the built launcher dir followed by all entries not under the real home dir."""
    from kitty.constants import kitty_exe

    current_home = os.path.expanduser('~') + os.sep
    paths = os.environ.get('PATH', '/usr/local/sbin:/usr/local/bin:/usr/bin').split(os.pathsep)
    path = os.pathsep.join(x for x in paths if not x.startswith(current_home))
    launcher_dir = os.path.dirname(kitty_exe())
    return f'{launcher_dir}{os.pathsep}{path}'


def report_test_env() -> None:
    """Print details of the environment tests will run in. Called once in the master process as workers have their stdout captured."""
    print('Running under CI:', is_ci)
    print('Using PATH in test environment:', path_for_python_tests())
    from kitty.fast_data_types import has_avx2, has_avx512, has_sse4_2

    print(f'Intrinsics: {has_avx512=} {has_avx2=} {has_sse4_2=}')


@contextmanager
def env_for_python_tests() -> Iterator[None]:
    gohome = os.path.expanduser('~/go')
    slangc = os.environ.get('SLANGC') or shutil.which('slangc') or 'slangc'
    path = path_for_python_tests()
    with (
        TemporaryDirectory() as tdir,
        env_vars(
            HOME=tdir,
            KT_ORIGINAL_HOME=os.path.expanduser('~'),
            USERPROFILE=tdir,
            PATH=path,
            TERM='xterm-kitty',
            SLANGC=slangc,
            XDG_CONFIG_HOME=os.path.join(tdir, '.config'),
            XDG_CONFIG_DIRS=os.path.join(tdir, '.config'),
            XDG_DATA_DIRS=os.path.join(tdir, '.local', 'xdg'),
            XDG_CACHE_HOME=os.path.join(tdir, '.cache'),
            XDG_RUNTIME_DIR=os.path.join(tdir, '.cache', 'run'),
            PYTHONWARNINGS='error',
        ),
    ):
        if os.path.isdir(gohome):
            os.symlink(gohome, os.path.join(tdir, os.path.basename(gohome)))
        yield


def send_record(write_fd: int, record: dict[str, Any]) -> None:
    data = (json.dumps(record) + '\n').encode()
    while data:
        n = os.write(write_fd, data)
        data = data[n:]


class PipeTestResult(unittest.TestResult):
    """Writes test results as newline-delimited JSON records to a file descriptor."""

    def __init__(self, write_fd: int) -> None:
        super().__init__()
        self._wfd = write_fd
        self._test_start: float = 0.0

    def _send(self, record: dict[str, Any]) -> None:
        send_record(self._wfd, record)

    def _elapsed(self) -> float:
        return time.monotonic() - self._test_start

    def startTest(self, test: unittest.TestCase) -> None:
        super().startTest(test)
        self._test_start = time.monotonic()
        self._send({'t': 'start', 'id': str(test)})

    def addSuccess(self, test: unittest.TestCase) -> None:
        self._send({'t': 'ok', 'id': str(test), 'e': self._elapsed()})

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        self._send({'t': 'error', 'id': str(test), 'e': self._elapsed(), 'msg': self._exc_info_to_string(err, test)})

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        self._send({'t': 'fail', 'id': str(test), 'e': self._elapsed(), 'msg': self._exc_info_to_string(err, test)})

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        self._send({'t': 'skip', 'id': str(test), 'e': self._elapsed(), 'msg': reason})

    def addExpectedFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addExpectedFailure(test, err)
        self._send({'t': 'xfail', 'id': str(test), 'e': self._elapsed(), 'msg': self._exc_info_to_string(err, test)})

    def addUnexpectedSuccess(self, test: unittest.TestCase) -> None:
        super().addUnexpectedSuccess(test)
        self._send({'t': 'xpass', 'id': str(test), 'e': self._elapsed()})

    def addSubTest(self, test: unittest.TestCase, subtest: unittest.TestCase, err: Any) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            record_type = 'fail' if issubclass(err[0], test.failureException) else 'error'
            self._send({'t': record_type, 'id': str(subtest), 'e': self._elapsed(), 'msg': self._exc_info_to_string(err, test)})


def run_test_worker(tests: list[unittest.TestCase], write_fd: int, verbosity: Optional[int] = None) -> None:
    """Execute in a child process: run tests, then exit, never returning.

    With verbosity None, results are sent over write_fd as JSON records,
    otherwise they are printed to stdout. In both cases a final 'done' record
    is sent once the tests and their environment cleanup are complete, so the
    parent can detect a worker that exits early, for example because some code
    under test called os._exit(0). Such an exit cannot be detected in-process.
    """
    worker_pid = os.getpid()
    exit_code = 1
    try:
        with env_for_python_tests():
            if verbosity is not None:
                ok = run_cli(unittest.TestSuite(tests), verbosity)
            else:
                from .base import forwardable_stdio

                with forwardable_stdio():
                    unittest.TestSuite(tests).run(PipeTestResult(write_fd))
                ok = True
        exit_code = 0 if ok else 1
    except BaseException:
        if os.getpid() != worker_pid:
            # A process forked by a test escaped from it, it must not report
            # anything on behalf of the worker
            os._exit(1)
        import traceback

        with suppress(OSError):
            send_record(write_fd, {'t': 'worker_error', 'msg': traceback.format_exc()})
        exit_code = 1
    if os.getpid() == worker_pid:
        with suppress(OSError):
            send_record(write_fd, {'t': 'done'})
    with suppress(OSError):
        os.close(write_fd)
    os._exit(exit_code)


def detach_from_controlling_terminal() -> None:
    """Put the calling process into its own session, with no controlling terminal.

    Tests must never depend on the terminal the test suite happens to be run
    from, and must never be able to disturb it. Without this, any program a
    test runs can reach it via /dev/tty; a job control aware shell for instance
    will make its own process group the foreground one on it, and a second such
    shell started while that is the case stops the entire test run with SIGTTIN.
    """
    try:
        os.setsid()
    except OSError:  # already a session leader
        pass


def fork_test_workers(tests: list[unittest.TestCase]) -> tuple[list[int], list[int]]:
    """Chunk tests and fork worker processes. Returns (pids, read_fds)."""
    n = min(os.cpu_count() or 4, 8, len(tests))
    # Round-robin assignment so slow and fast tests are spread across all workers
    chunks: list[list[unittest.TestCase]] = [[] for _ in range(n)]
    for i, test in enumerate(tests):
        chunks[i % n].append(test)
    pids: list[int] = []
    read_fds: list[int] = []
    for chunk in chunks:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            detach_from_controlling_terminal()
            devnull = os.open(os.devnull, os.O_RDWR)
            os.dup2(devnull, 0)
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
            os.close(devnull)
            run_test_worker(chunk, w)
            # run_test_worker calls os._exit() — never returns
        os.close(w)
        read_fds.append(r)
        pids.append(pid)
    return pids, read_fds


def worker_main(write_fd: int, ids_fd: int, verbosity: Optional[int] = None) -> None:
    import warnings

    warnings.simplefilter('error')
    # we need fonts installed in the user home directory as well, so initialize
    # fontconfig before nuking $HOME and friends
    from kitty.fonts.common import all_fonts_map

    all_fonts_map(True)

    with open(ids_fd) as f:
        test_ids = json.load(f)
    tests = [getattr(importlib.import_module(t['module']), t['cls'])(t['method']) for t in test_ids]
    run_test_worker(tests, write_fd, verbosity)


def spawn_test_worker(tests: list[unittest.TestCase], verbosity: Optional[int] = None) -> tuple['subprocess.Popen[bytes]', int]:
    """Spawn a worker subprocess via kitty +runpy. Returns (proc, read_fd).

    Used on macOS where fork() + threading is unsafe. With verbosity None the
    worker is detached from the terminal, otherwise it shares it with us."""
    from kitty.constants import kitty_exe

    r, w = os.pipe()
    ids_r, ids_w = os.pipe()
    test_ids = [{'module': t.__class__.__module__, 'cls': t.__class__.__name__, 'method': t._testMethodName} for t in tests]
    detached = verbosity is None
    try:
        proc: subprocess.Popen[bytes] = subprocess.Popen(
            [kitty_exe(), '+runpy', f'from kitty_tests.main import *; worker_main({w}, {ids_r}, {verbosity!r})'],
            pass_fds=(w, ids_r),
            stdin=subprocess.DEVNULL if detached else None,
            stdout=subprocess.DEVNULL if detached else None,
            stderr=subprocess.DEVNULL if detached else None,
            start_new_session=detached,  # see detach_from_controlling_terminal()
        )
    except BaseException:
        os.close(r)
        os.close(ids_w)
        raise
    finally:
        os.close(w)
        os.close(ids_r)
    with open(ids_w, 'w') as f:
        json.dump(test_ids, f)
    return proc, r


def spawn_test_workers(tests: list[unittest.TestCase]) -> tuple[list[subprocess.Popen[bytes]], list[int]]:
    """Chunk tests and spawn worker subprocesses. Returns (procs, read_fds)."""
    n = min(os.cpu_count() or 4, 8, len(tests))
    chunks: list[list[unittest.TestCase]] = [[] for _ in range(n)]
    for i, test in enumerate(tests):
        chunks[i % n].append(test)

    procs: list[subprocess.Popen[bytes]] = []
    read_fds: list[int] = []

    for chunk in chunks:
        proc, r = spawn_test_worker(chunk)
        read_fds.append(r)
        procs.append(proc)

    return procs, read_fds


def run_serial_tests(tests: list[unittest.TestCase], verbosity: int) -> bool:
    """Run tests in a single child process that shares our terminal, printing
    results as they happen. The child is used so that it exiting before all
    tests have run is detected."""
    if sys.platform == 'darwin':
        proc, r = spawn_test_worker(tests, verbosity)
        pids: list[int] = []
        procs = [proc]
    else:
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            run_test_worker(tests, w, verbosity)
        os.close(w)
        pids, procs = [pid], []
    try:
        data = b''
        while chunk := os.read(r, 65536):
            data += chunk
        os.close(r)
        if pids:
            status = os.waitstatus_to_exitcode(os.waitpid(pids[0], 0)[1])
        else:
            status = procs[0].wait()
    except BaseException:
        terminate_workers(pids, procs, ())
        raise
    done = False
    worker_errors: list[str] = []
    for line in data.splitlines():
        if line:
            rec = json.loads(line)
            if rec['t'] == 'done':
                done = True
            elif rec['t'] == 'worker_error':
                worker_errors.append(rec['msg'])
    if not done:
        worker_errors.append(f'\nPython test process exited with status {status} before completing its tests')
    for msg in worker_errors:
        print(msg, file=sys.stderr)
    sys.stderr.flush()
    return status == 0 and not worker_errors


_RED = '\x1b[31m'
_GREEN = '\x1b[32m'
_YELLOW = '\x1b[33m'
_CYAN = '\x1b[36m'
_RESET = '\x1b[0m'
_BOLD = '\x1b[1m'
_DIM = '\x1b[2m'


def collect_worker_results(
    pids: list[int],
    procs: list[subprocess.Popen[bytes]],
    read_fds: list[int],
    total_py_tests: int,
    externals: Sequence[ExternalTestProc] = (),
) -> tuple[bool, bool]:
    """Read JSON from worker pipes and the external test processes' stdout, show live progress, print failures."""
    use_tty = sys.stdout.isatty()
    py_buffers: dict[int, bytes] = {fd: b'' for fd in read_fds}
    # read_fds are in the same order as the pids or procs they belong to
    worker_pid_for_fd = dict(zip(read_fds, pids + [p.pid for p in procs]))
    py_completed: set[int] = set()
    py_last_started: dict[int, str] = {}
    active_py = list(read_fds)
    external_for_fd = {p.stdout_fd: p for p in externals}
    start = time.monotonic()

    py_run = 0
    py_failures: list[tuple[str, str]] = []
    py_errors: list[tuple[str, str]] = []
    py_skipped = 0
    py_unexpected_successes: list[str] = []
    py_worker_errors: list[str] = []

    # (elapsed_seconds, label) for every completed test in both suites
    all_timings: list[tuple[float, str]] = []

    def c(code: str, text: str) -> str:
        return (code + text + _RESET) if use_tty else text

    def render_progress() -> str:
        elapsed = time.monotonic() - start
        parts: list[str] = []
        if total_py_tests > 0 or active_py:
            py_mark = c(_GREEN, ' ✓') if not active_py else ''
            parts.append(c(_CYAN + _BOLD, 'Py') + f' {py_run}/{total_py_tests}' + py_mark)
        for ext in externals:
            # External totals are omitted: go's t.Run() sub-tests inflate the count
            # beyond what static analysis of Test* functions can predict.
            ext_mark = c(_GREEN, ' ✓') if not ext.active else ''
            parts.append(c(_YELLOW + _BOLD, ext.label) + f' {ext.run}' + ext_mark)
        fail_count = len(py_failures) + len(py_errors) + sum(len(ext.failures) for ext in externals)
        if fail_count:
            parts.append(c(_RED, f'{fail_count} failed'))
        return '  ' + '  '.join(parts) + '  ' + c(_DIM, f'[{elapsed:.1f}s]')

    def show_progress(force: bool = False) -> None:
        line = render_progress()
        if use_tty:
            print(f'\r{line}\x1b[K', end='', flush=True)
        elif force or (total_py_tests > 0 and py_run % max(1, total_py_tests // 10) == 0):
            print(line, flush=True)

    while active_py or any(ext.active for ext in externals):
        watch = list(active_py) + [ext.stdout_fd for ext in externals if ext.active]
        readable, _, _ = select.select(watch, [], [])

        for fd in readable:
            if (ext := external_for_fd.get(fd)) is not None:
                failures_before = len(ext.failures)
                ext.read_available()
                show_progress(force=not ext.active or len(ext.failures) != failures_before)
            else:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    chunk = b''
                if not chunk:
                    active_py.remove(fd)
                    os.close(fd)
                    show_progress(force=True)
                    continue
                py_buffers[fd] += chunk
                while b'\n' in py_buffers[fd]:
                    raw_py, py_buffers[fd] = py_buffers[fd].split(b'\n', 1)
                    if not raw_py:
                        continue
                    rec: dict[str, Any] = json.loads(raw_py)
                    t = rec['t']
                    elapsed_test = float(rec.get('e') or 0.0)
                    if t == 'start':
                        py_last_started[fd] = rec['id']
                    elif t == 'done':
                        py_completed.add(fd)
                    elif t == 'ok':
                        py_run += 1
                        all_timings.append((elapsed_test, f'[Py] {rec["id"]}'))
                        show_progress()
                    elif t == 'fail':
                        py_run += 1
                        py_failures.append((rec['id'], rec['msg']))
                        all_timings.append((elapsed_test, f'[Py] {rec["id"]}'))
                        show_progress()
                    elif t == 'error':
                        py_run += 1
                        py_errors.append((rec['id'], rec['msg']))
                        all_timings.append((elapsed_test, f'[Py] {rec["id"]}'))
                        show_progress()
                    elif t == 'skip':
                        py_run += 1
                        py_skipped += 1
                        all_timings.append((elapsed_test, f'[Py] {rec["id"]}'))
                        show_progress()
                    elif t == 'xfail':
                        py_run += 1
                        all_timings.append((elapsed_test, f'[Py] {rec["id"]}'))
                        show_progress()
                    elif t == 'xpass':
                        py_run += 1
                        py_unexpected_successes.append(rec['id'])
                        all_timings.append((elapsed_test, f'[Py] {rec["id"]}'))
                        show_progress()
                    elif t == 'worker_error':
                        py_worker_errors.append(rec['msg'])

    exit_codes: dict[int, int] = {pid: os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1]) for pid in pids}
    exit_codes.update((proc.pid, proc.wait()) for proc in procs)
    for fd, pid in worker_pid_for_fd.items():
        if fd not in py_completed:
            msg = f'Python test worker {pid} exited with status {exit_codes[pid]} before completing its tests'
            if last := py_last_started.get(fd):
                msg += f', the last test it started was: {last}'
            py_worker_errors.append(msg)
        elif exit_codes[pid] != 0:
            py_worker_errors.append(f'Python test worker {pid} exited with status {exit_codes[pid]}')

    elapsed = time.monotonic() - start

    if use_tty:
        print()  # move past the progress line

    sep1 = '=' * 70
    sep2 = '-' * 70

    for msg in py_worker_errors:
        print(sep1)
        print(c(_RED + _BOLD, 'WORKER ERROR'))
        print(sep2)
        print(msg)

    for label_text, items in (('FAIL', py_failures), ('ERROR', py_errors)):
        for tid, msg in items:
            print(sep1)
            print(f'{c(_RED + _BOLD, label_text)}: {tid}')
            print(sep2)
            print(msg)

    for ext in externals:
        for ext_tid, ext_output in ext.failures:
            print(sep1)
            print(f'{c(_RED + _BOLD, "FAIL")}: {ext_tid}')
            print(sep2)
            if ext_output:
                print(ext_output, end='' if ext_output.endswith('\n') else '\n')
        if ext.returncode != 0 and ext.unplaced:
            print(sep1)
            print(c(_RED + _BOLD, f'{ext.label.upper()} ERROR'))
            print(sep2)
            print(ext.unplaced, end='' if ext.unplaced.endswith('\n') else '\n')

    if py_unexpected_successes:
        print(sep1)
        print('Unexpected successes:')
        for tid in py_unexpected_successes:
            print(f'  {tid}')

    print(sep2)
    total_ran = py_run + sum(ext.run for ext in externals)
    count_word = 'test' if total_ran == 1 else 'tests'
    print(f'Ran {total_ran} {count_word} in {elapsed:.3f}s')

    all_timings.extend(t for ext in externals for t in ext.timings)
    all_timings.sort(reverse=True)
    slowest = all_timings[:5]
    if slowest:
        print()
        print(c(_DIM, 'Slowest tests:'))
        for t_elapsed, t_name in slowest:
            print(f'  {c(_DIM, f"{t_elapsed:.3f}s")}  {t_name}')

    print()

    python_ok = not (py_failures or py_errors or py_unexpected_successes or py_worker_errors)
    externals_ok = all(ext.ok for ext in externals)

    if not python_ok or not externals_ok:
        fail_parts: list[str] = []
        if py_failures:
            fail_parts.append(f'failures={len(py_failures)}')
        if py_errors:
            fail_parts.append(f'errors={len(py_errors)}')
        if py_unexpected_successes:
            fail_parts.append(f'unexpected successes={len(py_unexpected_successes)}')
        if py_worker_errors:
            fail_parts.append(f'worker errors={len(py_worker_errors)}')
        for ext in externals:
            if ext.failures:
                fail_parts.append(f'{ext.label.lower()} failures={len(ext.failures)}')
            elif ext.returncode != 0:
                fail_parts.append(f'{ext.label.lower()} failed')
        result = f'FAILED ({", ".join(fail_parts)})'
        print(c(_RED + _BOLD, result))
        return python_ok, externals_ok

    ok_msg = 'OK'
    if py_skipped:
        ok_msg += f' (skipped={py_skipped})'
    print(c(_GREEN + _BOLD, ok_msg))
    return True, True


def terminate_workers(pids: list[int], procs: list['subprocess.Popen[bytes]'], externals: Sequence[ExternalTestProc]) -> None:
    """Kill any still running workers.

    Workers run in their own sessions, so they do not get the SIGINT/SIGQUIT
    the terminal sends to the foreground job when the user interrupts the test
    run. Without this they would be left behind as orphans.
    """
    import signal

    for pid in pids:
        # waitpid() fails with ChildProcessError if pid was already reaped, which
        # also guarantees we never signal an unrelated process that reused it
        with suppress(ChildProcessError, OSError):
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                os.kill(pid, signal.SIGKILL)
                os.waitpid(pid, 0)
    # Popen.kill() is a no-op once the process has been reaped
    for proc in procs:
        proc.kill()
        proc.wait()
    for ext in externals:
        ext.kill()


def run_tests(report_env: bool = False) -> None:
    report_env = report_env or is_ci
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        'name',
        nargs='*',
        default=[],
        help='The name of the test to run, for e.g. linebuf corresponds to test_linebuf. Can be specified multiple times.'
        ' For go tests Something corresponds to TestSometing. For Rust tests something corresponds to the test'
        ' function whose last path segment it is, for example tests::something.',
    )
    parser.add_argument('--verbosity', default=4, type=int, help='Test verbosity')
    parser.add_argument(
        '--module',
        default='',
        help='Name of a test module to restrict to. For example: ssh. For Go tests this is the name of a package, for example: tools/cli.'
        f' {RUST_MODULE} selects the Rust tests.',
    )
    args = parser.parse_args()
    if args.name and args.name[0] in ('type-check', 'type_check', 'mypy'):
        type_check()

    # Collect and filter all python tests upfront before any forking
    all_tests = find_all_tests()
    if args.module:
        all_tests = filter_tests_by_module(all_tests, args.module)
    if args.name:
        all_tests = filter_tests_by_name(all_tests, *args.name)
    tests_list = list(itertests(all_tests))

    go_pkgs = reduce_go_pkgs(args.module, args.name)
    has_go = bool(go_pkgs)
    has_rust = rust_tests_selected(args.module)
    # cargo is found before any worker or external process starts, so a missing cargo leaves none behind
    cargo_cmd = cargo_test_cmd() if has_rust else []
    rust_paths = resolve_rust_tests(cargo_cmd, args.name) if has_rust and args.name else []
    has_rust = has_rust and (bool(rust_paths) or not args.name)
    os.environ['ASAN_OPTIONS'] = 'detect_leaks=0'

    # Validate filters before doing any work
    if not tests_list and not has_go and not has_rust:
        if args.name:
            raise SystemExit(f'No test named {" ".join(args.name)} found' + (f' in module {args.module}' if args.module else ''))
        if args.module:
            raise SystemExit(f'No test module named {args.module} found')

    if report_env:
        report_test_env()

    # Start Python workers; each worker calls env_for_python_tests independently
    # for full HOME/XDG isolation, as does the serial path below.
    # On macOS fork()+threading is unsafe, so use subprocess workers there.
    use_parallel = len(tests_list) > PARALLEL_THRESHOLD
    worker_pids: list[int] = []
    worker_procs: list[subprocess.Popen[bytes]] = []
    read_fds: list[int] = []
    if use_parallel:
        if sys.platform == 'darwin':
            worker_procs, read_fds = spawn_test_workers(tests_list)
        else:
            # Pre-initialize fonts once before forking so all worker processes inherit
            # the warm C-level fontconfig state and their own all_fonts_map() calls are fast.
            from kitty.fonts.common import all_fonts_map

            all_fonts_map(True)
            worker_pids, read_fds = fork_test_workers(tests_list)

    # Launch the external suites immediately so they run in parallel with Python env setup and tests.
    # The launch is inside the try, so one that fails still terminates the workers and any external already started.
    externals: list[ExternalTestProc] = []
    externals_ok = True
    try:
        if has_go:
            if report_env:
                print('Go executable:', go_exe())
                print('Go packages being tested:', ' '.join(go_pkgs))
            externals.append(run_go(go_pkgs, args.name))
        if has_rust:
            externals.append(run_cargo(cargo_cmd, rust_paths))
        sys.stdout.flush()

        # Module filter with no python tests but external tests present: run those only
        if args.module and not tests_list:
            _, externals_ok = collect_worker_results([], [], [], 0, externals=externals)
            raise SystemExit(0 if externals_ok else 1)

        if use_parallel:
            python_ok, externals_ok = collect_worker_results(worker_pids, worker_procs, read_fds, len(tests_list), externals=externals)
        elif tests_list:
            # we need fonts installed in the user home directory as well, so initialize
            # fontconfig before nuking $HOME and friends
            from kitty.fonts.common import all_fonts_map

            all_fonts_map(True)
            # run_test_worker() runs the tests with env_for_python_tests() as
            # they must never run against the real $HOME, some of them delete
            # files/directories in it.
            python_ok = run_serial_tests(tests_list, args.verbosity)
            if externals:
                _, externals_ok = collect_worker_results([], [], [], 0, externals=externals)
        else:
            python_ok = True
            if externals:
                _, externals_ok = collect_worker_results([], [], [], 0, externals=externals)
    except BaseException:
        terminate_workers(worker_pids, worker_procs, externals)
        raise

    exit_code = 0 if (python_ok and externals_ok) else 1

    if exit_code != 0:
        print('\x1b[31mError\x1b[39m: Some tests failed!')
    raise SystemExit(exit_code)


def main() -> None:
    import warnings

    warnings.simplefilter('error')
    run_tests()
