#!/usr/bin/env python
# License: GPL v3 Copyright: 2026, Kovid Goyal <kovid at kovidgoyal.net>

import os
import signal
import subprocess
import tempfile
from contextlib import redirect_stderr
from io import StringIO
from unittest import TestResult
from unittest.mock import patch

from kitty.child import Child, KittyWrapper, children_of_process, cmdline_of_pid, cwd_of_process, memory_used_by_process_tree_rooted_at
from kitty.constants import is_macos, kitty_exe

from .base import PTY, BaseTest, Exited, KilledAtDeadline, ReapedElsewhere, await_exit, await_pid_exit


def spawn_and_wait_for_ready(code: str, what: str) -> subprocess.Popen:
    # Run code via kitty +runpy and wait for it to print ready. stderr is
    # captured, as in test workers it is /dev/null, so that the reason the
    # child failed is reported. It goes to a file, not a pipe, so a child
    # writing a long traceback never blocks on a full pipe and looks wedged.
    with tempfile.TemporaryFile() as stderr:
        p = subprocess.Popen([kitty_exe(), '+runpy', code], stdout=subprocess.PIPE, stderr=stderr)
        line = p.stdout.readline().strip()
        p.stdout.close()
        if line != b'ready':
            end = await_exit(p)
            stderr.seek(0)
            err = stderr.read().decode('utf-8', 'replace')
            raise AssertionError(f'Unexpected output from {what}: {line!r}, it {end}, stderr:\n{err}')
    return p


class ChildMemoryTest(BaseTest):
    def _spawn_allocating_child(self, alloc_bytes: int) -> subprocess.Popen:
        return spawn_and_wait_for_ready(
            f"""\
import sys, time
buf = bytearray({alloc_bytes})
for i in range(0, {alloc_bytes}, 4096):
    buf[i] = 1
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(300)
""",
            'allocating child',
        )

    def _terminate(self, p: subprocess.Popen) -> None:
        p.terminate()
        p.wait()

    def test_memory_returns_positive_for_live_process(self):
        mem = memory_used_by_process_tree_rooted_at(os.getpid())
        self.assertGreater(mem, 0)

    def test_memory_returns_minus_one_for_nonexistent_pid(self):
        self.ae(memory_used_by_process_tree_rooted_at(99999999), -1)

    def test_memory_accounts_for_child_allocation(self):
        # Verify that a child's known resident allocation shows up in the
        # measurement.  check_if_cgroup_root=True falls back to per-process
        # tree walk when pid is not the cgroup root (the common case when
        # running under a shared session cgroup), so this exercises the tree
        # walk path on Linux and the always-tree-walk path on macOS.
        alloc = 20 * 1024 * 1024  # 20 MiB
        child = self._spawn_allocating_child(alloc)
        try:
            mem = memory_used_by_process_tree_rooted_at(child.pid, check_if_cgroup_root=True)
            self.assertGreater(
                mem,
                alloc // 2,
                f'Expected at least {alloc // 2} bytes for a {alloc}-byte allocation, got {mem}',
            )
        finally:
            self._terminate(child)

    def test_memory_of_parent_tree_includes_child(self):
        # Measuring the current process tree must yield more than measuring
        # the child alone, because the test runner itself occupies memory.
        alloc = 20 * 1024 * 1024  # 20 MiB
        child = self._spawn_allocating_child(alloc)
        try:
            mem_child = memory_used_by_process_tree_rooted_at(child.pid, check_if_cgroup_root=True)
            mem_tree = memory_used_by_process_tree_rooted_at(os.getpid(), check_if_cgroup_root=True)
            self.assertGreater(
                mem_tree,
                mem_child,
                'Parent tree memory should exceed child-only memory',
            )
        finally:
            self._terminate(child)

    def test_memory_cgroup_path_returns_positive(self):
        # The fast cgroup path (check_if_cgroup_root=False, the default) must
        # return a usable value on Linux.
        if is_macos:
            self.skipTest('cgroup not available on macOS')
        mem = memory_used_by_process_tree_rooted_at(os.getpid())
        self.assertGreater(mem, 0)

    def test_memory_cgroup_and_tree_walk_both_positive(self):
        # Both the cgroup path and the tree-walk path should give positive
        # results for a process that is alive.
        if is_macos:
            self.skipTest('cgroup path not applicable on macOS')
        alloc = 10 * 1024 * 1024  # 10 MiB
        child = self._spawn_allocating_child(alloc)
        try:
            mem_cgroup = memory_used_by_process_tree_rooted_at(child.pid, check_if_cgroup_root=False)
            mem_walk = memory_used_by_process_tree_rooted_at(child.pid, check_if_cgroup_root=True)
            self.assertGreater(mem_cgroup, 0)
            self.assertGreater(mem_walk, 0)
        finally:
            self._terminate(child)


class ChildRootTest(BaseTest):
    def _spawn_wrapper(self, child_cwd: str, start_child: bool = True) -> subprocess.Popen:
        # A process that, like /usr/bin/login, runs a child and waits for it.
        # The child is started from a non-main thread, as kitten does.
        code = f"""\
import sys, threading, time
def run_child(started):
    import subprocess, sys, time
    subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(300)'], cwd={child_cwd!r},
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    started.set()
    time.sleep(300)  # keep the thread alive so the child stays its child
if {start_child!r}:
    started = threading.Event()
    threading.Thread(target=run_child, args=(started,), daemon=True).start()
    if not started.wait(10):
        raise SystemExit('Failed to start child')
sys.stdout.write("ready\\n")
sys.stdout.flush()
time.sleep(300)
"""
        return spawn_and_wait_for_ready(code, 'wrapper')

    def _terminate_tree(self, p: subprocess.Popen) -> None:
        children = children_of_process(p.pid)
        p.terminate()
        p.wait()
        for c in children:
            try:
                os.kill(c, 15)
            except ProcessLookupError:
                pass

    def _child_for(self, pid: int, *wrappers: KittyWrapper) -> Child:
        c = Child(['unused'], '/')
        c.pid = pid
        c.kitty_wrappers = wrappers
        return c

    def test_root_pid(self):
        with tempfile.TemporaryDirectory() as tdir:
            tdir = os.path.realpath(tdir)
            p = self._spawn_wrapper(tdir)
            try:
                children = children_of_process(p.pid)
                self.ae(len(children), 1)
                inner = children[0]
                self.ae(cwd_of_process(inner), tdir)
                self.assertNotEqual(cwd_of_process(p.pid), tdir)
                # Not launched via a wrapper, the original process is root
                c = self._child_for(p.pid)
                self.ae(c.root_pid, p.pid)
                self.ae(c.current_cwd, cwd_of_process(p.pid))
                # A wrapper that never replaces itself is always skipped
                c = self._child_for(p.pid, KittyWrapper(('/usr/bin/login', '-f'), False))
                self.ae(c.root_pid, inner)
                self.ae(c.current_cwd, tdir)
                # A wrapper that replaces itself is skipped only while it is
                # still running as the wrapper
                argv = tuple(cmdline_of_pid(p.pid))
                c = self._child_for(p.pid, KittyWrapper(argv[:2], True))
                self.ae(c.root_pid, inner)
                c = self._child_for(p.pid, KittyWrapper(('/some/other/wrapper',), True))
                self.ae(c.root_pid, p.pid)
                # Skipping stops at the first process that is not a wrapper
                c = self._child_for(p.pid, KittyWrapper(argv[:2], True), KittyWrapper(('/some/other/wrapper',), True))
                self.ae(c.root_pid, inner)
            finally:
                self._terminate_tree(p)

    def test_root_pid_caching(self):
        with tempfile.TemporaryDirectory() as tdir:
            p = self._spawn_wrapper(tdir)
            try:
                inner = children_of_process(p.pid)[0]
                argv = tuple(cmdline_of_pid(p.pid))
                # No wrappers, nothing to resolve
                c = self._child_for(p.pid)
                self.ae(c.root_pid, p.pid)
                self.assertIsNone(c.resolved_root_pid)
                # A wrapper that never replaces itself has a fixed child
                c = self._child_for(p.pid, KittyWrapper(('/usr/bin/login',), False))
                self.ae(c.root_pid, inner)
                self.ae(c.resolved_root_pid, inner)
                # A wrapper that will replace itself, its child is cached but
                # not the result
                wrapping = KittyWrapper(argv[:2], True)
                c = self._child_for(p.pid, wrapping)
                self.ae(c.root_pid, inner)
                self.assertIsNone(c.resolved_root_pid)
                self.ae(c.wrapper_children, {p.pid: inner})
                # Once it has replaced itself, the result is final
                replaced = KittyWrapper(('/not/the/wrapper/anymore',), True)
                c.kitty_wrappers = (replaced,)
                self.ae(c.root_pid, p.pid)
                self.ae(c.resolved_root_pid, p.pid)
                self.ae(c.wrapper_children, {})
                # Not looking like the wrapper without ever having looked like
                # it, means it might not have been exec-ed yet, so dont cache
                c = self._child_for(p.pid, replaced)
                self.ae(c.root_pid, p.pid)
                self.assertIsNone(c.resolved_root_pid)
                # The original process properties use the root process
                c = self._child_for(p.pid, KittyWrapper(('/usr/bin/login',), False))
                self.ae(c.current_cwd, os.path.realpath(tdir))
                self.ae(c.cmdline, cmdline_of_pid(inner))
            finally:
                self._terminate_tree(p)

    def test_descendants_from_threads(self):
        if is_macos:
            self.skipTest('Per thread children lists are Linux only')
        from kitty import child

        with tempfile.TemporaryDirectory() as tdir:
            p = self._spawn_wrapper(tdir)
            try:
                inner = children_of_process(p.pid)
                self.ae(len(inner), 1)
                self.ae(child._get_descendants_of(p.pid), set(inner))
                # Kernels without /proc/<pid>/task/<tid>/children
                orig = child.proc_children_supported
                child.proc_children_supported = lambda: False  # type: ignore
                try:
                    self.ae(children_of_process(p.pid), inner)
                    self.ae(child._get_descendants_of(p.pid), set(inner))
                finally:
                    child.proc_children_supported = orig
            finally:
                self._terminate_tree(p)

    def test_root_pid_wrapper_without_child(self):
        p = self._spawn_wrapper('/', start_child=False)
        try:
            self.ae(children_of_process(p.pid), [])
            c = self._child_for(p.pid, KittyWrapper(('/usr/bin/login',), False))
            self.ae(c.root_pid, p.pid)
        finally:
            self._terminate_tree(p)


class AwaitExitTest(BaseTest):
    def _spawn(self, code: str) -> subprocess.Popen:
        p = subprocess.Popen([kitty_exe(), '+runpy', code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.ae(p.stdout.read(), b'')
        p.stdout.close()
        return p

    def test_a_child_that_exits_without_printing_reports_its_status(self):
        p = self._spawn('raise SystemExit(0)')
        self.ae(await_exit(p), Exited(0))
        self.ae(p.wait(), 0)

    def test_a_child_reaped_elsewhere_is_not_reported_as_exit_0(self):
        p = self._spawn('raise SystemExit(3)')
        status = os.waitpid(p.pid, 0)[1]
        self.ae(await_exit(p), ReapedElsewhere())
        p.returncode = os.waitstatus_to_exitcode(status)  # the waiter that reaped it owns the status

    def test_a_child_that_lives_on_after_its_output_ends_is_killed_at_the_deadline(self):
        p = self._spawn('import os, time; os.close(1); time.sleep(300)')
        self.ae(await_exit(p, patience=0.2), KilledAtDeadline(0.2, -signal.SIGKILL))
        self.ae(p.returncode, -signal.SIGKILL)

    def _first_poll_misses_the_exit(self):
        # Stages the race where the child ends between await_exit's last poll and its kill
        real_waitpid, polls = os.waitpid, []
        def waitpid(pid, options):
            polls.append(options)
            return (0, 0) if len(polls) == 1 else real_waitpid(pid, options)
        return patch('kitty_tests.base.os.waitpid', waitpid)

    def test_a_child_that_exits_just_before_the_kill_reports_its_own_status(self):
        p = self._spawn('raise SystemExit(5)')
        os.waitid(os.P_PID, p.pid, os.WEXITED | os.WNOWAIT)  # WNOWAIT leaves it a zombie, unreaped
        with self._first_poll_misses_the_exit():
            self.ae(await_exit(p, patience=0), Exited(5))

    def test_a_child_reaped_elsewhere_just_before_the_kill_is_not_reported_as_killed(self):
        p = self._spawn('raise SystemExit(3)')
        status = os.waitpid(p.pid, 0)[1]
        with self._first_poll_misses_the_exit():
            self.ae(await_exit(p, patience=0), ReapedElsewhere())
        p.returncode = os.waitstatus_to_exitcode(status)

    def test_a_signal_with_no_name_is_reported_by_number(self):
        self.ae(str(Exited(-200)), 'was killed by signal 200')

    def test_a_signal_death_names_the_signal(self):
        self.ae(str(Exited(-signal.SIGSEGV)), 'was killed by signal SIGSEGV')


class PTYTeardownTest(BaseTest):
    def _reaped_here(self, pty: PTY) -> None:
        self.ae(await_pid_exit(pty.child_pid, patience=30 if self.is_ci else 10), Exited(0))

    def test_a_with_block_whose_child_was_reaped_elsewhere_fails_naming_it(self):
        pty = PTY([kitty_exe(), '+runpy', 'raise SystemExit(0)'])
        self._reaped_here(pty)
        with self.assertRaises(AssertionError) as cm, pty:
            pass
        self.ae(str(cm.exception), f'PTY child {pty.child_pid} {ReapedElsewhere()}')

    def test_a_created_pty_whose_child_was_reaped_elsewhere_fails_its_test_naming_it(self):
        pids = []

        class ReapsItsPTYChild(BaseTest):
            def runTest(inner) -> None:
                pty = inner.create_pty([kitty_exe(), '+runpy', 'raise SystemExit(0)'])
                pids.append(pty.child_pid)
                self._reaped_here(pty)

        ReapsItsPTYChild().run(result := TestResult())
        self.ae([msg.splitlines()[-1] for _, msg in result.failures + result.errors], [f'AssertionError: PTY child {pids[0]} {ReapedElsewhere()}'])

    def test_a_failing_with_block_keeps_its_failure_and_notes_the_reap(self):
        with self.assertRaises(ValueError) as cm, PTY([kitty_exe(), '+runpy', 'raise SystemExit(0)']) as pty:
            self._reaped_here(pty)
            raise ValueError('the test failed')
        self.ae(cm.exception.__notes__, [f'PTY child {pty.child_pid} {ReapedElsewhere()}'])

    def test_a_collected_pty_whose_child_was_reaped_elsewhere_names_it_on_stderr(self):
        pty = PTY([kitty_exe(), '+runpy', 'raise SystemExit(0)'])
        self._reaped_here(pty)
        with redirect_stderr(err := StringIO()):
            pty.__del__()
        self.ae(err.getvalue(), f'PTY child {pty.child_pid} {ReapedElsewhere()}\n')

    def test_a_pty_torn_down_twice_reaps_once_and_reports_nothing(self):
        with redirect_stderr(err := StringIO()):
            with PTY([kitty_exe(), '+runpy', 'raise SystemExit(0)']) as pty:
                pass
            pty.__del__()
        self.ae(err.getvalue(), '')
        self.assertRaises(ChildProcessError, os.waitpid, pty.child_pid, os.WNOHANG)


class WaitTillChildExitsTest(BaseTest):
    def _fails_naming(self, pty: PTY, *names: object, **kw: float) -> str:
        # the failure is named once, by the wait: neither the with block nor GC names it again
        with redirect_stderr(err := StringIO()):
            with pty, self.assertRaises(AssertionError) as cm:
                pty.wait_till_child_exits(**kw)
            pty.__del__()
        self.ae(err.getvalue(), '')
        head = str(cm.exception).partition('. ')[0]  # the screen contents follow
        for name in (f'PTY child {pty.child_pid}', *names):
            self.assertIn(str(name), head)
        return head

    def test_a_child_that_exits_with_the_required_status_returns_it(self) -> None:
        with PTY([kitty_exe(), '+runpy', 'raise SystemExit(0)']) as pty:
            self.ae(pty.wait_till_child_exits(require_exit_code=0), 0)

    def test_a_child_that_writes_more_than_the_pty_holds_still_exits(self) -> None:
        # a child blocked on a full PTY never exits, so the wait must read as it polls
        with PTY([kitty_exe(), '+runpy', 'import sys; sys.stdout.write("x" * 1_000_000); sys.stdout.flush()']) as pty:
            self.ae(pty.wait_till_child_exits(require_exit_code=0), 0)

    def test_a_child_with_the_wrong_status_fails_naming_both(self) -> None:
        self._fails_naming(PTY([kitty_exe(), '+runpy', 'raise SystemExit(3)']), Exited(3), 'exit status 0 was required', require_exit_code=0)

    def test_a_child_killed_by_a_signal_fails_naming_it(self) -> None:
        pty = PTY([kitty_exe(), '+runpy', 'import os, signal; os.kill(os.getpid(), signal.SIGKILL)'])
        self._fails_naming(pty, Exited(-signal.SIGKILL))

    def test_a_child_reaped_elsewhere_fails_naming_it_once(self) -> None:
        pty = PTY([kitty_exe(), '+runpy', 'raise SystemExit(0)'])
        self.ae(await_pid_exit(pty.child_pid, patience=30 if self.is_ci else 10), Exited(0))
        # no status was seen, so none is said to be wrong
        self.assertNotIn('required', self._fails_naming(pty, ReapedElsewhere(), require_exit_code=0))

    def test_a_child_still_running_at_the_timeout_is_killed_and_named(self) -> None:
        self._fails_naming(PTY([kitty_exe(), '+runpy', 'import time; time.sleep(300)']), KilledAtDeadline(0.2, -signal.SIGKILL), timeout=0.2)

    def test_a_child_that_never_stops_writing_is_killed_at_the_timeout(self) -> None:
        argv = [kitty_exe(), '+runpy', 'import sys\nwhile True: sys.stdout.write("x" * 65536)']
        self._fails_naming(PTY(argv), KilledAtDeadline(0.5, -signal.SIGKILL), timeout=0.5)

    def test_a_pid_that_names_a_process_group_is_refused(self) -> None:
        self.assertRaises(ValueError, await_pid_exit, 0, 1)
