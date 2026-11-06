---
name: run-kitty
description: Build this checkout's kitty on a test Mac (studious, else inferno), launch it, type and press keys into it, and screenshot its window back here. Use to run kitty, see a change working in the real app, or live-check a GUI behaviour such as the find bar.
---

# Run kitty

kitty runs on a test Mac, never on this one: it takes focus when it starts, and the keys are
real input that would land in whatever Brandon is typing into. `bin/run-kitty` picks
studious.local, else inferno.local, whichever first answers with cargo and go installed
(inferno has neither today), and exits if neither does. Running kitty on this Mac
then needs Brandon's go first, since he may be at the keyboard. `up` and `sync` record the
host they picked, and every later command goes to that host.

```bash
R=.claude/skills/run-kitty/bin/run-kitty   # from the checkout root
$R up                       # sync this checkout (uncommitted work too), make debug, launch
$R press leftCommand+f      # vhid chord names: leftCommand, leftShift, return, escape, delete, ...
$R type zebra
$R shot after-zebra         # prints the local png path; Read it
```

`launch` and `up` pass extra arguments to kitty, so `$R launch -o font_size=16 /tmp/fill.sh`
runs a program instead of the shell. The program's path is a path on the test Mac: put it
there with `scp` first.

Look at every screenshot. A command returning means the key was sent, not that kitty did
anything with it.

- **Redraws:** kitty draws only when something asks it to. The launch turns off cursor
  blink and parks the pointer off the window, because a blink or a mouse move would draw
  the frame and hide a redraw that is missing. Don't move the pointer over the window
  before a `shot` unless the mouse is what you are testing.
- **Settling:** `shot` waits `KITTY_RUN_SETTLE` seconds (0.4 by default) before it
  captures.
- **Rust crates:** cargo on studious can't reach crates.io, even though curl can. `build`
  vendors the crates here once per `Cargo.lock` and points cargo on the test Mac at them.
- **Per checkout:** each checkout gets its own copy on the test Mac (`~/code/kitty-run/<key>`,
  a git checkout that `sync` overwrites; never edit it there), its own kitty and its own
  shots, so sessions in sibling worktrees don't drive each other's kitty. A checkout's
  first `build` is a full one. `$R drop` stops kitty and deletes the copy; run it when the
  checkout's work is done.
- **Build failure:** a failed `build` prints the tail of its `build.log` on the test Mac.
