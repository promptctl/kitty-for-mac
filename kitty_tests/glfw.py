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


GLFW_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'glfw')
# Function-pointer types GLFW stores that are not callbacks into its client: its own event-loop
# plumbing, and functions it hands the client to call. Dynamically loaded library tables
# (PFN*) and GLFW's internal vtables (_GLFW*) are recognised by prefix.
GLFW_INTERNAL_FUNCTION_TYPES = frozenset((
    'watch_callback_func', 'timer_callback_func', 'dbus_pending_callback', 'GLFWuserdatafreefun',
    'GLFWdropreaddatafun', 'GLFWdropfinishfun', 'GLFWglproc', 'GLFWvkproc',
))


def _strip_c_comments_and_directives(src: str) -> str:
    src = re.sub(r'/\*.*?\*/', ' ', src, flags=re.DOTALL)
    src = re.sub(r'//[^\n]*', '', src)
    src = re.sub(r'"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])+\'', '""', src)
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
    sources = {}
    for name in sorted(os.listdir(glfw_dir)):
        if name.endswith(('.c', '.h', '.m')):
            with open(os.path.join(glfw_dir, name)) as f:
                sources[name] = _strip_c_comments_and_directives(f.read())
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
            for line in src.splitlines():
                if re.search(r'\.fn\s*\(', line):
                    problems.append(f'{name}: event invoked outside _glfwDeliver: {line.strip()}')
    return problems


class TestGLFWCallbackKinds(BaseTest):
    @unittest.skipUnless(os.path.isdir(GLFW_DIR), 'needs the glfw sources')
    def test_every_glfw_callback_declares_whether_its_delivery_ticks(self):
        # [LAW:single-enforcer] glfw/internal.h: an event delivered through _glfwDeliver requests the
        # tick; a callback that is not declared one or the other would skip that silently.
        self.assertEqual([], unclassified_glfw_callbacks())
