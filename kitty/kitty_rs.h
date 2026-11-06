/*
 * kitty_rs.h
 * The C ABI of the Rust crate in rust/ (libkitty_rs.a, linked into
 * fast_data_types on macOS). Hand-written: each declaration here must match its
 * #[unsafe(no_mangle)] extern "C" fn in rust/src/lib.rs. Rust panics abort,
 * they never unwind into C.
 */

#pragma once

#include <stddef.h>
#include <stdint.h>

// Wiring probe, replaced by the find engine: the number of codepoints in the UTF-8
// text utf8[0..len). utf8 must be non-NULL (even when len is 0) and valid UTF-8.
size_t kitty_rs_count_codepoints(const uint8_t *utf8, size_t len);
