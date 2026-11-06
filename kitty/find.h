/*
 * find.h
 * Distributed under terms of the GPL3 license.
 */

#pragma once

#include "screen.h"
#include "kitty_rs.h"

// Receives each logical line in buffer order; returning false stops the walk.
typedef bool (*FindLineSink)(void *ctx, const FindLogicalLine *line);

typedef enum { FIND_WALK_COMPLETE, FIND_WALK_SINK_STOPPED, FIND_WALK_OUT_OF_MEMORY } FindWalkResult;

// Feeds every logical line of the main buffer, oldest history line through the last screen
// row, to sink. Sets no Python error: the caller turns FIND_WALK_OUT_OF_MEMORY into one.
FindWalkResult find_walk_main_buffer(Screen *self, FindLineSink sink, void *ctx);
