/*
 * keys.h
 * Copyright (C) 2021 Kovid Goyal <kovid at kovidgoyal.net>
 *
 * Distributed under terms of the GPL3 license.
 */


#pragma once
#include "data-types.h"
#include "glfw-wrapper.h"
#include <limits.h>

#define KEY_BUFFER_SIZE 128
#define SEND_TEXT_TO_CHILD INT_MIN
#define debug debug_input

// A key event's whole escape: in inline_bytes, or on the heap when embedded text makes it longer than KEY_BUFFER_SIZE
typedef struct { char inline_bytes[KEY_BUFFER_SIZE]; char *heap; } EncodedKey;
static inline const char* encoded_key_bytes(const EncodedKey *k) { return k->heap ? k->heap : k->inline_bytes; }
static inline void encoded_key_release(EncodedKey *k) { free(k->heap); k->heap = NULL; }
#define RAII_ENCODED_KEY(name) __attribute__((cleanup(encoded_key_release))) EncodedKey name = {0}

int encode_glfw_key_event(const GLFWkeyevent *e, const bool cursor_key_mode, const unsigned flags, EncodedKey *output);

bool is_modifier_key(const uint32_t key);
