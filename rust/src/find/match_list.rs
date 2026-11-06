//! The match list: every match in buffer order, held as an unrolled list of sorted blocks so
//! an edit rebuilds only the blocks it touches. A backward fill inserts before every match
//! already found; in one contiguous list that insert, and each growth of the list, would move
//! all of them.

use super::FindMatch;
use std::ops::Range;

/// The most matches a block holds. An edit copies at most twice this many matches besides the
/// ones it inserts, and the list holds at most 2 * len / `BLOCK_CAP` + 1 blocks.
const BLOCK_CAP: usize = 4096;

#[derive(Default, Debug)]
pub struct MatchList {
    /// Their concatenation is the list. Each holds 1 to `BLOCK_CAP` matches, and no two
    /// neighbours fit `BLOCK_CAP` together: they would have been merged.
    blocks: Vec<Vec<FindMatch>>,
}

impl MatchList {
    pub fn len(&self) -> usize {
        self.blocks.iter().map(Vec::len).sum()
    }

    pub fn clear(&mut self) {
        self.blocks.clear();
    }

    pub fn first(&self) -> Option<&FindMatch> {
        self.blocks.first().and_then(|b| b.first())
    }

    pub fn last(&self) -> Option<&FindMatch> {
        self.blocks.last().and_then(|b| b.last())
    }

    pub fn get(&self, i: usize) -> Option<&FindMatch> {
        let (b, at) = self.locate(i);
        self.blocks.get(b).map(|b| &b[at])
    }

    /// As `slice::partition_point`: the count of the leading matches `pred` holds for.
    pub fn partition_point(&self, pred: impl Fn(&FindMatch) -> bool) -> usize {
        // a block whose last match holds holds throughout
        let b = self
            .blocks
            .partition_point(|b| pred(b.last().expect("a block is never empty")));
        let before: usize = self.blocks[..b].iter().map(Vec::len).sum();
        before + self.blocks.get(b).map_or(0, |b| b.partition_point(pred))
    }

    /// Where `m` is in the list, if it is there.
    pub fn position(&self, m: &FindMatch) -> Option<usize> {
        let i = self.partition_point(|x| x < m);
        (self.get(i) == Some(m)).then_some(i)
    }

    /// The matches at `range`, as the contiguous runs they are stored in, in order.
    pub fn runs(&self, range: Range<usize>) -> impl Iterator<Item = &[FindMatch]> {
        let starts = self.blocks.iter().scan(0, |start, b| {
            let s = *start;
            *start += b.len();
            Some(s)
        });
        self.blocks.iter().zip(starts).filter_map(move |(b, s)| {
            let run = range.start.max(s)..range.end.min(s + b.len());
            (run.start < run.end).then(|| &b[run.start - s..run.end - s])
        })
    }

    // [LAW:single-enforcer] every edit of the list ends here, eviction included, and leaves
    // the blocks non-empty and merged
    /// Replaces the matches at `range` with `found`, in O(found + `BLOCK_CAP` + blocks).
    pub fn replace(&mut self, range: Range<usize>, found: Vec<FindMatch>) {
        // [LAW:no-silent-failure] a reversed range would insert inside the matches it keeps
        assert!(range.start <= range.end, "a replaced range is in order");
        let ((first, at), (last, to)) = (self.locate(range.start), self.locate(range.end));
        let n = self.blocks.len();
        // the kept matches around the range: the head is copied out, and the tail is the end
        // block itself, trimmed in place, so a cut at the list's front (eviction, a backward
        // fill's insert) allocates nothing
        let head = self.blocks.get(first).map_or_else(Vec::new, |b| b[..at].to_vec());
        let tail = self.blocks.get_mut(last).map_or_else(Vec::new, |b| {
            b.drain(..to);
            std::mem::take(b)
        });
        // a few matches, as streaming output finds, become a block as they are, uncopied
        let found = if found.len() <= BLOCK_CAP {
            vec![found]
        } else {
            found.chunks(BLOCK_CAP).map(<[FindMatch]>::to_vec).collect()
        };
        let pieces: Vec<_> = std::iter::once(head)
            .chain(found)
            .chain(std::iter::once(tail))
            .filter(|b| !b.is_empty())
            .collect();
        let inserted = pieces.len();
        self.blocks.splice(first.min(n)..(last + 1).min(n), pieces);
        // only the neighbours around the edit can fit together now: a merge only grows a
        // block, so it never makes another pair fit
        let mut i = first.saturating_sub(1);
        let mut end = first + inserted;
        while i < end && i + 1 < self.blocks.len() {
            if self.blocks[i].len() + self.blocks[i + 1].len() <= BLOCK_CAP {
                let next = self.blocks.remove(i + 1);
                self.blocks[i].extend(next);
                end -= 1;
            } else {
                i += 1;
            }
        }
    }

    /// The block holding the match at `i`, and its offset there: (blocks, 0) past the end.
    fn locate(&self, mut i: usize) -> (usize, usize) {
        for (b, block) in self.blocks.iter().enumerate() {
            if i < block.len() {
                return (b, i);
            }
            i -= block.len();
        }
        (self.blocks.len(), 0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn all(list: &MatchList) -> impl Iterator<Item = FindMatch> + '_ {
        list.runs(0..list.len()).flatten().copied()
    }

    fn at(line: u64) -> FindMatch {
        FindMatch {
            abs_start_line: line,
            start_x: 0,
            abs_end_line: line,
            end_x: 0,
        }
    }

    /// A deterministic stream of numbers, so a failing sequence replays.
    struct Lcg(u64);

    impl Lcg {
        fn below(&mut self, n: usize) -> usize {
            self.0 = self
                .0
                .wrapping_mul(6_364_136_223_846_793_005)
                .wrapping_add(1_442_695_040_888_963_407);
            usize::try_from(self.0 >> 33).expect("31 bits fit a usize") % n.max(1)
        }
    }

    fn assert_holds(list: &MatchList, model: &[FindMatch]) {
        assert!(
            list.blocks
                .iter()
                .all(|b| (1..=BLOCK_CAP).contains(&b.len())),
            "every block holds 1 to BLOCK_CAP matches"
        );
        assert!(
            list.blocks
                .windows(2)
                .all(|w| w[0].len() + w[1].len() > BLOCK_CAP),
            "no two neighbours fit together"
        );
        assert!(list.blocks.len() <= 2 * model.len() / BLOCK_CAP + 1);
        assert_eq!(all(list).collect::<Vec<_>>(), model);
        assert_eq!(list.len(), model.len());
    }

    #[test]
    fn edits_match_a_plain_vec() {
        let (mut list, mut model, mut rng) = (MatchList::default(), Vec::new(), Lcg(7));
        let mut next_line = 0;
        for step in 0..3000 {
            let start = rng.below(model.len() + 1);
            let end = start + rng.below((model.len() - start).min(3 * BLOCK_CAP) + 1);
            // mostly small edits, sometimes a fill's worth, sometimes nothing: eviction
            let k = match rng.below(10) {
                0 => 0,
                1 => rng.below(3 * BLOCK_CAP),
                _ => rng.below(64),
            };
            // the list does not order what it holds; distinct lines make every move visible
            let found: Vec<_> = (next_line..next_line + k as u64).map(at).collect();
            next_line += k as u64;
            model.splice(start..end, found.iter().copied());
            list.replace(start..end, found);
            assert_holds(&list, &model);
            let i = rng.below(model.len() + 1);
            assert_eq!(list.get(i), model.get(i), "step {step}");
            let r = rng.below(model.len() + 1);
            let r = r..r + rng.below(model.len() - r + 1);
            assert_eq!(
                list.runs(r.clone()).flatten().copied().collect::<Vec<_>>(),
                &model[r]
            );
        }
    }

    #[test]
    fn searches_find_what_a_sorted_vec_finds() {
        let mut list = MatchList::default();
        // inserted back to front, as a backward fill inserts its chunks
        for chunk in (0..20u64).rev() {
            list.replace(
                0..0,
                (chunk * 1000..chunk * 1000 + 999)
                    .map(at)
                    .collect::<Vec<_>>(),
            );
        }
        let model: Vec<_> = all(&list).collect();
        assert_holds(&list, &model);
        for line in [0, 1, 998, 999, 1000, 12_345, 19_998, 19_999, 20_000] {
            let pred = |m: &FindMatch| m.abs_start_line < line;
            assert_eq!(list.partition_point(pred), model.partition_point(pred));
            assert_eq!(
                list.position(&at(line)),
                model.binary_search(&at(line)).ok()
            );
        }
        assert_eq!((list.first(), list.last()), (model.first(), model.last()));
    }

    #[test]
    fn a_dense_backward_fill_keeps_blocks_few() {
        // 'n' over a million wide lines: three matches a line, 37k a chunk
        let mut list = MatchList::default();
        for chunk in (0..80u64).rev() {
            list.replace(
                0..0,
                (chunk * 37_500..(chunk + 1) * 37_500)
                    .map(at)
                    .collect::<Vec<_>>(),
            );
            let model: Vec<_> = all(&list).collect();
            assert_holds(&list, &model);
        }
        assert_eq!(list.len(), 3_000_000);
    }

    #[test]
    fn eviction_copies_at_most_a_block() {
        let mut list = MatchList::default();
        list.replace(0..0, (0..40_000).map(at).collect::<Vec<_>>());
        let blocks = list.blocks.len();
        // one match evicted a frame, as streaming output scrolls one line off at a time
        list.replace(0..1, Vec::new());
        assert_eq!(list.first(), Some(&at(1)));
        // the front block shrank and every block behind it is the one it was
        assert_eq!(list.blocks.len(), blocks);
        assert_eq!(list.blocks[0].len(), BLOCK_CAP - 1);
    }
}
