/*
 * kitty_rs.h
 * The C ABI of the Rust crate in rust/ (libkitty_rs.a, linked into
 * fast_data_types on macOS). Hand-written: each declaration here must match its
 * #[unsafe(no_mangle)] extern "C" fn in rust/src/lib.rs, and each struct it uses
 * (here or from the walker's find.h) its #[repr(C)] twin in rust/src. Rust panics abort, they never unwind into C.
 */

#pragma once

#include "find.h"

// A match from (abs_start_line, start_x) through (abs_end_line, end_x), both cells included.
typedef struct {
    uint64_t abs_start_line;
    uint32_t start_x;
    uint64_t abs_end_line;
    uint32_t end_x;
} FindMatch;

// The find engine: a query and its matches, kept sorted in buffer order.
typedef struct FindEngine FindEngine;

// Never NULL: allocation failure aborts. The new engine holds the empty query.
FindEngine* kitty_rs_find_engine_new(void);
void kitty_rs_find_engine_free(FindEngine *engine);
// Drops every match and sets a literal query; "" is the query that finds nothing. Returns
// false when the query cannot be compiled, leaving the empty query and the reason in err
// (NUL-terminated, truncated to err_size). utf8 must be non-NULL (even when len is 0) and
// valid UTF-8; err_size must be at least 1.
bool kitty_rs_find_set_query(FindEngine *engine, const uint8_t *utf8, size_t len, bool case_insensitive, char *err, size_t err_size);
// Appends the matches in one logical line; lines must arrive in buffer order.
void kitty_rs_find_feed_line(FindEngine *engine, const FindLogicalLine *line);
// Drops the matches starting at abs_line or later, ahead of feeding those lines again.
void kitty_rs_find_drop_lines_from(FindEngine *engine, uint64_t abs_line);
// Drops the matches starting before abs_line.
void kitty_rs_find_evict_before(FindEngine *engine, uint64_t abs_line);
// The matches in buffer order, *count of them, valid until the engine next changes.
const FindMatch* kitty_rs_find_matches(const FindEngine *engine, size_t *count);
// The matches covering any of the lines [first, limit), *count of them, valid until the
// engine next changes.
const FindMatch* kitty_rs_find_matches_in_lines(const FindEngine *engine, uint64_t first, uint64_t limit, size_t *count);
// Counts the changes to the match list: equal generations mean the same matches.
uint64_t kitty_rs_find_generation(const FindEngine *engine);
// The current match, always one of the matches: set_query and the cuts that drop it end it.
// Makes m the current match, returning false and changing nothing when it is not one of the matches.
bool kitty_rs_find_set_current(FindEngine *engine, FindMatch m);
// Leaves no current match; the matches stay.
void kitty_rs_find_end_current(FindEngine *engine);
// A cell position, ordered as a match's start is: by line, then column.
typedef struct {
    uint64_t line;
    uint32_t x;
} FindPos;
// Makes the match one step from the current one, later unless backwards, the current match
// and writes it to *out, returning false and writing nothing when there are no matches.
// With no current match the step starts from [from_start, from_end), where the user is
// looking: forwards the first match starting in it or after, backwards the last starting
// before from_end. Past either end it wraps, and kitty_rs_find_status says so.
bool kitty_rs_find_step(FindEngine *engine, bool backwards, FindPos from_start, FindPos from_end, FindMatch *out);
// current is 1-based among the matches, 0 when there is no current match; wrapped says the
// step that reached it went past an end.
typedef struct {
    size_t current, total;
    bool wrapped;
} FindStatus;
FindStatus kitty_rs_find_status(const FindEngine *engine);
