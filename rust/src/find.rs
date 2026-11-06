//! The find engine: matches a query against the logical lines kitty's cell walker
//! (kitty/find.c) feeds it, keeping every match in absolute buffer coordinates.

use regex::bytes::{Regex, RegexBuilder};

/// The cells one byte of a logical line came from, as kitty/kitty_rs.h defines it.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct FindCellSpan {
    pub row: u32,
    pub x: u32,
    pub x_last: u32,
}

/// One match: from (abs_start_line, start_x) through (abs_end_line, end_x), both cells
/// included. Field order is buffer order, so the derived Ord sorts matches as they appear.
#[repr(C)]
#[derive(Clone, Copy, Debug, PartialEq, Eq, PartialOrd, Ord)]
pub struct FindMatch {
    pub abs_start_line: u64,
    pub start_x: u32,
    pub abs_end_line: u64,
    pub end_x: u32,
}

#[derive(Default)]
pub struct FindEngine {
    /// None is the empty query: there is nothing to find, so every line yields no match.
    regex: Option<Regex>,
    /// Sorted in buffer order: lines are fed oldest first.
    matches: Vec<FindMatch>,
}

impl FindEngine {
    /// Starts a new search, dropping every match of the previous one. The query is a
    /// literal: `regex::escape` puts it through the regex crate's literal fast path.
    pub fn set_query(&mut self, query: &str, case_insensitive: bool) -> Result<(), regex::Error> {
        self.matches.clear();
        // a query that fails to build leaves the empty query, never the previous one
        self.regex = None;
        self.regex = (!query.is_empty())
            .then(|| RegexBuilder::new(&regex::escape(query)).case_insensitive(case_insensitive).build())
            .transpose()?;
        Ok(())
    }

    /// Appends the matches in one logical line, whose byte i of `text` came from `cells[i]`.
    pub fn feed_line(&mut self, abs_first_line: u64, text: &[u8], cells: &[FindCellSpan]) {
        assert_eq!(text.len(), cells.len(), "a logical line has one cell span per byte");
        debug_assert!(
            self.matches.last().is_none_or(|m| m.abs_end_line < abs_first_line),
            "lines are fed in buffer order"
        );
        let found = self.regex.iter().flat_map(|re| re.find_iter(text)).filter(|m| !m.is_empty());
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

    /// Drops the matches starting at `abs_line` or later, ahead of feeding those lines again.
    pub fn drop_lines_from(&mut self, abs_line: u64) {
        self.matches.truncate(self.matches.partition_point(|m| m.abs_start_line < abs_line));
    }

    /// Drops the matches starting before `abs_line`: their lines have left the history ring.
    pub fn evict_before(&mut self, abs_line: u64) {
        self.matches.drain(..self.matches.partition_point(|m| m.abs_start_line < abs_line));
    }

    pub fn matches(&self) -> &[FindMatch] {
        &self.matches
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A logical line built the way the walker builds one: each char of each row covers
    /// `width(char)` cells, and every byte of the char maps to those cells.
    fn line(rows: &[&str], width: impl Fn(char) -> u32) -> (Vec<u8>, Vec<FindCellSpan>) {
        let (mut text, mut cells) = (Vec::new(), Vec::new());
        for (row, s) in rows.iter().enumerate() {
            let mut x = 0;
            for c in s.chars() {
                let span = FindCellSpan { row: row as u32, x, x_last: x + width(c) - 1 };
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

    fn search(query: &str, case_insensitive: bool, lines: &[(u64, &[&str])]) -> Vec<FindMatch> {
        let mut e = FindEngine::default();
        e.set_query(query, case_insensitive).unwrap();
        for (abs, rows) in lines {
            let (text, cells) = line(rows, narrow);
            e.feed_line(*abs, &text, &cells);
        }
        e.matches().to_vec()
    }

    fn m(abs_start_line: u64, start_x: u32, abs_end_line: u64, end_x: u32) -> FindMatch {
        FindMatch { abs_start_line, start_x, abs_end_line, end_x }
    }

    #[test]
    fn every_match_in_one_line() {
        assert_eq!(search("ab", false, &[(7, &["xabyab"])]), [m(7, 1, 7, 2), m(7, 4, 7, 5)]);
    }

    #[test]
    fn a_match_across_wrapped_rows_ends_on_the_later_row() {
        assert_eq!(search("cdef", false, &[(3, &["abcd", "efgh"])]), [m(3, 2, 4, 1)]);
    }

    #[test]
    fn matches_from_later_lines_follow_in_buffer_order() {
        assert_eq!(search("x", false, &[(0, &["x"]), (1, &["ax", "x"]), (3, &["x"])]), [
            m(0, 0, 0, 0),
            m(1, 1, 1, 1),
            m(2, 0, 2, 0),
            m(3, 0, 3, 0)
        ]);
    }

    #[test]
    fn wide_and_multibyte_chars_map_to_their_cells() {
        let mut e = FindEngine::default();
        e.set_query("é中z", false).unwrap();
        let (text, cells) = line(&["a中é中z"], |c| if c == '中' { 2 } else { 1 });
        e.feed_line(0, &text, &cells);
        // a=0, 中=1..2, é=3, 中=4..5, z=6: the match ends on z, and starts on é after a wide char
        assert_eq!(e.matches(), [m(0, 3, 0, 6)]);
        e.set_query("中", false).unwrap();
        e.feed_line(0, &text, &cells);
        // a match that is itself a wide char ends on that char's last cell
        assert_eq!(e.matches(), [m(0, 1, 0, 2), m(0, 4, 0, 5)]);
    }

    #[test]
    fn a_tab_covers_its_padding() {
        let mut e = FindEngine::default();
        e.set_query("\tb", false).unwrap();
        let (text, cells) = line(&["a\tb"], |c| if c == '\t' { 7 } else { 1 });
        e.feed_line(0, &text, &cells);
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
        e.feed_line(0, &text, &cells);
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
            e.feed_line(abs, &text, &cells);
        }
        e.drop_lines_from(2);
        assert_eq!(e.matches(), &all[..2]);
        e.evict_before(1);
        assert_eq!(e.matches(), &all[1..2]);
    }
}
