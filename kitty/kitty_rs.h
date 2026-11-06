/*
 * kitty_rs.h
 * The C ABI of the Rust crate in rust/ (libkitty_rs.a, linked into
 * fast_data_types on macOS). Hand-written: each declaration here must match its
 * #[unsafe(no_mangle)] extern "C" fn in rust/src/lib.rs, and each struct its
 * #[repr(C)] twin in rust/src. Rust panics abort, they never unwind into C.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// The cells one codepoint of a logical line came from: physical row `row` counted from the
// logical line's first physical line, cells x..x_last inclusive (a wide char or a tab with
// its padding covers several).
typedef struct {
    uint32_t row, x, x_last;
} FindCellSpan;

// One logical line (physical lines joined through the wrapped-line flag) as UTF-8 text with
// one FindCellSpan per byte, so a regex match on bytes [s, e) covers cells[s] .. cells[e - 1].
// text and cells belong to the walker and are valid only for the duration of the sink call.
typedef struct {
    uint64_t abs_first_line;
    const char *text;
    size_t text_len;
    const FindCellSpan *cells;
} FindLogicalLine;

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
// false when the query cannot be compiled, leaving the empty query. utf8 must be non-NULL
// (even when len is 0) and valid UTF-8.
bool kitty_rs_find_set_query(FindEngine *engine, const uint8_t *utf8, size_t len, bool case_insensitive);
// Appends the matches in one logical line; lines must arrive in buffer order.
void kitty_rs_find_feed_line(FindEngine *engine, const FindLogicalLine *line);
// Drops the matches starting at abs_line or later, ahead of feeding those lines again.
void kitty_rs_find_drop_lines_from(FindEngine *engine, uint64_t abs_line);
// Drops the matches starting before abs_line.
void kitty_rs_find_evict_before(FindEngine *engine, uint64_t abs_line);
// The matches in buffer order, *count of them, valid until the engine next changes.
const FindMatch* kitty_rs_find_matches(const FindEngine *engine, size_t *count);
