#!/usr/bin/env python

import json

from .base import BaseTest
from .external import CargoTextParser, GoJsonParser, Outcome, OutputParser, Record, TestRecord, UnitFailure


def parse(parser: OutputParser, text: str) -> tuple[list[Record], str]:
    records: list[Record] = []
    for line in text.splitlines():
        records.extend(parser.feed(line))
    pending, unplaced = parser.finish()
    return records + pending, unplaced


def unit_failure(record: Record) -> UnitFailure:
    if not isinstance(record, UnitFailure):
        raise AssertionError(f'expected a UnitFailure, got {record!r}')
    return record


def go_events(*events: dict[str, object]) -> str:
    return '\n'.join(json.dumps(e) for e in events)


class TestExternalOutput(BaseTest):
    def test_go_test_results(self) -> None:
        records, _ = parse(
            GoJsonParser(),
            go_events(
                {'Action': 'run', 'Package': 'p', 'Test': 'TestA'},
                {'Action': 'output', 'Package': 'p', 'Test': 'TestA', 'Output': 'boom\n'},
                {'Action': 'fail', 'Package': 'p', 'Test': 'TestA', 'Elapsed': 0.5},
                {'Action': 'pass', 'Package': 'p', 'Test': 'TestB', 'Elapsed': 0.25},
                {'Action': 'fail', 'Package': 'p'},
            ),
        )
        # the package failed because TestA did, so it is not a failure of its own
        self.ae(records, [TestRecord('p.TestA', Outcome.failed, 0.5, 'boom\n'), TestRecord('p.TestB', Outcome.passed, 0.25, '')])

    def test_go_package_that_does_not_build(self) -> None:
        # Recorded from go 1.26: one package with a failing test, another that does not compile
        records, _ = parse(
            GoJsonParser(),
            go_events(
                {'ImportPath': 'gp/b [gp/b.test]', 'Action': 'build-output', 'Output': '# gp/b\n'},
                {'ImportPath': 'gp/b [gp/b.test]', 'Action': 'build-output', 'Output': 'b/b.go:3:1: syntax error\n'},
                {'ImportPath': 'gp/b [gp/b.test]', 'Action': 'build-fail'},
                {'Action': 'fail', 'Package': 'gp/a', 'Test': 'TestA', 'Elapsed': 0},
                {'Action': 'fail', 'Package': 'gp/a'},
                {'Action': 'output', 'Package': 'gp/b', 'Output': 'FAIL\tgp/b [build failed]\n'},
                {'Action': 'fail', 'Package': 'gp/b', 'FailedBuild': 'gp/b [gp/b.test]'},
            ),
        )
        self.ae(
            records,
            [
                TestRecord('gp/a.TestA', Outcome.failed, 0.0, ''),
                UnitFailure('gp/b', '# gp/b\nb/b.go:3:1: syntax error\nFAIL\tgp/b [build failed]\n'),
            ],
        )

    def test_go_dependency_that_does_not_build(self) -> None:
        # Both packages import gp/u, whose compile error is each one's failure
        records, unplaced = parse(
            GoJsonParser(),
            go_events(
                {'ImportPath': 'gp/u', 'Action': 'build-output', 'Output': 'u/u.go:3:1: syntax error\n'},
                {'ImportPath': 'gp/u', 'Action': 'build-fail'},
                {'ImportPath': 'gp/orphan', 'Action': 'build-output', 'Output': 'orphaned\n'},
                {'Action': 'fail', 'Package': 'gp/a', 'FailedBuild': 'gp/u'},
                {'Action': 'fail', 'Package': 'gp/b', 'FailedBuild': 'gp/u'},
                {'Action': 'output', 'Package': 'gp/c', 'Output': 'never finished\n'},
            ),
        )
        self.ae(records, [UnitFailure('gp/a', 'u/u.go:3:1: syntax error\n'), UnitFailure('gp/b', 'u/u.go:3:1: syntax error\n')])
        self.ae(unplaced, 'orphaned\nnever finished\n')

    def test_go_text_outside_json(self) -> None:
        _, unplaced = parse(GoJsonParser(), 'go: downloading x\n1\n' + go_events({'Action': 'pass', 'Package': 'p', 'Test': 'TestA'}))
        self.ae(unplaced, 'go: downloading x\n1\n')

    def test_cargo_test_results(self) -> None:
        records, unplaced = parse(
            CargoTextParser(),
            """\
   Compiling kitty_rs v0.1.0
    Finished `test` profile [unoptimized + debuginfo] target(s) in 0.28s
     Running unittests src/lib.rs (build/rust/debug/deps/kitty_rs-b5203e56d9ead203)

running 4 tests
test t::a ... ok
test t::ab ... FAILED
test t::c ... ignored
test t::p - should panic ... FAILED

failures:

---- t::ab stdout ----

thread 't::ab' panicked at src/lib.rs:9:20:
boom

---- t::p stdout ----
note: test did not panic as expected at src/lib.rs:14:8

failures:
    t::ab
    t::p

test result: FAILED. 1 passed; 2 failed; 1 ignored; 0 measured; 0 filtered out; finished in 0.00s

error: test failed, to rerun pass `--lib`
""",
        )
        target = 'unittests src/lib.rs'
        self.ae(
            records,
            [
                TestRecord(f'{target}::t::a', Outcome.passed, None, ''),
                TestRecord(f'{target}::t::c', Outcome.skipped, None, ''),
                TestRecord(f'{target}::t::ab', Outcome.failed, None, "\nthread 't::ab' panicked at src/lib.rs:9:20:\nboom\n\n"),
                TestRecord(f'{target}::t::p', Outcome.failed, None, 'note: test did not panic as expected at src/lib.rs:14:8\n\n'),
            ],
        )
        # the build before the first binary succeeded, so it is not an error to show
        self.ae(unplaced, '')

    def test_cargo_binary_that_crashes_after_a_failure(self) -> None:
        # libtest prints failure output only at the end, so a crash leaves just the FAILED line
        records, _ = parse(
            CargoTextParser(),
            """\
     Running unittests src/lib.rs (build/rust/debug/deps/kitty_rs-b5203e56d9ead203)

running 3 tests
test t::a ... FAILED
error: test failed, to rerun pass `--lib`

Caused by:
  process didn't exit successfully: `kitty_rs-b5203e56d9ead203` (signal: 6, SIGABRT: process abort signal)
""",
        )
        self.ae([type(r) for r in records], [TestRecord, UnitFailure])
        crash = unit_failure(records[1])
        self.ae(crash.unit, 'unittests src/lib.rs')
        self.assertIn('SIGABRT', crash.output)

    def test_cargo_compile_error(self) -> None:
        records, unplaced = parse(
            CargoTextParser(),
            """\
   Compiling kitty_rs v0.1.0
error[E0308]: mismatched types
error: could not compile `kitty_rs` (lib test) due to 1 previous error
""",
        )
        self.ae(len(records), 1)
        failure = unit_failure(records[0])
        self.ae(failure.unit, 'cargo build')
        self.assertIn('error[E0308]', failure.output)
        self.ae(unplaced, '')
