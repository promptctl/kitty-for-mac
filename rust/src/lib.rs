//! kitty's Rust code, linked into the `fast_data_types` extension as a static library.
//! Every export is an `extern "C"` function declared in `kitty/kitty_rs.h`, which is the
//! C side's only view of this crate.

mod find;

use std::ffi::c_void;

use find::{
    Direction, FindBufferBounds, FindCellSpan, FindEngine, FindFill, FindMatch, FindMode,
    FindOrigin, FindPos, FindQueryChange, FindRefeed, FindRefeedRange, FindStatus, FindWant,
    StepOutcome,
};

/// `kitty/find.h`'s `FindLogicalLine`: the walker's view of one logical line.
#[repr(C)]
pub struct FindLogicalLine {
    abs_first_line: u64,
    before: *const u8,
    before_len: usize,
    rows: u32,
    text: *const u8,
    text_len: usize,
    cells: *const FindCellSpan,
}

/// A new engine holding the empty query. Never NULL: allocation failure aborts.
#[unsafe(no_mangle)]
pub extern "C" fn kitty_rs_find_engine_new() -> *mut FindEngine {
    Box::into_raw(Box::default())
}

/// # Safety
/// `engine` came from `kitty_rs_find_engine_new` and is not used again.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_engine_free(engine: *mut FindEngine) {
    // SAFETY: the caller's contract above, stated again in kitty/kitty_rs.h.
    drop(unsafe { Box::from_raw(engine) });
}

/// Unchanged when the query and mode are the ones already set, touching nothing. Rejected when
/// the query cannot be searched for: the engine holds it with no matches.
///
/// # Safety
/// `engine` is live; `utf8` points to `len` readable bytes (non-null even when `len` is 0).
///
/// # Panics
/// When `utf8` is not UTF-8: kitty always passes its query as UTF-8, so other bytes are a
/// caller bug, and the panic aborts.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_set_query(
    engine: *mut FindEngine,
    utf8: *const u8,
    len: usize,
    mode: FindMode,
) -> FindQueryChange {
    // SAFETY: the caller's contract above.
    let (engine, bytes) = unsafe { (&mut *engine, std::slice::from_raw_parts(utf8, len)) };
    // [LAW:no-silent-failure] invalid UTF-8 is a caller bug; the panic aborts loudly.
    let query = std::str::from_utf8(bytes).expect("kitty_rs_find_set_query: query is not UTF-8");
    engine.set_query(query, mode)
}

/// Searches for the query already set, in `mode`; the empty query stays empty.
///
/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_set_mode(
    engine: *mut FindEngine,
    mode: FindMode,
) -> FindQueryChange {
    // SAFETY: the caller's contract above.
    unsafe { &mut *engine }.set_mode(mode)
}

/// Writes the pattern that matches the UTF-8 text `utf8` literally to `out` and returns its
/// length: the escaping a literal query gets, for a regular-expression query seeded from text.
///
/// # Safety
/// `utf8` points to `len` readable bytes and `out` to `2 * len` writable bytes (both non-null
/// even when `len` is 0): escaping adds at most one backslash per character.
///
/// # Panics
/// When `utf8` is not UTF-8, as `kitty_rs_find_set_query` does.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_escape(utf8: *const u8, len: usize, out: *mut u8) -> usize {
    // SAFETY: the caller's contract above.
    let (bytes, out) = unsafe {
        (
            std::slice::from_raw_parts(utf8, len),
            std::slice::from_raw_parts_mut(out, 2 * len),
        )
    };
    let text = std::str::from_utf8(bytes).expect("kitty_rs_find_escape: text is not UTF-8");
    let escaped = regex::escape(text);
    out[..escaped.len()].copy_from_slice(escaped.as_bytes());
    escaped.len()
}

/// An empty refeed of `lines` under the engine's current query. Never NULL: allocation
/// failure aborts. It owns its own copy of the query, so the engine stays usable meanwhile.
///
/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_refeed_new(
    engine: *const FindEngine,
    lines: FindRefeedRange,
) -> *mut FindRefeed {
    // SAFETY: the caller's contract above.
    Box::into_raw(Box::new(unsafe { &*engine }.refeed(lines)))
}

/// # Safety
/// `refeed` is live; `line` points to a `FindLogicalLine` whose before holds `before_len`
/// bytes and whose text and cells each hold `text_len` entries (each pointer non-null even
/// when its length is 0).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_refeed_line(
    refeed: *mut FindRefeed,
    line: *const FindLogicalLine,
) {
    // SAFETY: the caller's contract above.
    let (refeed, l) = unsafe { (&mut *refeed, &*line) };
    let (before, text, cells) = unsafe {
        (
            std::slice::from_raw_parts(l.before, l.before_len),
            std::slice::from_raw_parts(l.text, l.text_len),
            std::slice::from_raw_parts(l.cells, l.text_len),
        )
    };
    refeed.feed_line(l.abs_first_line, before, l.rows, text, cells);
}

/// Replaces the matches starting in the refeed's lines, all of them searched, with its own,
/// and frees it.
///
/// # Safety
/// `engine` is live; `refeed` came from `kitty_rs_find_refeed_new` and is not used again.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_splice(engine: *mut FindEngine, refeed: *mut FindRefeed) {
    // SAFETY: the caller's contract above.
    let (engine, refeed) = unsafe { (&mut *engine, Box::from_raw(refeed)) };
    engine.splice(*refeed);
}

/// Adds the matches of the unsearched lines the refeed walked, marks them searched, and frees
/// it.
///
/// # Safety
/// `engine` is live; `refeed` came from `kitty_rs_find_refeed_new` and is not used again.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_add_searched(
    engine: *mut FindEngine,
    refeed: *mut FindRefeed,
) {
    // SAFETY: the caller's contract above.
    let (engine, refeed) = unsafe { (&mut *engine, Box::from_raw(refeed)) };
    engine.add_searched(*refeed);
}

/// Makes every line from `from` on unsearched, then adds the onward refeed's walked lines as
/// searched, and frees it.
///
/// # Safety
/// `engine` is live; `refeed` came from `kitty_rs_find_refeed_new` and is not used again.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_refresh(
    engine: *mut FindEngine,
    from: u64,
    refeed: *mut FindRefeed,
) {
    // SAFETY: the caller's contract above.
    let (engine, refeed) = unsafe { (&mut *engine, Box::from_raw(refeed)) };
    engine.refresh(from, *refeed);
}

/// Frees a refeed without splicing it: the engine keeps the matches it had.
///
/// # Safety
/// `refeed` came from `kitty_rs_find_refeed_new` and is not used again.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_refeed_free(refeed: *mut FindRefeed) {
    // SAFETY: the caller's contract above.
    drop(unsafe { Box::from_raw(refeed) });
}

/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_evict_before(engine: *mut FindEngine, oldest: u64) -> bool {
    // SAFETY: the caller's contract above.
    unsafe { &mut *engine }.evict_before(oldest)
}

/// `kitty/kitty_rs.h`'s `FindMatchVisitor`: handed `n` matches in buffer order.
pub type FindMatchVisitor = unsafe extern "C" fn(ctx: *mut c_void, run: *const FindMatch, n: usize);

/// Hands `visit` the matches covering any of the lines `first..limit`, in buffer order, a run
/// at a time, before it returns. A run is valid only during its call.
///
/// # Safety
/// `engine` is live, and `visit` is safe to call with `ctx` and does not use the engine.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_each_match_in_lines(
    engine: *const FindEngine,
    first: u64,
    limit: u64,
    visit: FindMatchVisitor,
    ctx: *mut c_void,
) {
    // SAFETY: the caller's contract above.
    for run in unsafe { &*engine }.matches_in_lines(first, limit) {
        unsafe { visit(ctx, run.as_ptr(), run.len()) };
    }
}

/// How many matches cover any of the lines `first..limit`.
///
/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_count_in_lines(
    engine: *const FindEngine,
    first: u64,
    limit: u64,
) -> usize {
    // SAFETY: the caller's contract above.
    unsafe { &*engine }.count_in_lines(first, limit)
}

/// Counts the changes to the match list.
///
/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub const unsafe extern "C" fn kitty_rs_find_generation(engine: *const FindEngine) -> u64 {
    // SAFETY: the caller's contract above.
    unsafe { &*engine }.generation()
}

/// Makes `m` the current match; false, changing nothing, when it is not one of the matches.
///
/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_set_current(engine: *mut FindEngine, m: FindMatch) -> bool {
    // SAFETY: the caller's contract above.
    unsafe { &mut *engine }.set_current(m)
}

/// `kitty/kitty_rs.h`'s `FindOrigin`.
///
/// After `after_match` when `after` is set, else within `start..end`. A bool tag crosses the
/// ABI rather than a Rust enum: a C value outside a Rust enum's variants is undefined behaviour.
#[repr(C)]
pub struct FindOriginAbi {
    after: bool,
    after_match: FindMatch,
    start: FindPos,
    end: FindPos,
}

/// What `kitty_rs_find_step` found, as `kitty/kitty_rs.h` declares it.
#[repr(C)]
#[derive(Debug, PartialEq, Eq)]
pub enum FindStepResult {
    Landed,
    NoMatch,
    Waiting,
}

/// Steps from `origin`, the next match or the previous when `backwards`. Landed writes the
/// match to `landed`; Waiting writes what the answer still needs to `want`.
///
/// # Safety
/// `engine` is live; `landed` and `want` are writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_step(
    engine: *mut FindEngine,
    backwards: bool,
    origin: FindOriginAbi,
    bounds: FindBufferBounds,
    landed: *mut FindMatch,
    want: *mut FindWant,
) -> FindStepResult {
    let direction = if backwards {
        Direction::Previous
    } else {
        Direction::Next
    };
    let origin = if origin.after {
        FindOrigin::After(origin.after_match)
    } else {
        FindOrigin::Within(origin.start..origin.end)
    };
    // SAFETY: the caller's contract above.
    match unsafe { &mut *engine }.step(direction, &origin, bounds) {
        StepOutcome::Landed(m) => {
            unsafe { *landed = m };
            FindStepResult::Landed
        }
        StepOutcome::NoMatch => FindStepResult::NoMatch,
        StepOutcome::Waiting(w) => {
            unsafe { *want = w };
            FindStepResult::Waiting
        }
    }
}

/// Where the fill searches next, toward `want` when it is not NULL; false once nothing is left
/// to search.
///
/// # Safety
/// `engine` is live; `want` is NULL or readable; `out` is writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_next_fill(
    engine: *const FindEngine,
    bounds: FindBufferBounds,
    want: *const FindWant,
    out: *mut FindFill,
) -> bool {
    // SAFETY: the caller's contract above.
    let want = unsafe { want.as_ref() }.copied();
    unsafe { &*engine }
        .next_fill(bounds, want)
        .map(|f| unsafe { *out = f })
        .is_some()
}

/// Writes the current match to `out`; false, writing nothing, when there is none.
///
/// # Safety
/// `engine` is live and `out` is writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_current(
    engine: *const FindEngine,
    out: *mut FindMatch,
) -> bool {
    // SAFETY: the caller's contract above.
    unsafe { &*engine }
        .current()
        .map(|m| unsafe { *out = m })
        .is_some()
}

/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_end_current(engine: *mut FindEngine) {
    // SAFETY: the caller's contract above.
    unsafe { &mut *engine }.end_current();
}

/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_status(
    engine: *const FindEngine,
    bounds: FindBufferBounds,
) -> FindStatus {
    // SAFETY: the caller's contract above.
    unsafe { &*engine }.status(bounds)
}

#[cfg(test)]
mod tests {
    use super::*;

    const FOUND: FindMatch = FindMatch {
        abs_start_line: 5,
        start_x: 1,
        abs_end_line: 5,
        end_x: 1,
    };
    const BOUNDS: FindBufferBounds = FindBufferBounds { oldest: 5, end: 6 };

    /// An engine that has searched its one-line buffer, "aÉ" on line 5, for "é" across the
    /// ABI, as kitty/screen.c does: set the query, ask for the fill, walk it, add it.
    fn searched_engine() -> *mut FindEngine {
        let engine = kitty_rs_find_engine_new();
        let (query, text) = ("é", "aÉ");
        let span = |x| FindCellSpan {
            row: 0,
            x,
            x_last: x,
        };
        let cells = [span(0), span(1), span(1)];
        let line = FindLogicalLine {
            abs_first_line: 5,
            before: [].as_ptr(),
            before_len: 0,
            rows: 1,
            text: text.as_ptr(),
            text_len: text.len(),
            cells: cells.as_ptr(),
        };
        let set = || unsafe {
            kitty_rs_find_set_query(engine, query.as_ptr(), query.len(), FindMode::default())
        };
        assert_eq!(set(), FindQueryChange::Changed);
        assert_eq!(set(), FindQueryChange::Unchanged);
        let mut fill = FindFill {
            from: 0,
            limit: 0,
            forward: true,
        };
        unsafe {
            assert!(kitty_rs_find_next_fill(
                engine,
                BOUNDS,
                std::ptr::null(),
                &raw mut fill
            ));
            assert_eq!(
                fill,
                FindFill {
                    from: 5,
                    limit: 6,
                    forward: false
                }
            );
            let refeed = kitty_rs_find_refeed_new(
                engine,
                FindRefeedRange {
                    from: fill.from,
                    limit: fill.limit,
                },
            );
            kitty_rs_find_refeed_line(refeed, &raw const line);
            kitty_rs_find_add_searched(engine, refeed);
            assert!(!kitty_rs_find_next_fill(
                engine,
                BOUNDS,
                std::ptr::null(),
                &raw mut fill
            ));
        }
        engine
    }

    #[test]
    fn a_search_crosses_the_abi() {
        unsafe extern "C" fn collect(ctx: *mut c_void, run: *const FindMatch, n: usize) {
            let all = unsafe { &mut *ctx.cast::<Vec<FindMatch>>() };
            all.extend_from_slice(unsafe { std::slice::from_raw_parts(run, n) });
        }
        let engine = searched_engine();
        let mut matches = Vec::<FindMatch>::new();
        unsafe {
            kitty_rs_find_each_match_in_lines(
                engine,
                0,
                u64::MAX,
                collect,
                (&raw mut matches).cast(),
            );
        };
        assert_eq!(matches, [FOUND]);
        unsafe { kitty_rs_find_engine_free(engine) };
    }

    #[test]
    fn a_step_crosses_the_abi() {
        let engine = searched_engine();
        let origin = FindOriginAbi {
            after: false,
            after_match: FOUND,
            start: FindPos { line: 5, x: 0 },
            end: FindPos { line: 6, x: 0 },
        };
        let mut landed = FindMatch {
            abs_start_line: 0,
            start_x: 0,
            abs_end_line: 0,
            end_x: 0,
        };
        let mut want = FindWant {
            line: 0,
            forward: false,
        };
        unsafe {
            assert_eq!(
                kitty_rs_find_step(
                    engine,
                    false,
                    origin,
                    BOUNDS,
                    &raw mut landed,
                    &raw mut want
                ),
                FindStepResult::Landed
            );
            assert_eq!(landed, FOUND);
            assert_eq!(kitty_rs_find_status(engine, BOUNDS).current, 1);
            kitty_rs_find_engine_free(engine);
        }
    }

    #[test]
    fn escaped_text_is_a_pattern_that_matches_it_literally() {
        let text = "a.b(é)*";
        let mut out = vec![0u8; 2 * text.len()];
        let n = unsafe { kitty_rs_find_escape(text.as_ptr(), text.len(), out.as_mut_ptr()) };
        let pattern = std::str::from_utf8(&out[..n]).unwrap();
        assert_eq!(pattern, r"a\.b\(é\)\*");
        assert!(regex::Regex::new(pattern).unwrap().is_match("xa.b(é)*"));
    }

    #[test]
    fn a_query_that_cannot_compile_is_held_rejected() {
        let engine = kitty_rs_find_engine_new();
        // past the regex crate's default compiled-size limit
        let query = "é".repeat(1 << 17);
        let set = || unsafe {
            kitty_rs_find_set_query(engine, query.as_ptr(), query.len(), FindMode::default())
        };
        assert_eq!(set(), FindQueryChange::Rejected);
        assert_eq!(
            set(),
            FindQueryChange::Unchanged,
            "the same rejected query again changes nothing"
        );
        let status = unsafe { kitty_rs_find_status(engine, BOUNDS) };
        assert_eq!(
            (status.total, status.query),
            (0, find::FindQueryState::Rejected)
        );
        unsafe { kitty_rs_find_engine_free(engine) };
    }
}
