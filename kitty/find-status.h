/*
 * find-status.h
 * Where the find engine's search stands, as kitty_rs_find_status reports it. Apart from
 * kitty_rs.h so that screen.h, which kitty_rs.h includes by way of find.h, can hold one.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

// Whether the engine is searching for a query. REJECTED holds a query that cannot be searched
// for, such as a regular expression that does not parse, and it has no matches.
typedef enum { FIND_QUERY_EMPTY, FIND_QUERY_SEARCHING, FIND_QUERY_STATE_REJECTED } FindQueryState;

// current is 1-based among the matches, 0 when there is no current match; wrapped says the
// step that reached it went past an end; unsearched counts the buffer's lines not yet searched,
// 0 once all are and for the empty and rejected queries.
typedef struct {
    size_t current, total;
    bool wrapped;
    uint64_t unsearched;
    FindQueryState query;
} FindStatus;
