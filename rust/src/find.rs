//! The find engine: matches a query against the logical lines kitty's cell walker
//! (kitty/find.c) feeds it, keeping every match in absolute buffer coordinates.

use regex::bytes::{Regex, RegexBuilder};
use std::sync::Arc;

/// The cells one byte of a logical line came from, as kitty/find.h defines it.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindCellSpan {
    pub row: u32,
    pub x: u32,
    pub x_last: u32,
}

/// One match: from (`abs_start_line`, `start_x`) through (`abs_end_line`, `end_x`), both cells
/// included. Field order is buffer order, so the derived Ord sorts matches as they appear.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FindMatch {
    pub abs_start_line: u64,
    pub start_x: u32,
    pub abs_end_line: u64,
    pub end_x: u32,
}

/// A cell position in the buffer, ordered as a match's start is: by line, then column.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FindPos {
    pub line: u64,
    pub x: u32,
}

impl FindMatch {
    const fn start(&self) -> FindPos {
        FindPos {
            line: self.abs_start_line,
            x: self.start_x,
        }
    }
}

/// The lines a refeed covers, as kitty/find.h defines it: `from..limit`, `from` the first line
/// of a logical line and `limit` `u64::MAX` for "onward", wherever the buffer now ends.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindRefeedRange {
    pub from: u64,
    pub limit: u64,
}

/// The matches in a range of lines, searched afresh, to be spliced into the engine whole:
/// the engine is never seen half re-fed.
pub struct FindRefeed {
    lines: FindRefeedRange,
    /// The first line the next fed line may start on: past every line fed so far.
    next_line: u64,
    /// The engine's own regex, shared: a cloned Regex would build its search caches anew.
    regex: Option<Arc<Regex>>,
    matches: Vec<FindMatch>,
}

impl FindRefeed {
    /// Adds the matches in one logical line, whose byte i of `text` came from `cells[i]`.
    pub fn feed_line(&mut self, abs_first_line: u64, text: &[u8], cells: &[FindCellSpan]) {
        assert_eq!(
            text.len(),
            cells.len(),
            "a logical line has one cell span per byte"
        );
        // [LAW:no-silent-failure] splice cuts the list at the range and evict_before and
        // matches_in_lines need it sorted: a line outside the range or out of order would
        // corrupt them quietly
        assert!(
            abs_first_line < self.lines.limit,
            "a refeed line lies in its range"
        );
        assert!(
            abs_first_line >= self.next_line,
            "lines are fed in buffer order, from the range's start"
        );
        self.next_line = abs_first_line + 1 + cells.last().map_or(0, |c| u64::from(c.row));
        // a non-empty literal never matches empty, so every match has a last byte
        let found = self.regex.iter().flat_map(|re| re.find_iter(text));
        self.matches.extend(found.map(|m| {
            let (first, last) = (cells[m.start()], cells[m.end() - 1]);
            FindMatch {
                abs_start_line: abs_first_line + u64::from(first.row),
                start_x: first.x,
                abs_end_line: abs_first_line + u64::from(last.row),
                end_x: last.x_last,
            }
        }));
    }
}

/// "Next" is later in the buffer, "Previous" earlier.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Next,
    Previous,
}

/// The match the user is on, and whether the step that reached it wrapped past an end.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Current {
    m: FindMatch,
    wrapped: bool,
}

/// Where the user stands in the matches: `current` is 1-based, 0 when there is none.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindStatus {
    pub current: usize,
    pub total: usize,
    pub wrapped: bool,
}

#[derive(Default)]
pub struct FindEngine {
    /// None is the empty query: there is nothing to find, so every line yields no match.
    regex: Option<Arc<Regex>>,
    /// Sorted in buffer order: a refeed is fed in order and spliced in at its range, which
    /// starts a logical line, so no match crosses the cut. The first `evicted` entries have
    /// left the history ring and are no longer matches.
    matches: Vec<FindMatch>,
    /// Evicted entries still at the front of `matches`, removed in one drain once they are
    /// as many as the live ones, so eviction costs amortized O(evicted), not O(live).
    evicted: usize,
    /// Counts the changes to `matches()`, so a renderer can tell that what it drew is stale.
    generation: u64,
    /// The match the user is on: always one of `matches()`.
    current: Option<Current>,
}

impl FindEngine {
    /// Starts a new search, dropping every match of the previous one. The query is a
    /// literal: `regex::escape` puts it through the regex crate's literal fast path.
    pub fn set_query(&mut self, query: &str, case_insensitive: bool) -> Result<(), regex::Error> {
        let cleared = self.matches().len();
        self.matches.clear();
        self.evicted = 0;
        self.changed(cleared);
        // a query that fails to build leaves the empty query, never the previous one
        self.regex = None;
        self.regex = (!query.is_empty())
            .then(|| {
                RegexBuilder::new(&regex::escape(query))
                    .case_insensitive(case_insensitive)
                    .build()
                    .map(Arc::new)
            })
            .transpose()?;
        Ok(())
    }

    /// An empty refeed of `lines` under the current query, for the caller to feed and splice.
    pub fn refeed(&self, lines: FindRefeedRange) -> FindRefeed {
        FindRefeed {
            lines,
            next_line: lines.from,
            regex: self.regex.clone(),
            matches: Vec::new(),
        }
    }

    /// Replaces the matches starting in the refeed's lines with its matches. With an onward
    /// range that is every match from its first line on, so matches past where the buffer now
    /// ends go too. Unchanged lines re-fed are no change: the generation stays.
    pub fn splice(&mut self, refeed: FindRefeed) {
        let FindRefeedRange { from, limit } = refeed.lines;
        let all = self.matches();
        let (cut, to) = (
            all.partition_point(|m| m.abs_start_line < from),
            all.partition_point(|m| m.abs_start_line < limit),
        );
        let differs = all[cut..to] != refeed.matches[..];
        if differs {
            self.matches
                .splice(self.evicted + cut..self.evicted + to, refeed.matches);
        }
        self.changed(usize::from(differs));
    }

    /// Drops the matches starting before `abs_line`: their lines have left the history ring.
    pub fn evict_before(&mut self, abs_line: u64) {
        let gone = self
            .matches()
            .partition_point(|m| m.abs_start_line < abs_line);
        self.evicted += gone;
        if self.evicted >= self.matches.len() - self.evicted {
            self.matches.drain(..self.evicted);
            self.evicted = 0;
        }
        self.changed(gone);
    }

    // [LAW:single-enforcer] every edit of the list ends here: the generation moves exactly
    // when the list did, and a current match the edit removed is no longer current.
    fn changed(&mut self, entries: usize) {
        self.generation += u64::from(entries > 0);
        self.current = self
            .current
            .filter(|c| self.matches().binary_search(&c.m).is_ok());
    }

    pub fn matches(&self) -> &[FindMatch] {
        &self.matches[self.evicted..]
    }

    /// The matches covering any of the lines `first..limit`.
    pub fn matches_in_lines(&self, first: u64, limit: u64) -> &[FindMatch] {
        let all = self.matches();
        // matches never overlap, so their ends are in buffer order as well as their starts
        let from = all.partition_point(|m| m.abs_end_line < first);
        let to = all.partition_point(|m| m.abs_start_line < limit).max(from);
        &all[from..to]
    }

    pub const fn generation(&self) -> u64 {
        self.generation
    }

    /// Makes `m` the current match; false, changing nothing, when it is not one of `matches()`.
    /// A match names itself by its absolute coordinates, which eviction does not move. Setting
    /// the match that is already current keeps whether the step to it wrapped.
    pub fn set_current(&mut self, m: FindMatch) -> bool {
        let found = self.matches().binary_search(&m).is_ok();
        let wrapped = self.current.is_some_and(|c| c.m == m && c.wrapped);
        self.current = found.then_some(Current { m, wrapped }).or(self.current);
        found
    }

    pub fn current(&self) -> Option<FindMatch> {
        self.current.map(|c| c.m)
    }

    pub const fn end_current(&mut self) {
        self.current = None;
    }

    /// Makes the match one step from the current one the current match and returns it, None
    /// when there are no matches. With no current match the step starts from `from`, where
    /// the user is looking: Next takes the first match starting in it or after, Previous the
    /// last starting before its end. Past either end it wraps, and `status()` says so.
    pub fn step(
        &mut self,
        direction: Direction,
        from: std::ops::Range<FindPos>,
    ) -> Option<FindMatch> {
        self.current = self.step_target(direction, from);
        self.current()
    }

    fn step_target(&self, direction: Direction, from: std::ops::Range<FindPos>) -> Option<Current> {
        let all = self.matches();
        // the count of matches before the step's start, whichever way it goes
        let before = match (self.current, direction) {
            (Some(c), Direction::Next) => all.partition_point(|m| *m <= c.m),
            (Some(c), Direction::Previous) => all.partition_point(|m| *m < c.m),
            (None, Direction::Next) => all.partition_point(|m| m.start() < from.start),
            (None, Direction::Previous) => all.partition_point(|m| m.start() < from.end),
        };
        let (target, wrapped) = match direction {
            Direction::Next => all
                .get(before)
                .map_or_else(|| (all.first(), true), |m| (Some(m), false)),
            Direction::Previous => before
                .checked_sub(1)
                .map_or_else(|| (all.last(), true), |i| (all.get(i), false)),
        };
        target.map(|&m| Current { m, wrapped })
    }

    pub fn status(&self) -> FindStatus {
        let all = self.matches();
        let current = self.current.map_or(0, |c| {
            all.binary_search(&c.m)
                .expect("the current match is one of the matches")
                + 1
        });
        FindStatus {
            current,
            total: all.len(),
            wrapped: self.current.is_some_and(|c| c.wrapped),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A logical line built the way the walker builds one: each char of each row covers
    /// `width(char)` cells, and every byte of the char maps to those cells.
    fn line(rows: &[&str], width: impl Fn(char) -> u32) -> (Vec<u8>, Vec<FindCellSpan>) {
        let (mut text, mut cells) = (Vec::new(), Vec::new());
        for (row, s) in (0u32..).zip(rows) {
            let mut x = 0;
            for c in s.chars() {
                let span = FindCellSpan {
                    row,
                    x,
                    x_last: x + width(c) - 1,
                };
                let mut buf = [0; 4];
                text.extend_from_slice(c.encode_utf8(&mut buf).as_bytes());
                cells.extend(std::iter::repeat_n(span, c.len_utf8()));
                x += width(c);
            }
        }
        (text, cells)
    }

    fn narrow(_: char) -> u32 {
        1
    }

    fn onward(from: u64) -> FindRefeedRange {
        FindRefeedRange {
            from,
            limit: u64::MAX,
        }
    }

    /// One logical line fed onward from its first line: past the last match, an append.
    fn append(e: &mut FindEngine, abs: u64, text: &[u8], cells: &[FindCellSpan]) {
        let mut r = e.refeed(onward(abs));
        r.feed_line(abs, text, cells);
        e.splice(r);
    }

    /// An empty onward refeed: the buffer now ends before `abs`.
    fn drop_from(e: &mut FindEngine, abs: u64) {
        let r = e.refeed(onward(abs));
        e.splice(r);
    }

    /// A refeed of `lines` with one "x" line at each of `abs_lines`.
    fn refeed_of_x(e: &FindEngine, lines: FindRefeedRange, abs_lines: &[u64]) -> FindRefeed {
        let mut r = e.refeed(lines);
        for &abs in abs_lines {
            let (text, cells) = line(&["x"], narrow);
            r.feed_line(abs, &text, &cells);
        }
        r
    }

    fn search(query: &str, case_insensitive: bool, lines: &[(u64, &[&str])]) -> Vec<FindMatch> {
        let mut e = FindEngine::default();
        e.set_query(query, case_insensitive).unwrap();
        for (abs, rows) in lines {
            let (text, cells) = line(rows, narrow);
            append(&mut e, *abs, &text, &cells);
        }
        e.matches().to_vec()
    }

    fn m(abs_start_line: u64, start_x: u32, abs_end_line: u64, end_x: u32) -> FindMatch {
        FindMatch {
            abs_start_line,
            start_x,
            abs_end_line,
            end_x,
        }
    }

    #[test]
    fn every_match_in_one_line() {
        assert_eq!(
            search("ab", false, &[(7, &["xabyab"])]),
            [m(7, 1, 7, 2), m(7, 4, 7, 5)]
        );
    }

    #[test]
    fn a_match_across_wrapped_rows_ends_on_the_later_row() {
        assert_eq!(
            search("cdef", false, &[(3, &["abcd", "efgh"])]),
            [m(3, 2, 4, 1)]
        );
    }

    #[test]
    fn matches_from_later_lines_follow_in_buffer_order() {
        assert_eq!(
            search("x", false, &[(0, &["x"]), (1, &["ax", "x"]), (3, &["x"])]),
            [m(0, 0, 0, 0), m(1, 1, 1, 1), m(2, 0, 2, 0), m(3, 0, 3, 0)]
        );
    }

    #[test]
    fn wide_and_multibyte_chars_map_to_their_cells() {
        let mut e = FindEngine::default();
        e.set_query("é中z", false).unwrap();
        let (text, cells) = line(&["a中é中z"], |c| if c == '中' { 2 } else { 1 });
        append(&mut e, 0, &text, &cells);
        // a=0, 中=1..2, é=3, 中=4..5, z=6: the match ends on z, and starts on é after a wide char
        assert_eq!(e.matches(), [m(0, 3, 0, 6)]);
        e.set_query("中", false).unwrap();
        append(&mut e, 0, &text, &cells);
        // a match that is itself a wide char ends on that char's last cell
        assert_eq!(e.matches(), [m(0, 1, 0, 2), m(0, 4, 0, 5)]);
    }

    #[test]
    fn a_tab_covers_its_padding() {
        let mut e = FindEngine::default();
        e.set_query("\tb", false).unwrap();
        let (text, cells) = line(&["a\tb"], |c| if c == '\t' { 7 } else { 1 });
        append(&mut e, 0, &text, &cells);
        assert_eq!(e.matches(), [m(0, 1, 0, 8)]);
    }

    #[test]
    fn case_insensitive_folds_letters_one_to_one() {
        assert_eq!(search("é", true, &[(0, &["É"])]), [m(0, 0, 0, 0)]);
        assert_eq!(search("abc", true, &[(0, &["xABc"])]), [m(0, 1, 0, 3)]);
        // simple case folding: ß does not expand to SS
        assert_eq!(search("straße", true, &[(0, &["STRASSE"])]), []);
    }

    #[test]
    fn case_sensitive_by_request() {
        assert_eq!(search("abc", false, &[(0, &["ABC abc"])]), [m(0, 4, 0, 6)]);
    }

    #[test]
    fn regex_metacharacters_are_literal() {
        assert_eq!(search("a.c", false, &[(0, &["abc a.c"])]), [m(0, 4, 0, 6)]);
        assert_eq!(search("(", false, &[(0, &["f(x)"])]), [m(0, 1, 0, 1)]);
    }

    #[test]
    fn the_empty_query_finds_nothing() {
        assert_eq!(search("", false, &[(0, &["abc"])]), []);
    }

    #[test]
    fn a_new_query_drops_the_previous_matches() {
        let mut e = FindEngine::default();
        e.set_query("a", false).unwrap();
        let (text, cells) = line(&["a"], narrow);
        append(&mut e, 0, &text, &cells);
        e.set_query("b", false).unwrap();
        assert_eq!(e.matches(), []);
    }

    #[test]
    fn drop_and_evict_cut_the_list_at_a_line() {
        let all = search("x", false, &[(0, &["x"]), (1, &["x"]), (2, &["x"])]);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        for abs in 0..3 {
            let (text, cells) = line(&["x"], narrow);
            append(&mut e, abs, &text, &cells);
        }
        drop_from(&mut e, 2);
        assert_eq!(e.matches(), &all[..2]);
        e.evict_before(1);
        assert_eq!(e.matches(), &all[1..2]);
    }

    fn starts(e: &FindEngine) -> Vec<u64> {
        e.matches().iter().map(|m| m.abs_start_line).collect()
    }

    fn engine_with_one_match_per_line(lines: u64) -> FindEngine {
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        for abs in 0..lines {
            let (text, cells) = line(&["x"], narrow);
            append(&mut e, abs, &text, &cells);
        }
        e
    }

    #[test]
    fn eviction_in_steps_keeps_exactly_the_later_matches() {
        let mut e = engine_with_one_match_per_line(10);
        // evicting one line at a time crosses the point where the evicted entries are drained
        for oldest in 1..=10 {
            e.evict_before(oldest);
            assert_eq!(starts(&e), (oldest..10).collect::<Vec<_>>());
        }
    }

    #[test]
    fn drop_after_evict_cuts_at_the_line() {
        let mut e = engine_with_one_match_per_line(6);
        e.evict_before(2);
        drop_from(&mut e, 4);
        let (text, cells) = line(&["x"], narrow);
        append(&mut e, 4, &text, &cells);
        assert_eq!(starts(&e), [2, 3, 4]);
    }

    #[test]
    #[should_panic(expected = "lines are fed in buffer order, from the range")]
    fn an_out_of_order_feed_panics() {
        refeed_of_x(&FindEngine::default(), onward(0), &[3, 2]);
    }

    #[test]
    #[should_panic(expected = "a refeed line lies in its range")]
    fn a_line_past_the_refeed_range_panics() {
        refeed_of_x(
            &FindEngine::default(),
            FindRefeedRange { from: 2, limit: 3 },
            &[3],
        );
    }

    #[test]
    #[should_panic(expected = "lines are fed in buffer order, from the range's start")]
    fn a_line_before_the_refeed_range_panics() {
        refeed_of_x(
            &FindEngine::default(),
            FindRefeedRange { from: 2, limit: 3 },
            &[1],
        );
    }

    #[test]
    fn an_identical_refeed_is_no_change() {
        let mut e = engine_with_one_match_per_line(6);
        let current = e.matches()[4];
        e.step(Direction::Previous, EVERYWHERE);
        e.step(Direction::Previous, EVERYWHERE);
        assert_eq!(e.current(), Some(current));
        let g = e.generation();
        let r = refeed_of_x(&e, onward(3), &[3, 4, 5]);
        e.splice(r);
        let r = refeed_of_x(&e, FindRefeedRange { from: 4, limit: 5 }, &[4]);
        e.splice(r);
        assert_eq!(e.generation(), g);
        assert_eq!(
            e.current(),
            Some(current),
            "the re-fed current match is still current"
        );
        assert_eq!(starts(&e), [0, 1, 2, 3, 4, 5]);
    }

    #[test]
    fn a_refeed_that_changes_its_lines_is_a_change() {
        let mut e = engine_with_one_match_per_line(6);
        e.set_current(e.matches()[4]);
        let g = e.generation();
        // line 4 no longer holds an x
        let r = refeed_of_x(&e, FindRefeedRange { from: 4, limit: 5 }, &[]);
        e.splice(r);
        assert_ne!(e.generation(), g);
        assert_eq!(e.current(), None, "a current match the refeed removed ends");
        assert_eq!(
            starts(&e),
            [0, 1, 2, 3, 5],
            "a one-line refeed leaves the lines after it"
        );
    }

    #[test]
    fn an_onward_refeed_replaces_every_match_from_its_line() {
        let mut e = engine_with_one_match_per_line(6);
        e.evict_before(1);
        // the buffer now ends after line 3: the matches on 4 and 5 go though nothing was fed there
        let r = refeed_of_x(&e, onward(2), &[2, 3]);
        e.splice(r);
        assert_eq!(starts(&e), [1, 2, 3]);
        let r = refeed_of_x(&e, onward(3), &[3, 7]);
        e.splice(r);
        assert_eq!(starts(&e), [1, 2, 3, 7]);
    }

    #[test]
    fn setting_the_current_match_again_keeps_its_wrap() {
        let mut e = engine_with_one_match_per_line(3);
        e.step(Direction::Previous, EVERYWHERE);
        e.step(Direction::Next, EVERYWHERE);
        assert!(e.status().wrapped);
        assert!(e.set_current(e.matches()[0]));
        assert!(
            e.status().wrapped,
            "the same match: still reached by wrapping"
        );
        assert!(e.set_current(e.matches()[1]));
        assert!(!e.status().wrapped, "another match: not a wrap");
    }

    #[test]
    fn the_generation_moves_exactly_when_the_list_does() {
        let mut e = engine_with_one_match_per_line(4);
        let g = e.generation();
        let (text, cells) = line(&["y"], narrow);
        append(&mut e, 4, &text, &cells);
        e.evict_before(0);
        drop_from(&mut e, 5);
        assert_eq!(
            e.generation(),
            g,
            "a feed that finds nothing and no-op cuts are not changes"
        );
        let mut empty = FindEngine::default();
        let g0 = empty.generation();
        empty.set_query("x", false).unwrap();
        empty.set_query("", false).unwrap();
        assert_eq!(
            empty.generation(),
            g0,
            "a query that leaves the list empty is not a change"
        );
        let changes: [fn(&mut FindEngine); 3] = [
            |e| e.evict_before(1),
            |e| drop_from(e, 3),
            |e| e.set_query("x", false).unwrap(),
        ];
        for change in changes {
            let g = e.generation();
            change(&mut e);
            assert_ne!(e.generation(), g);
        }
    }

    #[test]
    fn the_current_match_is_always_in_the_list() {
        let mut e = engine_with_one_match_per_line(6);
        assert!(!e.set_current(m(6, 0, 6, 0)));
        assert_eq!(e.current(), None);
        let current = e.matches()[2];
        assert!(e.set_current(current));
        assert_eq!(e.current(), Some(current));
        e.evict_before(2);
        drop_from(&mut e, 3);
        assert_eq!(
            e.current(),
            Some(current),
            "cuts around the current match keep it"
        );
        drop_from(&mut e, 2);
        assert_eq!(e.current(), None, "dropping its line ends it");
        let mut e = engine_with_one_match_per_line(6);
        e.set_current(e.matches()[1]);
        e.evict_before(2);
        assert_eq!(e.current(), None, "evicting its line ends it");
        assert!(
            !e.set_current(m(1, 0, 1, 0)),
            "an evicted match cannot become current"
        );
        e.set_current(e.matches()[0]);
        e.set_query("x", false).unwrap();
        assert_eq!(e.current(), None, "a new query ends it");
        let mut e = engine_with_one_match_per_line(2);
        e.set_current(e.matches()[1]);
        e.end_current();
        assert_eq!(e.current(), None);
        assert_eq!(
            e.matches().len(),
            2,
            "ending the current match leaves the matches"
        );
    }

    fn pos(line: u64, x: u32) -> FindPos {
        FindPos { line, x }
    }

    /// Lines 0 and 1: "ab ab" wraps onto "ab", so its second match spans both rows; line 2
    /// holds a wide char before the match.
    fn engine_for_steps() -> (FindEngine, Vec<FindMatch>) {
        let mut e = FindEngine::default();
        e.set_query("ab", false).unwrap();
        let (text, cells) = line(&["ab a", "b ab"], narrow);
        append(&mut e, 0, &text, &cells);
        let (text, cells) = line(&["中ab"], |c| if c == '中' { 2 } else { 1 });
        append(&mut e, 2, &text, &cells);
        let all = e.matches().to_vec();
        assert_eq!(
            all,
            [m(0, 0, 0, 1), m(0, 3, 1, 0), m(1, 2, 1, 3), m(2, 2, 2, 3)]
        );
        (e, all)
    }

    const EVERYWHERE: std::ops::Range<FindPos> = FindPos { line: 0, x: 0 }..FindPos {
        line: u64::MAX,
        x: 0,
    };

    #[test]
    fn steps_follow_buffer_order_and_wrap_at_both_ends() {
        let (mut e, all) = engine_for_steps();
        let walk = |e: &mut FindEngine, d| {
            (0..=all.len())
                .map(|_| (e.step(d, EVERYWHERE).unwrap(), e.status().wrapped))
                .collect::<Vec<_>>()
        };
        let forward = walk(&mut e, Direction::Next);
        assert_eq!(
            forward,
            [
                (all[0], false),
                (all[1], false),
                (all[2], false),
                (all[3], false),
                (all[0], true)
            ]
        );
        let back = walk(&mut e, Direction::Previous);
        assert_eq!(
            back,
            [
                (all[3], true),
                (all[2], false),
                (all[1], false),
                (all[0], false),
                (all[3], true)
            ]
        );
    }

    #[test]
    fn with_no_current_match_a_step_starts_from_where_the_user_looks() {
        let (mut e, all) = engine_for_steps();
        // Next takes the first match starting in the range, Previous the last starting before its end
        assert_eq!(e.step(Direction::Next, pos(0, 3)..pos(1, 3)), Some(all[1]));
        e.set_query("ab", false).unwrap();
        let (text, cells) = line(&["ab a", "b ab"], narrow);
        append(&mut e, 0, &text, &cells);
        assert_eq!(
            e.step(Direction::Previous, pos(0, 3)..pos(1, 3)),
            Some(all[2])
        );
        assert!(!e.status().wrapped);
        // nothing at or after the start wraps to the first; nothing before the end wraps to the last
        let mut e = engine_for_steps().0;
        assert_eq!(e.step(Direction::Next, pos(3, 0)..pos(5, 0)), Some(all[0]));
        assert!(e.status().wrapped);
        let mut e = engine_for_steps().0;
        assert_eq!(
            e.step(Direction::Previous, pos(0, 0)..pos(0, 0)),
            Some(all[3])
        );
        assert!(e.status().wrapped);
        // once there is a current match, the range plays no part
        assert_eq!(
            e.step(Direction::Previous, pos(0, 0)..pos(0, 0)),
            Some(all[2])
        );
    }

    #[test]
    fn with_no_matches_a_step_finds_nothing() {
        let mut e = FindEngine::default();
        assert_eq!(e.step(Direction::Next, EVERYWHERE), None);
        e.set_query("zz", false).unwrap();
        let (text, cells) = line(&["ab"], narrow);
        append(&mut e, 0, &text, &cells);
        assert_eq!(e.step(Direction::Previous, EVERYWHERE), None);
        assert_eq!(
            e.status(),
            FindStatus {
                current: 0,
                total: 0,
                wrapped: false
            }
        );
    }

    #[test]
    fn the_status_counts_the_current_match_among_the_live_ones() {
        let (mut e, all) = engine_for_steps();
        assert_eq!(
            e.status(),
            FindStatus {
                current: 0,
                total: 4,
                wrapped: false
            }
        );
        e.step(Direction::Previous, EVERYWHERE);
        assert_eq!(
            e.status(),
            FindStatus {
                current: 4,
                total: 4,
                wrapped: false
            }
        );
        // setting the current match directly is not a wrap
        e.set_current(all[2]);
        assert_eq!(
            e.status(),
            FindStatus {
                current: 3,
                total: 4,
                wrapped: false
            }
        );
        // eviction counts from the oldest live match
        e.evict_before(1);
        assert_eq!(
            e.status(),
            FindStatus {
                current: 1,
                total: 2,
                wrapped: false
            }
        );
        // a step that wraps, then one that does not, clears the flag
        e.step(Direction::Previous, EVERYWHERE);
        assert!(e.status().wrapped);
        e.step(Direction::Previous, EVERYWHERE);
        assert_eq!(
            e.status(),
            FindStatus {
                current: 1,
                total: 2,
                wrapped: false
            }
        );
        // and so does losing the current match
        e.step(Direction::Previous, EVERYWHERE);
        drop_from(&mut e, 0);
        assert_eq!(
            e.status(),
            FindStatus {
                current: 0,
                total: 0,
                wrapped: false
            }
        );
    }

    #[test]
    fn the_matches_in_lines_are_those_touching_them() {
        let mut e = FindEngine::default();
        e.set_query("xx", false).unwrap();
        // lines 0, 1-2 (one match wrapping), 4
        for (abs, rows) in [(0, &["xx"][..]), (1, &["   x", "x  "]), (4, &["xx"])] {
            let (text, cells) = line(rows, narrow);
            append(&mut e, abs, &text, &cells);
        }
        let all = e.matches().to_vec();
        assert_eq!(all, [m(0, 0, 0, 1), m(1, 3, 2, 0), m(4, 0, 4, 1)]);
        assert_eq!(
            e.matches_in_lines(2, 4),
            &all[1..2],
            "a match ending in the window is in it"
        );
        assert_eq!(
            e.matches_in_lines(0, 2),
            &all[..2],
            "a match starting in the window is in it"
        );
        assert_eq!(e.matches_in_lines(3, 4), []);
        assert_eq!(e.matches_in_lines(0, 9), &all[..]);
        assert_eq!(
            e.matches_in_lines(4, 4),
            [],
            "an empty window holds nothing"
        );
    }
}
