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

// The main buffer by absolute line: history oldest first, then the screen rows.
static uint64_t
buffer_limit(const Screen *self) {
    return self->historybuf->abs_oldest_line + self->historybuf->count + self->main_linebuf->ynum;
}

static CPUCell*
cells_at(Screen *self, uint64_t abs_line) {
    HistoryBuf *hb = self->historybuf;
    const uint64_t i = abs_line - hb->abs_oldest_line;
    // historybuf numbers its lines newest first
    return i < hb->count ? historybuf_cpu_cells(hb, hb->count - 1 - i) : linebuf_cpu_cells_for_line(self->main_linebuf, i - hb->count);
}

// whether abs_line's last cell wraps onto a next line that is in the buffer
static bool
continues(Screen *self, uint64_t abs_line) {
    return abs_line + 1 < buffer_limit(self) && cells_at(self, abs_line)[self->main_linebuf->xnum - 1].next_char_was_wrapped;
}

FindRefeedRange
find_onward_range(Screen *self, uint64_t abs_line) {
    uint64_t from = abs_line;
    while (from > self->historybuf->abs_oldest_line && continues(self, from - 1)) from--;
    return (FindRefeedRange){.from = from, .limit = UINT64_MAX};
}

FindRefeedRange
find_logical_line_range(Screen *self, uint64_t abs_line) {
    uint64_t last = abs_line;
    while (continues(self, last)) last++;
    return (FindRefeedRange){.from = find_onward_range(self, abs_line).from, .limit = last + 1};
}

FindBufferBounds
find_buffer_bounds(const Screen *self) {
    return (FindBufferBounds){.oldest = self->historybuf->abs_oldest_line, .end = buffer_limit(self)};
}

FindWalkResult
find_walk_main_buffer(Screen *self, FindRefeedRange lines, FindLineSink sink, void *ctx) {
    HistoryBuf *hb = self->historybuf;
    LineBuf *lb = self->main_linebuf;
    const index_type num_lines = hb->count + lb->ynum;
    __attribute__((cleanup(free_find_line_buf))) FindLineBuf b = {0};
    // allocated up front so even an empty first line hands the sink non-NULL pointers
    if (!reserve(&b, 4 * lb->xnum)) return FIND_WALK_OUT_OF_MEMORY;
    // [LAW:single-enforcer] the range already starts a logical line and ends one or runs onward:
    // the walk feeds exactly its lines, so the refeed it fills covers what its splice replaces
    const index_type start = lines.from - hb->abs_oldest_line;
    const index_type end = MIN((uint64_t)num_lines, lines.limit - hb->abs_oldest_line);
    index_type first = start;
    for (index_type i = start; i < end; i++) {
        Line *line;
        if (i < hb->count) {
            historybuf_init_line(hb, hb->count - 1 - i, hb->line);
            line = hb->line;
        } else {
            linebuf_init_line(lb, i - hb->count);
            line = lb->line;
        }
        if (!append_physical_line(&b, line, i - first)) return FIND_WALK_OUT_OF_MEMORY;
        if (!continues(self, hb->abs_oldest_line + i)) {
            const FindLineStart line_start = first == 0 && hb->oldest_line_continues_evicted ? FIND_LINE_CONTINUES_EVICTED : FIND_LINE_STARTS_LINE;
            const FindLogicalLine l = {.abs_first_line = hb->abs_oldest_line + first, .start = line_start, .rows = i - first + 1, .text = b.text, .text_len = b.len, .cells = b.cells};
            if (!sink(ctx, &l)) return FIND_WALK_SINK_STOPPED;
            b.len = 0;
            first = i + 1;
        }
    }
    return FIND_WALK_COMPLETE;
}

FindFedRecord*
find_record_capture(Screen *self) {
    HistoryBuf *hb = self->historybuf;
    const index_type columns = self->main_linebuf->xnum;
    const uint64_t first_line = hb->abs_oldest_line + hb->count - MIN(hb->count, FIND_MULTICELL_REACH);
    const index_type rows = buffer_limit(self) - first_line;
    FindFedRecord *r = malloc(sizeof(FindFedRecord));
    if (!r) return NULL;
    *r = (FindFedRecord){.first_line = first_line, .abs_oldest_line = hb->abs_oldest_line, .oldest_line_continues_evicted = hb->oldest_line_continues_evicted, .rows = rows, .columns = columns};
    r->cells = malloc((size_t)rows * columns * sizeof(CPUCell));
    if (!r->cells) { free(r); return NULL; }
    for (index_type y = 0; y < rows; y++) memcpy(r->cells + (size_t)y * columns, cells_at(self, first_line + y), columns * sizeof(CPUCell));
    return r;
}

void
find_record_free(FindFedRecord *r) {
    if (r) free(r->cells);
    free(r);
}

uint64_t
find_changed_from(Screen *self, const FindFedRecord *r) {
    HistoryBuf *hb = self->historybuf;
    const uint64_t oldest = hb->abs_oldest_line, limit = buffer_limit(self), recorded_limit = r->first_line + r->rows;
    const index_type columns = self->main_linebuf->xnum;
    // the ring's own rewrites, widened by the multicell rows an edit could have reached above
    // them once a pop or delete moved screen row 0 down
    const uint64_t mark = hb->abs_lowest_rewritten_line;
    uint64_t changed = mark == UINT64_MAX ? UINT64_MAX : mark - MIN(mark, FIND_MULTICELL_REACH);
    // a recorded row differs when its line has left the end of the buffer or holds other
    // cells, as every row does at another column count
    for (uint64_t l = MAX(r->first_line, oldest); l < MIN(recorded_limit, changed); l++) {
        const CPUCell *was = r->cells + (size_t)(l - r->first_line) * r->columns;
        if (l >= limit || r->columns != columns || memcmp(cells_at(self, l), was, columns * sizeof(CPUCell)) != 0) {
            changed = l;
            break;
        }
    }
    // lines past the last recorded row are new
    if (limit > recorded_limit) changed = MIN(changed, recorded_limit);
    // [LAW:single-enforcer] the one clamp: the walker and the refeed only see lines in the buffer
    return changed == UINT64_MAX ? changed : MAX(changed, oldest);
}
