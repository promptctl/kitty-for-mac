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

// One logical line (physical lines joined through the wrapped-line flag) as UTF-8 text with
// one FindCellSpan per byte, so a regex match on bytes [s, e) covers cells[s] .. cells[e - 1].
// text and cells belong to the walker and are valid only for the duration of the sink call.
typedef struct {
    uint64_t abs_first_line;
    const char *text;
    size_t text_len;
    const FindCellSpan *cells;
} FindLogicalLine;

// Receives each logical line in buffer order; returning false aborts the walk.
typedef bool (*FindLineSink)(void *ctx, const FindLogicalLine *line);

// Feeds every logical line of the main buffer, oldest history line through the last screen
// row, to sink. Returns false when the sink returned false, or when memory ran out, in which
// case MemoryError is set.
bool find_walk_main_buffer(Screen *self, FindLineSink sink, void *ctx);
