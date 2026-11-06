/*
 * find.c
 * Distributed under terms of the GPL3 license.
 */

#include "find.h"
#include "lineops.h"
#include "charsets.h"

typedef struct {
    char *text;
    FindCellSpan *cells;
    size_t len, capacity;
} FindLineBuf;

static void
free_find_line_buf(FindLineBuf *b) {
    free(b->text);
    free(b->cells);
}

static bool
reserve(FindLineBuf *b, size_t capacity) {
    char *text = realloc(b->text, capacity);
    if (!text) return false;
    b->text = text;
    FindCellSpan *cells = realloc(b->cells, capacity * sizeof(cells[0]));
    if (!cells) return false;
    b->cells = cells;
    b->capacity = capacity;
    return true;
}

static bool
append_codepoint(FindLineBuf *b, char_type ch, FindCellSpan span) {
    // encode_utf8 writes at most 4 bytes
    if (b->len + 4 > b->capacity && !reserve(b, 2 * b->capacity)) return false;
    const unsigned n = encode_utf8(ch, b->text + b->len);
    for (unsigned i = 0; i < n; i++) b->cells[b->len++] = span;
    return true;
}

// The text rules of unicode_in_range (as line_as_unicode applies them) and apply_mark, with
// each codepoint's cells recorded: trailing never-written cells are not text.
static bool
append_physical_line(FindLineBuf *b, const Line *line, uint32_t row) {
    RAII_ListOfChars(lc);
    const index_type limit = xlimit_for_line(line);
    for (index_type x = 0; x < limit; x++) {
        const CPUCell *c = line->cpu_cells + x;
        // wide-char continuation cells and the lower rows of scaled text carry no text of their own
        if (c->is_multicell && (c->x || c->y)) continue;
        text_in_cell(c, line->text_cache, &lc);
        FindCellSpan span = {.row = row, .x = x, .x_last = x};
        // a never-written interior cell reads as one space
        if (!lc.chars[0]) lc.chars[0] = ' ';
        if (lc.chars[0] == '\t') {
            span.x_last = tab_padding_end(line->cpu_cells, x, limit, lc.count > 1 ? lc.chars[1] : 0);
            // the tab's second char is its padding count, not text
            lc.count = 1;
        } else if (c->is_multicell) span.x_last = MIN(line->xnum, x + mcd_x_limit(c)) - 1;
        // a grapheme's codepoints (base and combining marks) all map to its one cell
        for (unsigned i = 0; i < lc.count; i++) {
            if (!append_codepoint(b, lc.chars[i], span)) return false;
        }
        x = span.x_last;
    }
    return true;
}

FindWalkResult
find_walk_main_buffer(Screen *self, FindLineSink sink, void *ctx) {
    HistoryBuf *hb = self->historybuf;
    LineBuf *lb = self->main_linebuf;
    const index_type num_lines = hb->count + lb->ynum;
    __attribute__((cleanup(free_find_line_buf))) FindLineBuf b = {0};
    // allocated up front so even an empty first line hands the sink non-NULL pointers
    if (!reserve(&b, 4 * lb->xnum)) return FIND_WALK_OUT_OF_MEMORY;
    index_type first = 0;
    for (index_type i = 0; i < num_lines; i++) {
        Line *line;
        if (i < hb->count) {
            historybuf_init_line(hb, hb->count - 1 - i, hb->line);
            line = hb->line;
        } else {
            linebuf_init_line(lb, i - hb->count);
            line = lb->line;
        }
        if (!append_physical_line(&b, line, i - first)) return FIND_WALK_OUT_OF_MEMORY;
        const bool continues = line->cpu_cells[line->xnum - 1].next_char_was_wrapped && i + 1 < num_lines;
        if (!continues) {
            const FindLogicalLine l = {.abs_first_line = historybuf_abs_line(hb, first), .text = b.text, .text_len = b.len, .cells = b.cells};
            if (!sink(ctx, &l)) return FIND_WALK_SINK_STOPPED;
            b.len = 0;
            first = i + 1;
        }
    }
    return FIND_WALK_COMPLETE;
}
