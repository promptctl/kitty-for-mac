/*
 * find.h
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "screen.h"

// The cells one codepoint of a logical line came from: physical row `row` counted from the
// logical line's first physical line, cells x..x_last inclusive (a wide char or a tab with
// its padding covers several).
typedef struct {
    uint32_t row, x, x_last;
} FindCellSpan;

// Whether a logical line's first row starts a line or continues one that left the top of
// scrollback, as only the oldest line can. Rust's FindLineStart.
typedef enum { FIND_LINE_STARTS_LINE, FIND_LINE_CONTINUES_EVICTED } FindLineStart;

// One logical line (physical lines joined through the wrapped-line flag) as UTF-8 text with
// one FindCellSpan per byte, so a regex match on bytes [s, e) covers cells[s] .. cells[e - 1].
// It is rows physical lines from abs_first_line, its rows past the last text included.
// text and cells belong to the walker and are valid only for the duration of the sink call.
typedef struct {
    uint64_t abs_first_line;
    FindLineStart start;
    uint32_t rows;
    const char *text;
    size_t text_len;
    const FindCellSpan *cells;
} FindLogicalLine;

// The absolute lines a walk and its refeed cover, [from, limit): from is the first line of a
// logical line, and limit UINT64_MAX is "onward", wherever the buffer now ends. The walker and
// the engine's splice take the same value, so they agree on which lines were re-fed. Made only
// by the two constructors below, which are the only code that finds a logical line's bounds.
typedef struct {
    uint64_t from, limit;
} FindRefeedRange;

// From the start of the logical line holding abs_line onward. abs_line is in the buffer or is
// its end, where the range re-feeds nothing and drops the matches past the end.
FindRefeedRange find_onward_range(Screen *self, uint64_t abs_line);
// The logical line holding abs_line, which is in the buffer.
FindRefeedRange find_logical_line_range(Screen *self, uint64_t abs_line);

// The main buffer as it is now, from its oldest line to just past its last screen row: what the
// engine is handed by every call that needs the buffer's bounds, since it keeps no copy.
typedef struct {
    uint64_t oldest, end;
} FindBufferBounds;
FindBufferBounds find_buffer_bounds(const Screen *self);

// Receives each logical line in buffer order; returning false stops the walk.
typedef bool (*FindLineSink)(void *ctx, const FindLogicalLine *line);

typedef enum { FIND_WALK_COMPLETE, FIND_WALK_SINK_STOPPED, FIND_WALK_OUT_OF_MEMORY } FindWalkResult;

// Feeds each logical line of the main buffer in lines, through the last screen row at most,
// to sink. Sets no Python error: the caller turns FIND_WALK_OUT_OF_MEMORY into one.
FindWalkResult find_walk_main_buffer(Screen *self, FindRefeedRange lines, FindLineSink sink, void *ctx);

// A multicell char's row index y is SCALE_BITS wide and less than its scale, so an edit made
// from the screen reaches at most this many rows up into history (nuke_multicell_char_at and
// halve_multicell_width in kitty/screen.c, through range_line_).
#define FIND_MULTICELL_REACH ((1u << SCALE_BITS) - 2)

// What the engine was last fed, so the next read can tell which lines' text may have changed:
// the main buffer's CPU cells (which hold all of a cell's text) from FIND_MULTICELL_REACH
// history rows above screen row 0 through the last screen row, and the oldest line then and
// whether it continued an evicted one.
typedef struct FindFedRecord {
    uint64_t first_line, abs_oldest_line;
    bool oldest_line_continues_evicted;
    index_type rows, columns;
    CPUCell *cells;
} FindFedRecord;

// A new record of the buffer as it is now; NULL when out of memory.
FindFedRecord* find_record_capture(Screen *self);
void find_record_free(FindFedRecord *r);
// The first absolute line whose text may differ from what r recorded, taking the history
// ring's own rewrites into account: a line in the buffer, or the buffer's end when only
// recorded lines past it are gone; UINT64_MAX when there is none.
uint64_t find_changed_from(Screen *self, const FindFedRecord *r);
