//! The find engine: matches a query against the logical lines kitty's cell walker
//! (kitty/find.c) feeds it, keeping every match in absolute buffer coordinates.

mod match_list;

use match_list::MatchList;
use regex::bytes::{Regex, RegexBuilder};
use std::ops::Range;
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

/// The buffer as it is now, from its oldest line to just past its last. Every call that needs
/// it is handed it: the engine keeps no copy, since the buffer is its one source.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindBufferBounds {
    pub oldest: u64,
    pub end: u64,
}

impl FindBufferBounds {
    const fn lines(self) -> Range<u64> {
        self.oldest..self.end
    }
}

/// The matches in a range of lines, searched afresh, to be spliced into the engine whole:
/// the engine is never seen half re-fed.
pub struct FindRefeed {
    lines: FindRefeedRange,
    /// The first line the next fed line may start on: past every line fed so far, so the
    /// lines walked are `lines.from..next_line`.
    next_line: u64,
    /// The engine's own regex, shared: a cloned Regex would build its search caches anew.
    regex: Option<Arc<Regex>>,
    matches: Vec<FindMatch>,
}

impl FindRefeed {
    /// Adds the matches in one logical line of `rows` physical lines, whose byte i of `text`
    /// came from `cells[i]`. Rows past its last text are still its rows: an erased row keeps the
    /// wrap flag of the row above.
    pub fn feed_line(
        &mut self,
        abs_first_line: u64,
        rows: u32,
        text: &[u8],
        cells: &[FindCellSpan],
    ) {
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
        assert!(
            rows > 0 && cells.last().is_none_or(|c| c.row < rows),
            "a logical line's cells lie in its rows"
        );
        self.next_line = abs_first_line + u64::from(rows);
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

    /// The lines fed: a walk the sink stopped early fed fewer than its range.
    const fn walked(&self) -> Range<u64> {
        self.lines.from..self.next_line
    }
}

/// The lines searched under the current query: sorted, disjoint, merged ranges, each starting
/// and ending on a logical-line boundary. The whole buffer searched is the search done; there
/// is no other record of it.
#[derive(Default, Debug, Clone, PartialEq, Eq)]
struct Searched(Vec<Range<u64>>);

impl Searched {
    fn contains(&self, line: u64) -> bool {
        self.0.iter().any(|r| r.contains(&line))
    }

    fn covers(&self, lines: &Range<u64>) -> bool {
        lines.is_empty()
            || self
                .0
                .iter()
                .any(|r| r.start <= lines.start && lines.end <= r.end)
    }

    fn add(&mut self, lines: Range<u64>) {
        // [LAW:single-enforcer] the one check that a fill searched only unsearched lines, so
        // the lines it adds held no match yet
        assert!(
            !self
                .0
                .iter()
                .any(|r| r.start < lines.end && lines.start < r.end),
            "a fill searches only unsearched lines"
        );
        if lines.is_empty() {
            return;
        }
        self.0.push(lines);
        self.0.sort_unstable_by_key(|r| r.start);
        self.0 = self.0.drain(..).fold(Vec::new(), |mut merged, r| {
            match merged.last_mut() {
                Some(last) if last.end == r.start => last.end = r.end,
                _ => merged.push(r),
            }
            merged
        });
    }

    fn forget_from(&mut self, line: u64) {
        self.0.retain_mut(|r| {
            r.end = r.end.min(line);
            r.start < r.end
        });
    }

    fn evict_before(&mut self, oldest: u64) {
        self.0.retain_mut(|r| {
            r.start = r.start.max(oldest);
            r.start < r.end
        });
    }

    /// The buffer's lines not yet searched.
    fn unsearched(&self, b: FindBufferBounds) -> u64 {
        let searched: u64 = self
            .0
            .iter()
            .map(|r| r.end.min(b.end).saturating_sub(r.start.max(b.oldest)))
            .sum();
        (b.end - b.oldest) - searched
    }

    /// The first unsearched line of `lines`, walking forward.
    fn first_gap(&self, lines: Range<u64>) -> Option<u64> {
        let mut l = lines.start;
        while l < lines.end {
            match self.0.iter().find(|r| r.contains(&l)) {
                Some(r) => l = r.end,
                None => return Some(l),
            }
        }
        None
    }

    /// The last unsearched line of `lines`, walking backward.
    fn last_gap(&self, lines: Range<u64>) -> Option<u64> {
        let mut l = lines.end;
        while l > lines.start {
            match self.0.iter().find(|r| r.contains(&(l - 1))) {
                Some(r) => l = r.start,
                None => return Some(l - 1),
            }
        }
        None
    }

    /// The unsearched stretch of the buffer holding the unsearched `line`.
    fn gap_around(&self, line: u64, b: FindBufferBounds) -> Range<u64> {
        let start = self
            .0
            .iter()
            .rev()
            .find(|r| r.end <= line)
            .map_or(b.oldest, |r| r.end);
        let end = self
            .0
            .iter()
            .find(|r| r.start > line)
            .map_or(b.end, |r| r.start);
        start.max(b.oldest)..end.min(b.end)
    }
}

/// "Next" is later in the buffer, "Previous" earlier.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    Next,
    Previous,
}

/// Where a step starts: after a match, as from the current match, or within a range of cells,
/// as from the user's selection or the rows on view.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum FindOrigin {
    After(FindMatch),
    Within(std::ops::Range<FindPos>),
}

/// An unsearched line a waiting step's answer needs, and which way the fill searches from it:
/// forward from it, or backward through it.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindWant {
    pub line: u64,
    pub forward: bool,
}

/// The unsearched lines a fill walks next, `from..limit`: from `from` on when `forward`, else
/// back from `limit`.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindFill {
    pub from: u64,
    pub limit: u64,
    pub forward: bool,
}

/// What a step found: a match, no match anywhere in the buffer, or that unsearched lines could
/// still hold its answer, and the first of them it needs.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum StepOutcome {
    Landed(FindMatch),
    NoMatch,
    Waiting(FindWant),
}

/// Whether a `set_query` started a new search.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FindQueryChange {
    Unchanged,
    Changed,
}

/// The match the user is on, and whether the step that reached it wrapped past an end.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
struct Current {
    m: FindMatch,
    wrapped: bool,
}

/// Where the user stands in the matches: `current` is 1-based, 0 when there is none, and
/// `unsearched` counts the buffer's lines the search has yet to reach, 0 once it has them all.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindStatus {
    pub current: usize,
    pub total: usize,
    pub wrapped: bool,
    pub unsearched: u64,
}

#[derive(Default)]
pub struct FindEngine {
    /// The query and whether it ignores case: a `set_query` with both the same changes nothing.
    query: (String, bool),
    /// None is the empty query: there is nothing to find, so every line yields no match.
    regex: Option<Arc<Regex>>,
    /// Sorted in buffer order, and every one in a searched range: a refeed is fed in order and
    /// spliced in at its range, which starts a logical line, so no match crosses the cut.
    matches: MatchList,
    searched: Searched,
    /// Counts the changes to `matches`, so a renderer can tell that what it drew is stale.
    generation: u64,
    /// The match the user is on: always one of `matches`.
    current: Option<Current>,
}

impl FindEngine {
    /// Starts a new search, dropping every match of the previous one, unless the query and
    /// case are the ones already searched for: then nothing changes. The query is a literal:
    /// `regex::escape` puts it through the regex crate's literal fast path.
    pub fn set_query(
        &mut self,
        query: &str,
        case_insensitive: bool,
    ) -> Result<FindQueryChange, regex::Error> {
        // the empty query finds nothing whichever case it ignores
        let key = (query.to_owned(), case_insensitive && !query.is_empty());
        if key == self.query {
            return Ok(FindQueryChange::Unchanged);
        }
        let cleared = self.matches.len();
        self.matches.clear();
        self.searched = Searched::default();
        self.changed(cleared);
        // a query that fails to build leaves the empty query, never the previous one
        self.regex = None;
        self.query = (String::new(), false);
        self.regex = (!query.is_empty())
            .then(|| {
                RegexBuilder::new(&regex::escape(query))
                    .case_insensitive(case_insensitive)
                    .build()
                    .map(Arc::new)
            })
            .transpose()?;
        self.query = key;
        Ok(FindQueryChange::Changed)
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

    /// Replaces the matches starting in the refeed's lines, all of them searched, with its own.
    /// Unchanged lines re-fed are no change: the generation stays.
    pub fn splice(&mut self, refeed: FindRefeed) {
        let FindRefeedRange { from, limit } = refeed.lines;
        assert!(
            self.searched.covers(&(from..limit)),
            "a refeed re-searches searched lines"
        );
        self.replace(from..limit, refeed.matches);
    }

    /// Adds the matches of the unsearched lines a fill walked, and marks them searched.
    pub fn add_searched(&mut self, refeed: FindRefeed) {
        let walked = refeed.walked();
        self.searched.add(walked.clone());
        self.replace(walked, refeed.matches);
    }

    /// A change at `from` makes every line from it on unsearched again, and the onward refeed,
    /// walked from its start toward the buffer's end, searches the lines it reached afresh. The
    /// matches it drops and finds are compared as one, so lines that still hold what they held
    /// move no generation.
    pub fn refresh(&mut self, from: u64, refeed: FindRefeed) {
        assert!(
            refeed.lines.limit == u64::MAX && refeed.lines.from >= from,
            "a refresh's refeed is onward, from the changed line or past it"
        );
        self.searched.forget_from(from);
        self.searched.add(refeed.walked());
        self.replace(from..u64::MAX, refeed.matches);
    }

    // [LAW:single-enforcer] every splice of the list ends here: the matches starting in lines
    // go, the found ones arrive, and the generation moves only when they differ.
    fn replace(&mut self, lines: Range<u64>, found: Vec<FindMatch>) {
        let cut = self
            .matches
            .partition_point(|m| m.abs_start_line < lines.start)
            ..self
                .matches
                .partition_point(|m| m.abs_start_line < lines.end);
        let differs = !self.matches.runs(cut.clone()).flatten().eq(&found);
        if differs {
            self.matches.replace(cut, found);
        }
        self.changed(usize::from(differs));
    }

    /// Drops the matches and searched lines before `oldest`: they have left the history ring.
    /// True when the buffer's first line is searched, so a logical line the eviction cut holds
    /// matches the caller must re-feed.
    pub fn evict_before(&mut self, oldest: u64) -> bool {
        self.replace(0..oldest, Vec::new());
        self.searched.evict_before(oldest);
        self.searched.contains(oldest)
    }

    // [LAW:single-enforcer] every edit of the list ends here: the generation moves exactly
    // when the list did, and a current match the edit removed is no longer current.
    fn changed(&mut self, entries: usize) {
        self.generation += u64::from(entries > 0);
        self.current = self
            .current
            .filter(|c| self.matches.position(&c.m).is_some());
    }

    /// Where the fill searches next: toward a waiting step's want while that line is unsearched,
    /// else the unsearched lines nearest the buffer's end, backward. None once all are searched,
    /// and for the empty query, which has nothing to search.
    pub fn next_fill(&self, b: FindBufferBounds, want: Option<FindWant>) -> Option<FindFill> {
        let searched = self.searchable()?;
        let wanted = want.filter(|w| b.lines().contains(&w.line) && !searched.contains(w.line));
        let at = wanted.or_else(|| {
            searched.last_gap(b.lines()).map(|line| FindWant {
                line,
                forward: false,
            })
        })?;
        let gap = searched.gap_around(at.line, b);
        Some(if at.forward {
            FindFill {
                from: at.line,
                limit: gap.end,
                forward: true,
            }
        } else {
            FindFill {
                from: gap.start,
                limit: at.line + 1,
                forward: false,
            }
        })
    }

    /// The searched lines under a query that can match; None for the empty query.
    // [LAW:single-enforcer] the empty query has nothing to search, so nothing waits on it
    fn searchable(&self) -> Option<&Searched> {
        self.regex.as_ref().map(|_| &self.searched)
    }

    /// The matches covering any of the lines `first..limit`, in buffer order, as the
    /// contiguous runs they are stored in.
    pub fn matches_in_lines(&self, first: u64, limit: u64) -> impl Iterator<Item = &[FindMatch]> {
        self.matches.runs(self.lines_cut(first, limit))
    }

    /// How many matches cover any of the lines `first..limit`.
    pub fn count_in_lines(&self, first: u64, limit: u64) -> usize {
        self.lines_cut(first, limit).len()
    }

    fn lines_cut(&self, first: u64, limit: u64) -> Range<usize> {
        // matches never overlap, so their ends are in buffer order as well as their starts. A
        // match covers a line of the window when it ends at `first` or later and its first line
        // from `first` on is inside it, so an empty window holds nothing.
        let from = self.matches.partition_point(|m| m.abs_end_line < first);
        let to = self
            .matches
            .partition_point(|m| m.abs_start_line.max(first) < limit)
            .max(from);
        from..to
    }

    pub const fn generation(&self) -> u64 {
        self.generation
    }

    /// Makes `m` the current match; false, changing nothing, when it is not one of `matches`.
    /// A match names itself by its absolute coordinates, which eviction does not move. Setting
    /// the match that is already current keeps whether the step to it wrapped.
    pub fn set_current(&mut self, m: FindMatch) -> bool {
        let found = self.matches.position(&m).is_some();
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

    /// The match one step from `origin`, which becomes the current match. After a match, Next
    /// takes the first match after it and Previous the last before it; within a range, Next
    /// takes the first match starting in it or after and Previous the last starting before its
    /// end. Past either end it wraps, and `status()` says so. The answer is the one the whole
    /// buffer gives: while a line the step passes over is unsearched it is Waiting on the first
    /// such line, and changes nothing.
    pub fn step(
        &mut self,
        direction: Direction,
        origin: &FindOrigin,
        b: FindBufferBounds,
    ) -> StepOutcome {
        let target = self.step_target(direction, origin);
        let line = match (origin, direction) {
            (FindOrigin::After(m), _) => m.abs_start_line,
            (FindOrigin::Within(r), Direction::Next) => r.start.line,
            // the last line holding a cell before the range's end
            (FindOrigin::Within(r), Direction::Previous) => {
                r.end.line - u64::from(r.end.x == 0 && r.end.line > r.start.line)
            }
        }
        .clamp(b.oldest, b.end.saturating_sub(1).max(b.oldest));
        // the lines the step passes over, in the order it passes them
        let passed = match (direction, target) {
            (Direction::Next, Some(c)) if !c.wrapped => [line..c.m.abs_start_line + 1, 0..0],
            (Direction::Next, Some(c)) => [line..b.end, b.oldest..c.m.abs_start_line + 1],
            (Direction::Next, None) => [line..b.end, b.lines()],
            (Direction::Previous, Some(c)) if !c.wrapped => [c.m.abs_start_line..line + 1, 0..0],
            (Direction::Previous, Some(c)) => [b.oldest..line + 1, c.m.abs_start_line..b.end],
            (Direction::Previous, None) => [b.oldest..line + 1, b.lines()],
        };
        let forward = direction == Direction::Next;
        let gap = self.searchable().and_then(|searched| {
            passed.into_iter().find_map(|lines| {
                let lines = lines.start.max(b.oldest)..lines.end.min(b.end);
                if forward {
                    searched.first_gap(lines)
                } else {
                    searched.last_gap(lines)
                }
            })
        });
        if let Some(line) = gap {
            return StepOutcome::Waiting(FindWant { line, forward });
        }
        self.current = target;
        target.map_or(StepOutcome::NoMatch, |c| StepOutcome::Landed(c.m))
    }

    fn step_target(&self, direction: Direction, origin: &FindOrigin) -> Option<Current> {
        let all = &self.matches;
        // the count of matches before the step's start, whichever way it goes
        let before = match (origin, direction) {
            (FindOrigin::After(c), Direction::Next) => all.partition_point(|m| m <= c),
            (FindOrigin::After(c), Direction::Previous) => all.partition_point(|m| m < c),
            (FindOrigin::Within(r), Direction::Next) => {
                all.partition_point(|m| m.start() < r.start)
            }
            (FindOrigin::Within(r), Direction::Previous) => {
                all.partition_point(|m| m.start() < r.end)
            }
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

    pub fn status(&self, b: FindBufferBounds) -> FindStatus {
        let current = self.current.map_or(0, |c| {
            self.matches
                .position(&c.m)
                .expect("the current match is one of the matches")
                + 1
        });
        FindStatus {
            current,
            total: self.matches.len(),
            wrapped: self.current.is_some_and(|c| c.wrapped),
            unsearched: self.searchable().map_or(0, |s| s.unsearched(b)),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    impl FindEngine {
        fn matches(&self) -> Vec<FindMatch> {
            self.in_lines(0, u64::MAX)
        }

        fn in_lines(&self, first: u64, limit: u64) -> Vec<FindMatch> {
            self.matches_in_lines(first, limit)
                .flatten()
                .copied()
                .collect()
        }
    }

    /// A logical line built the way the walker builds one: each char of each row covers
    /// `width(char)` cells, and every byte of the char maps to those cells.
    fn line(rows: &[&str], width: impl Fn(char) -> u32) -> (Vec<u8>, Vec<FindCellSpan>, u32) {
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
        (
            text,
            cells,
            u32::try_from(rows.len()).expect("a test line's rows"),
        )
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

    fn bounds(oldest: u64, end: u64) -> FindBufferBounds {
        FindBufferBounds { oldest, end }
    }

    /// A buffer of single-row logical lines, as the walker would feed them.
    struct Buffer(Vec<String>);

    impl Buffer {
        /// Line i holds "x" when `has_x(i)`, else "-".
        fn of(lines: u64, has_x: impl Fn(u64) -> bool) -> Self {
            Self(
                (0..lines)
                    .map(|i| if has_x(i) { "x" } else { "-" }.to_owned())
                    .collect(),
            )
        }

        fn end(&self) -> u64 {
            self.0.len() as u64
        }

        /// The walker: feeds the lines from..limit to a refeed of `range`, stopping after
        /// `max_lines` when given, as the sink stops a forward fill.
        fn walk(&self, e: &FindEngine, range: FindRefeedRange, lines: Range<u64>) -> FindRefeed {
            let mut r = e.refeed(range);
            for abs in lines.start..lines.end.min(self.end()) {
                let (text, cells, rows) = line(
                    &[&self.0[usize::try_from(abs).expect("a test buffer line")]],
                    narrow,
                );
                r.feed_line(abs, rows, &text, &cells);
            }
            r
        }

        /// One fill of at most `budget` lines, as `fill_chunk` in `kitty/screen.c` walks it.
        fn fill(
            &self,
            e: &mut FindEngine,
            oldest: u64,
            budget: u64,
            want: Option<FindWant>,
        ) -> bool {
            let b = bounds(oldest, self.end());
            let Some(f) = e.next_fill(b, want) else {
                return false;
            };
            let lines = if f.forward {
                f.from..f.limit.min(f.from + budget)
            } else {
                f.limit - budget.min(f.limit - f.from)..f.limit
            };
            let r = self.walk(
                e,
                FindRefeedRange {
                    from: lines.start,
                    limit: f.limit,
                },
                lines,
            );
            e.add_searched(r);
            true
        }

        fn fill_all(&self, e: &mut FindEngine, oldest: u64, budget: u64) {
            while self.fill(e, oldest, budget, None) {}
        }
    }

    /// One logical line, fed as a fill walks it: the line was unsearched.
    fn append(e: &mut FindEngine, abs: u64, rows: u32, text: &[u8], cells: &[FindCellSpan]) {
        let mut r = e.refeed(onward(abs));
        r.feed_line(abs, rows, text, cells);
        e.add_searched(r);
    }

    fn search(query: &str, case_insensitive: bool, lines: &[(u64, &[&str])]) -> Vec<FindMatch> {
        let mut e = FindEngine::default();
        e.set_query(query, case_insensitive).unwrap();
        for (abs, rows) in lines {
            let (text, cells, rows) = line(rows, narrow);
            append(&mut e, *abs, rows, &text, &cells);
        }
        e.matches()
    }

    fn m(abs_start_line: u64, start_x: u32, abs_end_line: u64, end_x: u32) -> FindMatch {
        FindMatch {
            abs_start_line,
            start_x,
            abs_end_line,
            end_x,
        }
    }

    fn starts(e: &FindEngine) -> Vec<u64> {
        e.matches().iter().map(|m| m.abs_start_line).collect()
    }

    /// An engine searching "x" over a whole `Buffer::of(lines, |_| true)`.
    fn engine_with_one_match_per_line(lines: u64) -> (FindEngine, Buffer) {
        let buf = Buffer::of(lines, |_| true);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        buf.fill_all(&mut e, 0, 1000);
        (e, buf)
    }

    const EVERYWHERE: std::ops::Range<FindPos> = FindPos { line: 0, x: 0 }..FindPos {
        line: u64::MAX,
        x: 0,
    };

    fn pos(line: u64, x: u32) -> FindPos {
        FindPos { line, x }
    }

    /// A step from the current match, or from everywhere with none: the user pressing a key.
    fn press(e: &mut FindEngine, d: Direction, b: FindBufferBounds) -> StepOutcome {
        let origin = e
            .current()
            .map_or(FindOrigin::Within(EVERYWHERE), FindOrigin::After);
        e.step(d, &origin, b)
    }

    fn landed(o: StepOutcome) -> FindMatch {
        match o {
            StepOutcome::Landed(m) => m,
            other => panic!("expected a landing, got {other:?}"),
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
        let (text, cells, rows) = line(&["a中é中z"], |c| if c == '中' { 2 } else { 1 });
        append(&mut e, 0, rows, &text, &cells);
        // a=0, 中=1..2, é=3, 中=4..5, z=6: the match ends on z, and starts on é after a wide char
        assert_eq!(e.matches(), [m(0, 3, 0, 6)]);
        e.set_query("中", false).unwrap();
        append(&mut e, 0, rows, &text, &cells);
        // a match that is itself a wide char ends on that char's last cell
        assert_eq!(e.matches(), [m(0, 1, 0, 2), m(0, 4, 0, 5)]);
    }

    #[test]
    fn a_tab_covers_its_padding() {
        let mut e = FindEngine::default();
        e.set_query("\tb", false).unwrap();
        let (text, cells, rows) = line(&["a\tb"], |c| if c == '\t' { 7 } else { 1 });
        append(&mut e, 0, rows, &text, &cells);
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
    fn the_empty_query_finds_nothing_and_has_nothing_to_search() {
        assert_eq!(search("", false, &[(0, &["abc"])]), []);
        let mut e = FindEngine::default();
        assert_eq!(e.next_fill(bounds(0, 10), None), None);
        assert_eq!(e.status(bounds(0, 10)).unsearched, 0);
        // so a step has no line to wait on: there is no match
        for d in [Direction::Next, Direction::Previous] {
            assert_eq!(press(&mut e, d, bounds(0, 10)), StepOutcome::NoMatch);
        }
    }

    #[test]
    fn a_new_query_drops_the_previous_matches_and_searched_lines() {
        let (mut e, _) = engine_with_one_match_per_line(4);
        assert_eq!(e.set_query("y", false).unwrap(), FindQueryChange::Changed);
        assert_eq!(e.matches(), []);
        assert_eq!(e.status(bounds(0, 4)).unsearched, 4);
    }

    #[test]
    fn the_same_query_again_changes_nothing() {
        let (mut e, buf) = engine_with_one_match_per_line(6);
        let b = bounds(0, buf.end());
        landed(press(&mut e, Direction::Next, b));
        landed(press(&mut e, Direction::Next, b));
        let (current, g) = (e.current(), e.generation());
        assert_eq!(e.set_query("x", false).unwrap(), FindQueryChange::Unchanged);
        assert_eq!(
            (e.current(), e.generation(), e.status(b).unsearched),
            (current, g, 0)
        );
        // the empty query is the same whichever case it ignores
        e.set_query("", false).unwrap();
        assert_eq!(e.set_query("", true).unwrap(), FindQueryChange::Unchanged);
        // the case flag is part of a real query
        e.set_query("x", false).unwrap();
        assert_eq!(e.set_query("x", true).unwrap(), FindQueryChange::Changed);
    }

    #[test]
    fn fills_of_any_size_and_order_give_the_one_full_walk() {
        let buf = Buffer::of(97, |i| i % 7 == 3 || i % 11 == 0);
        let full = {
            let mut e = FindEngine::default();
            e.set_query("x", false).unwrap();
            e.add_searched(buf.walk(&e, onward(0), 0..buf.end()));
            e.matches()
        };
        for budget in [1, 2, 5, 13, 96, 200] {
            let mut e = FindEngine::default();
            e.set_query("x", false).unwrap();
            // a want first, as a waiting step makes, then the default order
            assert!(buf.fill(
                &mut e,
                0,
                budget,
                Some(FindWant {
                    line: 40,
                    forward: true
                })
            ));
            buf.fill_all(&mut e, 0, budget);
            assert_eq!(e.matches(), &full[..], "budget {budget}");
            assert_eq!(e.status(bounds(0, buf.end())).unsearched, 0);
        }
    }

    #[test]
    fn the_default_fill_searches_back_from_the_end() {
        let buf = Buffer::of(10, |_| true);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        buf.fill(&mut e, 0, 3, None);
        assert_eq!(starts(&e), [7, 8, 9]);
        buf.fill(&mut e, 0, 3, None);
        assert_eq!(starts(&e), [4, 5, 6, 7, 8, 9]);
    }

    #[test]
    fn a_want_fills_toward_it_first() {
        let buf = Buffer::of(10, |_| true);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        buf.fill(&mut e, 0, 3, None);
        // a wrapping Next needs the oldest lines, forward
        buf.fill(
            &mut e,
            0,
            2,
            Some(FindWant {
                line: 0,
                forward: true,
            }),
        );
        assert_eq!(starts(&e), [0, 1, 7, 8, 9]);
        // a Previous needs the lines before its start, backward
        buf.fill(
            &mut e,
            0,
            2,
            Some(FindWant {
                line: 6,
                forward: false,
            }),
        );
        assert_eq!(starts(&e), [0, 1, 5, 6, 7, 8, 9]);
        // a want already searched falls back to the default
        buf.fill(
            &mut e,
            0,
            1,
            Some(FindWant {
                line: 8,
                forward: false,
            }),
        );
        assert_eq!(starts(&e), [0, 1, 4, 5, 6, 7, 8, 9]);
    }

    #[test]
    #[should_panic(expected = "a fill searches only unsearched lines")]
    fn a_fill_over_searched_lines_panics() {
        let (mut e, buf) = engine_with_one_match_per_line(4);
        let r = buf.walk(&e, onward(2), 2..4);
        e.add_searched(r);
    }

    #[test]
    #[should_panic(expected = "a refeed re-searches searched lines")]
    fn a_refeed_of_unsearched_lines_panics() {
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        let r = e.refeed(FindRefeedRange { from: 0, limit: 2 });
        e.splice(r);
    }

    #[test]
    fn a_refresh_forgets_from_the_change_and_searches_what_it_walked() {
        let (mut e, mut buf) = engine_with_one_match_per_line(8);
        let g = e.generation();
        // lines 5.. re-walked unchanged: no generation move
        let r = buf.walk(&e, onward(5), 5..8);
        e.refresh(5, r);
        assert_eq!((e.generation(), e.status(bounds(0, 8)).unsearched), (g, 0));
        // line 6 changed and the chunk reached back only to line 7: 5..7 are unsearched again
        buf.0[6] = "-".to_owned();
        let r = buf.walk(&e, onward(7), 7..8);
        e.refresh(5, r);
        assert_ne!(e.generation(), g);
        assert_eq!(starts(&e), [0, 1, 2, 3, 4, 7]);
        assert_eq!(e.status(bounds(0, 8)).unsearched, 2);
        buf.fill_all(&mut e, 0, 1);
        assert_eq!(starts(&e), [0, 1, 2, 3, 4, 5, 7]);
        // lines past the buffer's end go with the refresh too
        let r = buf.walk(&e, onward(6), 6..6);
        e.refresh(6, r);
        assert_eq!(starts(&e), [0, 1, 2, 3, 4, 5]);
    }

    #[test]
    fn a_refeed_of_searched_lines_replaces_them() {
        let (mut e, mut buf) = engine_with_one_match_per_line(6);
        e.set_current(e.matches()[4]);
        let g = e.generation();
        buf.0[4] = "-".to_owned();
        let r = buf.walk(&e, FindRefeedRange { from: 4, limit: 5 }, 4..5);
        e.splice(r);
        assert_ne!(e.generation(), g);
        assert_eq!(e.current(), None, "a current match the refeed removed ends");
        assert_eq!(starts(&e), [0, 1, 2, 3, 5]);
    }

    #[test]
    fn eviction_drops_matches_and_searched_lines() {
        let (mut e, buf) = engine_with_one_match_per_line(10);
        // evicting one line at a time crosses the point where the evicted entries are drained
        for oldest in 1..=10 {
            assert_eq!(
                e.evict_before(oldest),
                oldest < 10,
                "is the first line searched"
            );
            assert_eq!(starts(&e), (oldest..10).collect::<Vec<_>>());
            assert_eq!(e.status(bounds(oldest, buf.end())).unsearched, 0);
        }
        // eviction of unsearched lines leaves the first line unsearched
        let buf = Buffer::of(10, |_| true);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        buf.fill(&mut e, 0, 3, None);
        assert!(!e.evict_before(2));
        assert_eq!(e.status(bounds(2, 10)).unsearched, 5);
        // eviction reaching the searched lines completes what was left
        assert!(e.evict_before(7));
        assert_eq!(e.status(bounds(7, 10)).unsearched, 0);
    }

    #[test]
    fn the_generation_moves_exactly_when_the_list_does() {
        let (mut e, _) = engine_with_one_match_per_line(4);
        let g = e.generation();
        let (text, cells, rows) = line(&["y"], narrow);
        append(&mut e, 4, rows, &text, &cells);
        e.evict_before(0);
        assert_eq!(
            e.generation(),
            g,
            "a fill that finds nothing and a no-op cut are no change"
        );
        let mut empty = FindEngine::default();
        let g0 = empty.generation();
        empty.set_query("x", false).unwrap();
        empty.set_query("", false).unwrap();
        assert_eq!(
            empty.generation(),
            g0,
            "a query that leaves the list empty is no change"
        );
        let g = e.generation();
        e.evict_before(1);
        assert_ne!(e.generation(), g);
        let g = e.generation();
        e.set_query("z", false).unwrap();
        assert_ne!(e.generation(), g);
    }

    #[test]
    fn the_current_match_is_always_in_the_list() {
        let (mut e, _) = engine_with_one_match_per_line(6);
        assert!(!e.set_current(m(6, 0, 6, 0)));
        assert_eq!(e.current(), None);
        let current = e.matches()[2];
        assert!(e.set_current(current));
        e.evict_before(2);
        assert_eq!(
            e.current(),
            Some(current),
            "cuts around the current match keep it"
        );
        e.evict_before(3);
        assert_eq!(e.current(), None, "evicting its line ends it");
        e.set_current(e.matches()[0]);
        e.end_current();
        assert_eq!((e.current(), e.matches().len()), (None, 3));
    }

    /// Lines 0 and 1: "ab ab" wraps onto "ab", so its second match spans both rows; line 2
    /// holds a wide char before the match.
    fn engine_for_steps() -> (FindEngine, Vec<FindMatch>) {
        let mut e = FindEngine::default();
        e.set_query("ab", false).unwrap();
        let (text, cells, rows) = line(&["ab a", "b ab"], narrow);
        append(&mut e, 0, rows, &text, &cells);
        let (text, cells, rows) = line(&["中ab"], |c| if c == '中' { 2 } else { 1 });
        append(&mut e, 2, rows, &text, &cells);
        let all = e.matches();
        assert_eq!(
            all,
            [m(0, 0, 0, 1), m(0, 3, 1, 0), m(1, 2, 1, 3), m(2, 2, 2, 3)]
        );
        (e, all)
    }

    const STEPS: FindBufferBounds = FindBufferBounds { oldest: 0, end: 3 };

    #[test]
    fn steps_follow_buffer_order_and_wrap_at_both_ends() {
        let (mut e, all) = engine_for_steps();
        let walk = |e: &mut FindEngine, d| {
            (0..=all.len())
                .map(|_| (landed(press(e, d, STEPS)), e.status(STEPS).wrapped))
                .collect::<Vec<_>>()
        };
        assert_eq!(
            walk(&mut e, Direction::Next),
            [
                (all[0], false),
                (all[1], false),
                (all[2], false),
                (all[3], false),
                (all[0], true)
            ]
        );
        assert_eq!(
            walk(&mut e, Direction::Previous),
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
    fn a_step_within_a_range_starts_where_the_user_looks() {
        let (mut e, all) = engine_for_steps();
        let within = |r| FindOrigin::Within(r);
        // Next takes the first match starting in the range, Previous the last starting before its end
        assert_eq!(
            e.step(Direction::Next, &within(pos(0, 3)..pos(1, 3)), STEPS),
            StepOutcome::Landed(all[1])
        );
        assert_eq!(
            e.step(Direction::Previous, &within(pos(0, 3)..pos(1, 3)), STEPS),
            StepOutcome::Landed(all[2])
        );
        assert!(!e.status(STEPS).wrapped);
        // nothing at or after the start wraps to the first; nothing before the end wraps to the last
        assert_eq!(
            e.step(Direction::Next, &within(pos(3, 0)..pos(5, 0)), STEPS),
            StepOutcome::Landed(all[0])
        );
        assert!(e.status(STEPS).wrapped);
        assert_eq!(
            e.step(Direction::Previous, &within(pos(0, 0)..pos(0, 0)), STEPS),
            StepOutcome::Landed(all[3])
        );
        assert!(e.status(STEPS).wrapped);
        // after a match that is no longer in the list, the step goes by its coordinates
        assert_eq!(
            e.step(Direction::Next, &FindOrigin::After(m(1, 0, 1, 0)), STEPS),
            StepOutcome::Landed(all[2])
        );
    }

    #[test]
    fn with_no_matches_a_step_finds_nothing_once_all_is_searched() {
        let buf = Buffer::of(3, |_| false);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        let b = bounds(0, 3);
        assert_eq!(
            press(&mut e, Direction::Next, b),
            StepOutcome::Waiting(FindWant {
                line: 0,
                forward: true
            })
        );
        buf.fill_all(&mut e, 0, 1);
        assert_eq!(press(&mut e, Direction::Previous, b), StepOutcome::NoMatch);
        assert_eq!(
            e.status(b),
            FindStatus {
                current: 0,
                total: 0,
                wrapped: false,
                unsearched: 0
            }
        );
    }

    #[test]
    fn a_step_waits_while_unsearched_lines_could_hold_its_answer() {
        // matches on lines 2 and 15 of 20; the view is lines 16..20
        let buf = Buffer::of(20, |i| i == 2 || i == 15);
        let b = bounds(0, 20);
        let view = FindOrigin::Within(pos(16, 0)..pos(20, 0));
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        buf.fill(&mut e, 0, 6, None); // lines 14..20
        // Previous from the view finds line 15 with every line between searched
        assert_eq!(
            e.step(Direction::Previous, &view, b),
            StepOutcome::Landed(m(15, 0, 15, 0))
        );
        // Next from the view wraps: the first match overall needs the oldest lines
        e.end_current();
        assert_eq!(
            e.step(Direction::Next, &view, b),
            StepOutcome::Waiting(FindWant {
                line: 0,
                forward: true
            })
        );
        assert_eq!(e.current(), None, "a waiting step changes nothing");
        // Previous from line 15 finds nothing searched before it: it needs line 13, backward
        let after15 = FindOrigin::After(m(15, 0, 15, 0));
        assert_eq!(
            e.step(Direction::Previous, &after15, b),
            StepOutcome::Waiting(FindWant {
                line: 13,
                forward: false
            })
        );
        // the want's fill answers it: lines 0..2 forward hold no match before line 2's
        buf.fill(
            &mut e,
            0,
            3,
            Some(FindWant {
                line: 0,
                forward: true,
            }),
        );
        assert_eq!(
            e.step(Direction::Next, &view, b),
            StepOutcome::Landed(m(2, 0, 2, 0))
        );
        // with lines 3..14 unsearched, Previous from 15 still waits; once searched it lands on 2
        assert!(matches!(
            e.step(Direction::Previous, &after15, b),
            StepOutcome::Waiting(_)
        ));
        buf.fill_all(&mut e, 0, 4);
        assert_eq!(
            e.step(Direction::Previous, &after15, b),
            StepOutcome::Landed(m(2, 0, 2, 0))
        );
    }

    #[test]
    fn previous_from_rows_ending_at_a_line_start_needs_nothing_from_that_line() {
        // the view is lines 10..15, all searched; line 15 below it is not
        let buf = Buffer::of(20, |i| i == 12);
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        let want = FindWant {
            line: 10,
            forward: true,
        };
        buf.fill(&mut e, 0, 5, Some(want));
        let view = FindOrigin::Within(pos(10, 0)..pos(15, 0));
        assert_eq!(
            e.step(Direction::Previous, &view, bounds(0, 20)),
            StepOutcome::Landed(m(12, 0, 12, 0))
        );
    }

    #[test]
    fn the_status_counts_the_current_match_among_the_live_ones() {
        let (mut e, all) = engine_for_steps();
        assert_eq!((e.status(STEPS).current, e.status(STEPS).total), (0, 4));
        landed(press(&mut e, Direction::Previous, STEPS));
        assert_eq!(e.status(STEPS).current, 4);
        e.set_current(all[2]);
        assert_eq!(
            (e.status(STEPS).current, e.status(STEPS).wrapped),
            (3, false)
        );
        // eviction counts from the oldest live match
        e.evict_before(1);
        assert_eq!(
            (e.status(bounds(1, 3)).current, e.status(bounds(1, 3)).total),
            (1, 2)
        );
    }

    #[test]
    fn the_matches_in_lines_are_those_touching_them() {
        let mut e = FindEngine::default();
        e.set_query("xx", false).unwrap();
        // lines 0, 1-2 (one match wrapping), 4
        for (abs, rows) in [(0, &["xx"][..]), (1, &["   x", "x  "]), (4, &["xx"])] {
            let (text, cells, rows) = line(rows, narrow);
            append(&mut e, abs, rows, &text, &cells);
        }
        let all = e.matches();
        assert_eq!(all, [m(0, 0, 0, 1), m(1, 3, 2, 0), m(4, 0, 4, 1)]);
        assert_eq!(
            e.in_lines(2, 4),
            &all[1..2],
            "a match ending in the window is in it"
        );
        assert_eq!(
            e.in_lines(0, 2),
            &all[..2],
            "a match starting in the window is in it"
        );
        assert_eq!(e.in_lines(3, 4), []);
        assert_eq!(e.in_lines(0, 9), &all[..]);
        assert_eq!(e.in_lines(4, 4), [], "an empty window holds nothing");
        assert_eq!(e.in_lines(2, 2), [], "nor one a match crosses");
        assert_eq!(e.count_in_lines(2, 4), 1);
        assert_eq!(e.count_in_lines(2, 2), 0);
    }

    #[test]
    fn a_logical_line_walks_its_rows_past_its_last_text() {
        // two rows, the second erased: the next line starts after both
        let mut e = FindEngine::default();
        e.set_query("x", false).unwrap();
        let (text, cells, _) = line(&["x"], narrow);
        append(&mut e, 0, 2, &text, &cells);
        assert_eq!(e.status(bounds(0, 3)).unsearched, 1);
        assert_eq!(
            e.next_fill(bounds(0, 3), None),
            Some(FindFill {
                from: 2,
                limit: 3,
                forward: false
            })
        );
    }

    #[test]
    #[should_panic(expected = "lines are fed in buffer order, from the range's start")]
    fn an_out_of_order_feed_panics() {
        let buf = Buffer::of(4, |_| true);
        let e = FindEngine::default();
        let mut r = buf.walk(&e, onward(3), 3..4);
        let (text, cells, rows) = line(&["x"], narrow);
        r.feed_line(2, rows, &text, &cells);
    }

    #[test]
    #[should_panic(expected = "a refeed line lies in its range")]
    fn a_line_past_the_refeed_range_panics() {
        let e = FindEngine::default();
        let mut r = e.refeed(FindRefeedRange { from: 2, limit: 3 });
        let (text, cells, rows) = line(&["x"], narrow);
        r.feed_line(3, rows, &text, &cells);
    }
}
