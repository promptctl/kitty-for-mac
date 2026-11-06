#!/usr/bin/env python3
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

# Stands in for Claude Code 2.1.299 on a tmux pane, for the tests of kitty/claude_find.py: the same
# alternate-screen views and the same keys, as measured on the real program (promptctl-find-aqo.3va).
# Run as: fake_claude.py CONVERSATION KEYLOG [dialog|sticky]
#   CONVERSATION  a text file, one line per line of the conversation; read again at each search
#   KEYLOG        every chunk of input is appended here as repr(), one per line
#   dialog        draw a permission dialog and ignore every key
#   sticky        n and N never move the counter, as when matches share a row
#
# The views and what a key does in each (P = prompt, T = transcript, S = search field, A = accepted search):
#   P: Ctrl+o -> T.   T: / -> S, Esc or Ctrl+o -> P.   S: text types, Enter -> A (or T when nothing matches), Esc -> T.
#   A: n / N step through the matches found at Enter and wrap, / -> S, Esc or Ctrl+o -> P.
# Input that arrives as more than one byte is pasted text: it types into the search field and is ignored
# everywhere else, which is why a '/' sent together with the text opens no search.

import os
import sys
import termios
import tty

COLS, ROWS = 100, 23


def main() -> None:
    conversation, keylog = sys.argv[1], sys.argv[2]
    dialog, sticky = (len(sys.argv) > 3 and sys.argv[3] == flag for flag in ('dialog', 'sticky'))
    state, query, matches, current = 'P', '', [], 0

    def lines() -> list[str]:
        with open(conversation) as f:
            return f.read().splitlines()

    def found(q: str) -> list[int]:
        return [i for i, line in enumerate(lines()) if q and q.lower() in line.lower()]

    def right(left: str, count: str) -> str:
        return left.ljust(COLS - len(count) - 2) + count

    def draw() -> None:
        conv = lines()
        rule = '─' * COLS
        if dialog:
            rows = ['Do you want to proceed?', '❯ 1. Yes', '  2. No, and tell Claude what to do differently']
        elif state == 'P':
            rows = conv[-(ROWS - 6):] + [rule, '❯ ', rule, '  🍫 polled  ~/code (main)', '  ⏵⏵ auto mode on (shift+tab to cycle)']
        else:
            at = matches[current] if state == 'A' else max(len(conv) - 1, 0)
            top = max(0, min(at - 5, len(conv) - (ROWS - 2)))
            shown = conv[top:top + ROWS - 2]
            if state == 'T':
                footer = '  Showing detailed transcript · ctrl+o to toggle · ↑↓ scroll · v to open in code · ? for shortcuts'.ljust(COLS)
            elif state == 'S':
                footer = right('  /' + query, f'{len(found(query))}/{len(found(query))}' if found(query) else 'no matches')
            else:
                footer = right('  Showing detailed transcript · ctrl+o to toggle · n/N to navigate', f'{current + 1}/{len(matches)}')
            rows = shown + [''] * (ROWS - 2 - len(shown)) + [rule, footer]
        sys.stdout.write('\x1b[H\x1b[2J' + '\r\n'.join(rows))
        sys.stdout.flush()

    sys.stdout.write('\x1b[?1049h')
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setraw(fd)
    try:
        draw()
        while chunk := os.read(fd, 4096):
            with open(keylog, 'a') as f:
                f.write(repr(chunk) + '\n')
            if dialog:
                continue
            single = len(chunk) == 1
            if state == 'P':
                if chunk == b'\x0f':
                    state = 'T'
            elif state in 'TA':
                if chunk in (b'\x1b', b'\x0f'):
                    state = 'P'
                elif chunk == b'/' and single:
                    state, query = 'S', ''
                elif state == 'A' and chunk in (b'n', b'N') and not sticky:
                    current = (current + (1 if chunk == b'n' else -1)) % len(matches)
            elif state == 'S':
                if chunk == b'\x1b':
                    state = 'T'
                elif chunk == b'\r':
                    matches, current = found(query), 0
                    state = 'A' if matches else 'T'
                else:
                    query += chunk.decode()
            draw()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write('\x1b[?1049l')


if __name__ == '__main__':
    main()
