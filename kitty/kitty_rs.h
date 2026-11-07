/*
 * kitty_rs.h
 * The C ABI of the Rust crate in rust/ (libkitty_rs.a, linked into
 * fast_data_types on macOS). Hand-written: each declaration here must match its
 * #[unsafe(no_mangle)] extern "C" fn in rust/src/lib.rs, and each struct it uses
 * (here or from the walker's find.h) its #[repr(C)] twin in rust/src. Rust panics abort, they never unwind into C.
 */

#pragma once

#include "find.h"
#include "find-status.h"

// A match from (abs_start_line, start_x) through (abs_end_line, end_x), both cells included.
typedef struct FindMatch {
    uint64_t abs_start_line;
    uint32_t start_x;
    uint64_t abs_end_line;
    uint32_t end_x;
} FindMatch;

// The find engine: a query, the lines searched for it so far, and every match found in them,
// kept sorted in buffer order. The engine keeps no fact about the buffer: the calls that need
// its bounds are handed them.
typedef struct FindEngine FindEngine;

// Never NULL: allocation failure aborts. The new engine holds the empty query.
FindEngine* kitty_rs_find_engine_new(void);
void kitty_rs_find_engine_free(FindEngine *engine);
// How a query matches. SMART ignores case unless the query has an uppercase letter of its own.
// The values are Python's FindCaseRule and FindSyntax.
typedef enum { FIND_CASE_SMART, FIND_CASE_IGNORE, FIND_CASE_MATCH } FindCaseRule;
typedef enum { FIND_SYNTAX_LITERAL, FIND_SYNTAX_REGEX } FindSyntax;
typedef struct { FindCaseRule case_rule; FindSyntax syntax; } FindMode;
typedef enum { FIND_QUERY_UNCHANGED, FIND_QUERY_CHANGED, FIND_QUERY_REJECTED } FindQueryChange;
// Sets the query; "" is the query that finds nothing, in every mode. UNCHANGED when the query and
// mode are the ones already set, touching nothing. CHANGED drops every match and searched line.
// REJECTED does the same and holds a query that cannot be searched for, such as a regular
// expression that does not parse: it has no matches. utf8 must be non-NULL (even when len is 0)
// and valid UTF-8.
FindQueryChange kitty_rs_find_set_query(FindEngine *engine, const uint8_t *utf8, size_t len, FindMode mode);
// Sets the query already set again, in mode; the empty query stays empty.
FindQueryChange kitty_rs_find_set_mode(FindEngine *engine, FindMode mode);
// Writes the regular expression that matches the UTF-8 text literally to out, which has room for
// 2 * len bytes, and returns its length.
size_t kitty_rs_find_escape(const uint8_t *utf8, size_t len, uint8_t *out);
// The matches in a range of lines searched under the engine's query, spliced in whole.
typedef struct FindRefeed FindRefeed;
// Never NULL: allocation failure aborts.
FindRefeed* kitty_rs_find_refeed_new(const FindEngine *engine, FindRefeedRange lines);
// Adds the matches in one logical line; lines must lie in the range and arrive in buffer order.
void kitty_rs_find_refeed_line(FindRefeed *refeed, const FindLogicalLine *line);
// Replaces the matches starting in the refeed's lines, which must all be searched, with its
// own, and frees the refeed. The generation moves only when they differ.
void kitty_rs_find_splice(FindEngine *engine, FindRefeed *refeed);
// Adds the matches of the lines the refeed walked, which must all be unsearched, marks them
// searched, and frees the refeed.
void kitty_rs_find_add_searched(FindEngine *engine, FindRefeed *refeed);
// Makes every line from `from` on unsearched, then marks the lines the onward refeed walked
// (from `from` or past it) searched, its matches replacing those dropped, and frees it. The
// generation moves only when the matches differ.
void kitty_rs_find_refresh(FindEngine *engine, uint64_t from, FindRefeed *refeed);
// Frees a refeed without splicing it.
void kitty_rs_find_refeed_free(FindRefeed *refeed);
// Drops the matches and searched lines before oldest. True when line oldest is searched, so a
// logical line the eviction cut holds matches that must be re-fed (kitty_rs_find_splice).
bool kitty_rs_find_evict_before(FindEngine *engine, uint64_t oldest);
// Handed n matches in buffer order, valid only during the call.
typedef void (*FindMatchVisitor)(void *ctx, const FindMatch *run, size_t n);
// Calls visit with the matches covering any of the lines [first, limit), in buffer order, a
// run at a time, before it returns. visit must not use the engine.
void kitty_rs_find_each_match_in_lines(const FindEngine *engine, uint64_t first, uint64_t limit, FindMatchVisitor visit, void *ctx);
// How many matches cover any of the lines [first, limit).
size_t kitty_rs_find_count_in_lines(const FindEngine *engine, uint64_t first, uint64_t limit);
// Counts the changes to the match list: equal generations mean the same matches.
uint64_t kitty_rs_find_generation(const FindEngine *engine);
// The current match, always one of the matches: set_query and the cuts that drop it end it.
// Makes m the current match, returning false and changing nothing when it is not one of the
// matches. Setting the current match again keeps whether the step to it wrapped.
bool kitty_rs_find_set_current(FindEngine *engine, FindMatch m);
// Writes the current match to *out; false, writing nothing, when there is none.
bool kitty_rs_find_current(const FindEngine *engine, FindMatch *out);
// Leaves no current match; the matches stay.
void kitty_rs_find_end_current(FindEngine *engine);
// A cell position, ordered as a match's start is: by line, then column.
typedef struct {
    uint64_t line;
    uint32_t x;
} FindPos;
// An unsearched line a waiting step's answer needs, and which way to search from it: forward
// from it, or backward through it.
typedef struct {
    uint64_t line;
    bool forward;
} FindWant;
// The unsearched lines [from, limit) a fill walks next: forward from `from`, or backward from
// `limit`.
typedef struct {
    uint64_t from, limit;
    bool forward;
} FindFill;
// Where the fill searches next: toward *want while its line is unsearched (want may be NULL),
// else the unsearched lines nearest the buffer's end, backward. False once nothing is left to
// search, and always for the empty query.
bool kitty_rs_find_next_fill(const FindEngine *engine, FindBufferBounds bounds, const FindWant *want, FindFill *out);
// Where a step starts: after after_match when `after`, as from the current match, else within
// [start, end), as from the selection or the rows on view.
typedef struct {
    bool after;
    FindMatch after_match;
    FindPos start, end;
} FindOrigin;
typedef enum { FIND_STEP_LANDED, FIND_STEP_NO_MATCH, FIND_STEP_WAITING } FindStepResult;
// The match one step from origin, later unless backwards: after a match the next or previous
// one; within a range the first starting in it or after, or the last starting before its end.
// Past either end it wraps, and kitty_rs_find_status says so. The answer is the whole buffer's:
// LANDED makes it the current match and writes it to *landed; NO_MATCH means the whole buffer
// is searched and holds none; WAITING means unsearched lines could still hold the answer, writes
// the first it needs to *want, and changes nothing.
FindStepResult kitty_rs_find_step(FindEngine *engine, bool backwards, FindOrigin origin, FindBufferBounds bounds, FindMatch *landed, FindWant *want);
FindStatus kitty_rs_find_status(const FindEngine *engine, FindBufferBounds bounds);
