/*
 * history.h
 * Copyright (C) 2024 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "line.h"

typedef struct {
    GPUCell *gpu_cells;
    CPUCell *cpu_cells;
    LineAttrs *line_attrs;
    void *mem;
    size_t mmap_size;
} HistoryBufSegment;

typedef struct {
    void *ringbuf;
    size_t maximum_size;
    bool rewrap_needed;
} PagerHistoryBuf;


typedef struct {
    PyObject_HEAD

        index_type xnum,
        ynum, num_segments;
    HistoryBufSegment *segments;
    PagerHistoryBuf *pagerhist;
    Line *line;
    TextCache *text_cache;
    index_type start_of_data, count;
    // Absolute line number of the oldest line held: the number of lines that have left the
    // top of the ring (evicted or cleared). Line i from the oldest is abs_oldest_line + i, and
    // the screen continues the numbering. It never decreases, and a line keeps its number
    // through scrolling and eviction; a rewrap or historybuf_delete_newest_lines renumbers the
    // lines below what changed. A number names a position, not content: a reset or a redraw
    // writes new text at the same number.
    uint64_t abs_oldest_line;
    // Whether the line before abs_oldest_line, one that left the top of the ring, wraps onto
    // it: the oldest line then starts mid-line. Eviction sets it from the line it drops; a clear,
    // and a delete of the oldest line, leave the oldest line starting a line.
    bool oldest_line_continues_evicted;
    // The lowest absolute line this ring's own operations have renumbered or rewritten since
    // find last read it (pop, delete_newest_lines, rewrap), UINT64_MAX for none. Pushes,
    // eviction and clear move only abs_oldest_line and oldest_line_continues_evicted. Find resets it each time it records the
    // buffer it fed (kitty/screen.c, record_fed_buffer).
    uint64_t abs_lowest_rewritten_line;
} HistoryBuf;


HistoryBuf *alloc_historybuf(unsigned int, unsigned int, unsigned int, TextCache *tc);
HistoryBuf *historybuf_alloc_for_rewrap(unsigned int columns, HistoryBuf *self);
void historybuf_finish_rewrap(HistoryBuf *dest, HistoryBuf *src);
void historybuf_fast_rewrap(HistoryBuf *dest, HistoryBuf *src, index_type count, bool steal);
index_type historybuf_next_dest_line(HistoryBuf *self, ANSIBuf *as_ansi_buf, Line *src_line, index_type dest_y, Line *dest_line, bool continued);
bool historybuf_is_line_continued(HistoryBuf *self, index_type lnum);
void historybuf_delete_newest_lines(HistoryBuf *self, index_type count);
