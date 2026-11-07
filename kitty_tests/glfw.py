#!/usr/bin/env python
# License: GPL v3 Copyright: 2020, Kovid Goyal <kovid at kovidgoyal.net>

import os
import re
import sys
import unittest

from .base import BaseTest

_plat = sys.platform.lower()
is_macos = 'darwin' in _plat


class TestGLFW(BaseTest):
    def test_os_window_size_calculation(self):
        from kitty.utils import get_new_os_window_size

        def t(w, h, width=0, height=0, unit='cells', incremental=False):
            self.ae((w, h), get_new_os_window_size(metrics, width, height, unit, incremental, has_window_scaling))

        with self.subTest(has_window_scaling=False):
            has_window_scaling = False
            metrics = {
                'width': 200,
                'height': 100,
                'framebuffer_width': 200,
                'framebuffer_height': 100,
                'xscale': 2.0,
                'yscale': 2.0,
                'xdpi': 192.0,
                'ydpi': 192.0,
                'cell_width': 8,
                'cell_height': 16,
            }
            t(80 * metrics['cell_width'], 100, 80)
            t(80 * metrics['cell_width'] + metrics['width'], 100, 80, incremental=True)
            t(1217, 100, 1217, unit='pixels')
            t(1217 + metrics['width'], 100, 1217, unit='pixels', incremental=True)

        with self.subTest(has_window_scaling=True):
            has_window_scaling = True
            metrics['framebuffer_width'] = metrics['width'] * 2
            metrics['framebuffer_height'] = metrics['height'] * 2
            t(80 * metrics['cell_width'] / metrics['xscale'], 100, 80)
            t(80 * metrics['cell_width'] / metrics['xscale'] + metrics['width'], 100, 80, incremental=True)
            t(1217, 100, 1217, unit='pixels')
            t(1217 + metrics['width'], 100, 1217, unit='pixels', incremental=True)

    @unittest.skipIf(is_macos, 'Skipping test on macOS because glfw-cocoa.so is not built with backend_utils')
    def test_utf_8_strndup(self):
        import ctypes

        from kitty.constants import glfw_path

        backend_utils = glfw_path('x11')
        lib = ctypes.CDLL(backend_utils)
        libc = ctypes.CDLL(None)

        class allocated_c_char_p(ctypes.c_char_p):
            def __del__(self):
                libc.free(self)

        utf_8_strndup = lib.utf_8_strndup
        utf_8_strndup.restype = allocated_c_char_p
        utf_8_strndup.argtypes = (ctypes.c_char_p, ctypes.c_size_t)

        def test(string):
            string_bytes = bytes(string, 'utf-8')
            prev_part_bytes = b''
            prev_length_bytes = -1
            for length in range(len(string) + 1):
                part = string[:length]
                part_bytes = bytes(part, 'utf-8')
                length_bytes = len(part_bytes)
                for length_bytes_2 in range(prev_length_bytes + 1, length_bytes):
                    self.ae(utf_8_strndup(string_bytes, length_bytes_2).value, prev_part_bytes)
                self.ae(utf_8_strndup(string_bytes, length_bytes).value, part_bytes)
                prev_part_bytes = part_bytes
                prev_length_bytes = length_bytes
            self.ae(utf_8_strndup(string_bytes, len(string_bytes) + 1).value, string_bytes)  # Try to go one character after the end of the string

        self.ae(utf_8_strndup(None, 2).value, None)
        self.ae(utf_8_strndup(b'', 2).value, b'')

        test('ö')
        test('>a<')
        test('>ä<')
        test('>ế<')
        test('>𐍈<')
        test('∮ E⋅da = Q,  n → ∞, 𐍈∑ f(i) = ∏ g(i)')
        self.ae(utf_8_strndup(b'\xf0\x9f\x98\xb8', 3).value, b'')
        self.ae(utf_8_strndup(b'\xc3\xb6\xf0\x9f\x98\xb8', 4).value, b'\xc3\xb6')

    def _two_due_timers_each_acting_on_the_other(self, act):
        # Both timers are due in the same round and each one's callback calls act(lib, eld, other_id). Returns
        # what dispatchTimers returned, the ids called and the (id, data) pairs freed once all timers are removed.
        import ctypes

        from kitty.constants import glfw_path

        lib = ctypes.CDLL(glfw_path('x11'))
        timer_func = ctypes.CFUNCTYPE(None, ctypes.c_ulonglong, ctypes.c_void_p)
        lib.addTimer.restype = ctypes.c_ulonglong
        lib.addTimer.argtypes = (
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int64, ctypes.c_int, ctypes.c_bool, timer_func, ctypes.c_void_p, timer_func)
        lib.removeTimer.argtypes = (ctypes.c_void_p, ctypes.c_ulonglong)
        lib.toggleTimer.argtypes = (ctypes.c_void_p, ctypes.c_ulonglong, ctypes.c_int)
        lib.dispatchTimers.restype = ctypes.c_uint
        lib.dispatchTimers.argtypes = (ctypes.c_void_p,)
        lib.removeAllTimers.argtypes = (ctypes.c_void_p,)
        eld = ctypes.create_string_buffer(1 << 20)  # a zeroed EventLoopData: no watches, no timers
        ids, called, freed = [], [], []

        @timer_func
        def act_on_the_other(timer_id, data):
            called.append(timer_id)
            act(lib, eld, ids[1] if timer_id == ids[0] else ids[0])

        @timer_func
        def free(timer_id, data):
            freed.append((timer_id, data))

        for data in (1, 2):
            ids.append(lib.addTimer(eld, b'test', 0, 1, True, act_on_the_other, data, free))
        fired = lib.dispatchTimers(eld)
        lib.removeAllTimers(eld)
        return fired, called, sorted(freed), ids

    @unittest.skipIf(is_macos, 'Skipping test on macOS because glfw-cocoa.so is not built with backend_utils')
    def test_timer_removed_earlier_in_its_round_is_not_called(self):
        # Whichever fires first removes the other, so the second is not called and each timer's data is freed once.
        fired, called, freed, ids = self._two_due_timers_each_acting_on_the_other(lambda lib, eld, other: lib.removeTimer(eld, other))
        self.ae((fired, len(called)), (1, 1))
        self.ae(freed, [(ids[0], 1), (ids[1], 2)])

    @unittest.skipIf(is_macos, 'Skipping test on macOS because glfw-cocoa.so is not built with backend_utils')
    def test_timer_cancelled_earlier_in_its_round_is_not_called(self):
        fired, called, freed, ids = self._two_due_timers_each_acting_on_the_other(lambda lib, eld, other: lib.toggleTimer(eld, other, 0))
        self.ae((fired, len(called)), (1, 1))


GLFW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'glfw')
# Function-pointer types GLFW stores that are not callbacks into its client: its own event-loop
# plumbing, and functions it hands the client to call. Dynamically loaded library tables
# (PFN*) and GLFW's internal vtables (_GLFW*) are recognised by prefix.
GLFW_INTERNAL_FUNCTION_TYPES = frozenset((
    'watch_callback_func', 'timer_callback_func', 'dbus_pending_callback', 'GLFWuserdatafreefun',
    'GLFWdropreaddatafun', 'GLFWdropfinishfun', 'GLFWglproc', 'GLFWvkproc',
))


def _strip_c_comments(src: str) -> str:
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.DOTALL)
    src = re.sub(r'//[^\n]*', '', src)
    return re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])+\'', '""', src)


def _strip_c_directives(src: str) -> str:
    # preprocessor lines, with their continuations
    return re.sub(r'^[ \t]*#(?:[^\n]*\\\n)*[^\n]*', '', src, flags=re.MULTILINE)


def _storage_statements(src: str):
    # Yield the text of every statement that declares storage: struct and union fields, file-scope
    # variables, and static locals. A function body's ordinary locals are not storage.
    aggregate = re.compile(r'(?:\b(?:struct|union|enum)\b[\s\w]*|=\s*|\bextern\s*""\s*)$')  # extern "C" is a scope, not code
    stack: list[bool] = []  # True for an aggregate brace (struct body or initializer)
    start = 0
    for m in re.finditer(r'[{};]', src):
        ch = m.group()
        if ch == '{':
            stack.append(bool(aggregate.search(src[start:m.start()].rstrip())))
        elif ch == '}' and stack:  # both arms of an #if keep their braces once directives are gone
            stack.pop()
        else:
            stmt = src[start:m.start()].strip()
            if all(stack) or stmt.startswith('static'):
                yield stmt
        start = m.end()


def unclassified_glfw_callbacks(glfw_dir: str = GLFW_DIR) -> list[str]:
    with_macros, sources = {}, {}
    for name in sorted(os.listdir(glfw_dir)):
        if name.endswith(('.c', '.h', '.m')):
            with open(os.path.join(glfw_dir, name)) as f:
                with_macros[name] = _strip_c_comments(f.read())
                sources[name] = _strip_c_directives(with_macros[name])
    function_types = set()
    for src in sources.values():
        function_types.update(re.findall(r'\btypedef\b[^;]*?\(\s*\*\s*(\w+)\s*\)\s*\(', src))
    problems = []
    for name, src in sources.items():
        for stmt in _storage_statements(src):
            if stmt.startswith('typedef') or '_GLFW_EVENT(' in stmt or '_GLFW_NO_TICK(' in stmt:
                continue
            if re.search(r'\(\s*\*\s*\w+\s*\)\s*\(', stmt):
                problems.append(f'{name}: inline function-pointer declarator: {" ".join(stmt.split())}')
                continue
            m = re.match(r'(?:(?:static|const|volatile|extern|GLFWAPI)\s+)*(\w+)\s+\**\s*\w+\s*(?:\[[^\]]*\]\s*)*(?:=|$|,)', stmt)
            if m is None or m.group(1) not in function_types:
                continue
            t = m.group(1)
            if t.startswith(('PFN', '_GLFW')) or t in GLFW_INTERNAL_FUNCTION_TYPES:
                continue
            problems.append(f'{name}: callback {t} is neither _GLFW_EVENT nor _GLFW_NO_TICK: {" ".join(stmt.split())}')
        if name != 'callback_kinds.h':
            for line in with_macros[name].splitlines():  # a macro body can invoke an event too
                if re.search(r'(?:\.|->)\s*fn\s*\)*\s*\(', line):
                    problems.append(f'{name}: event invoked outside _glfwDeliver: {line.strip()}')
    # A client callback GLFW holds as an argument or a local is not storage, so its kind comes from
    # its type: a type some declaration makes an event is an event wherever its value travels.
    event_types = set()
    for src in sources.values():
        event_types.update(re.findall(r'_GLFW_EVENT\(\s*(\w+)\s*\)', src))
    for name, src in sources.items():
        for fn_name, params, body in _function_definitions(src):
            held = [*re.finditer(r'\b(\w+)\s+\**\s*(\w+)\s*(?:,|$)', params), *re.finditer(r'\b(\w+)\s+\**\s*(\w+)\s*[=;]', body)]
            for v in held:
                if v.group(1) in event_types and re.search(rf'(?<![\w.>]){v.group(2)}\s*\(', body):
                    problems.append(f'{name}: {fn_name} calls its {v.group(1)} {v.group(2)} outside _glfwDeliver')
    return problems


def _function_definitions(src: str):
    # a parameter list may hold one level of parentheses: a function-pointer parameter
    for m in re.finditer(r'\b(\w+)\s*\(((?:[^()]|\([^()]*\))*)\)\s*\{', src):
        depth, end = 1, m.end()
        while depth and end < len(src):
            depth += {'{': 1, '}': -1}.get(src[end], 0)
            end += 1
        yield m.group(1), m.group(2), src[m.end():end]


class TestGLFWCallbackKinds(BaseTest):
    @unittest.skipUnless(os.path.isdir(GLFW_DIR), 'needs the glfw sources')
    def test_every_glfw_callback_declares_whether_its_delivery_ticks(self):
        # [LAW:single-enforcer] glfw/internal.h: an event delivered through _glfwDeliver requests the
        # tick; a callback that is not declared one or the other would skip that silently.
        self.assertEqual([], unclassified_glfw_callbacks())
