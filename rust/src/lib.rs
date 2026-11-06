//! kitty's Rust code, linked into the fast_data_types extension as a static library.
//! Every export is an `extern "C"` function declared in kitty/kitty_rs.h, which is the
//! C side's only view of this crate.

mod find;

use find::{FindCellSpan, FindEngine, FindMatch};

/// kitty/find.h's FindLogicalLine: the walker's view of one logical line.
#[repr(C)]
pub struct FindLogicalLine {
    abs_first_line: u64,
    text: *const u8,
    text_len: usize,
    cells: *const FindCellSpan,
}

/// A new engine holding the empty query. Never NULL: allocation failure aborts.
#[unsafe(no_mangle)]
pub extern "C" fn kitty_rs_find_engine_new() -> *mut FindEngine {
    Box::into_raw(Box::default())
}

/// # Safety
/// `engine` came from kitty_rs_find_engine_new and is not used again.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_engine_free(engine: *mut FindEngine) {
    // SAFETY: the caller's contract above, stated again in kitty/kitty_rs.h.
    drop(unsafe { Box::from_raw(engine) });
}

/// Returns false when the query cannot be compiled, leaving the empty query and the reason
/// in `err`.
///
/// # Safety
/// `engine` is live; `utf8` points to `len` readable bytes (non-null even when `len` is 0);
/// `err` points to `err_size` >= 1 writable bytes.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_set_query(
    engine: *mut FindEngine,
    utf8: *const u8,
    len: usize,
    case_insensitive: bool,
    err: *mut u8,
    err_size: usize,
) -> bool {
    // SAFETY: the caller's contract above.
    let (engine, bytes, err) =
        unsafe { (&mut *engine, std::slice::from_raw_parts(utf8, len), std::slice::from_raw_parts_mut(err, err_size)) };
    // [LAW:no-silent-failure] invalid UTF-8 is a caller bug; the panic aborts loudly.
    let query = std::str::from_utf8(bytes).expect("kitty_rs_find_set_query: query is not UTF-8");
    engine.set_query(query, case_insensitive).map_err(|e| write_c_string(&e.to_string(), err)).is_ok()
}

/// Writes `s` into `buf` as a NUL-terminated C string, cut at a char boundary to fit.
fn write_c_string(s: &str, buf: &mut [u8]) {
    let mut n = s.len().min(buf.len() - 1);
    while !s.is_char_boundary(n) {
        n -= 1;
    }
    buf[..n].copy_from_slice(&s.as_bytes()[..n]);
    buf[n] = 0;
}

/// # Safety
/// `engine` is live; `line` points to a FindLogicalLine whose text and cells each hold
/// text_len entries (non-null even when text_len is 0).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_feed_line(engine: *mut FindEngine, line: *const FindLogicalLine) {
    // SAFETY: the caller's contract above.
    let (engine, l) = unsafe { (&mut *engine, &*line) };
    let (text, cells) = unsafe { (std::slice::from_raw_parts(l.text, l.text_len), std::slice::from_raw_parts(l.cells, l.text_len)) };
    engine.feed_line(l.abs_first_line, text, cells);
}

/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_drop_lines_from(engine: *mut FindEngine, abs_line: u64) {
    // SAFETY: the caller's contract above.
    unsafe { &mut *engine }.drop_lines_from(abs_line);
}

/// # Safety
/// `engine` is live.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_evict_before(engine: *mut FindEngine, abs_line: u64) {
    // SAFETY: the caller's contract above.
    unsafe { &mut *engine }.evict_before(abs_line);
}

/// The matches in buffer order, `*count` of them, valid until the engine next changes.
///
/// # Safety
/// `engine` is live and `count` is writable.
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_find_matches(engine: *const FindEngine, count: *mut usize) -> *const FindMatch {
    // SAFETY: the caller's contract above.
    let matches = unsafe { &*engine }.matches();
    unsafe { *count = matches.len() };
    matches.as_ptr()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_search_crosses_the_abi() {
        let engine = kitty_rs_find_engine_new();
        let query = "é";
        let text = "aÉ";
        let span = |x| FindCellSpan { row: 0, x, x_last: x };
        let cells = [span(0), span(1), span(1)];
        let line = FindLogicalLine { abs_first_line: 5, text: text.as_ptr(), text_len: text.len(), cells: cells.as_ptr() };
        let mut count = 0;
        let found = unsafe {
            let mut err = [0u8; 8];
            assert!(kitty_rs_find_set_query(engine, query.as_ptr(), query.len(), true, err.as_mut_ptr(), err.len()));
            kitty_rs_find_feed_line(engine, &line);
            std::slice::from_raw_parts(kitty_rs_find_matches(engine, &mut count), count).to_vec()
        };
        assert_eq!(found, [FindMatch { abs_start_line: 5, start_x: 1, abs_end_line: 5, end_x: 1 }]);
        unsafe { kitty_rs_find_engine_free(engine) };
    }

    #[test]
    fn a_query_that_cannot_compile_reports_why() {
        let engine = kitty_rs_find_engine_new();
        // past the regex crate's default compiled-size limit
        let query = "é".repeat(1 << 17);
        let mut err = [0xffu8; 256];
        let ok = unsafe { kitty_rs_find_set_query(engine, query.as_ptr(), query.len(), true, err.as_mut_ptr(), err.len()) };
        assert!(!ok);
        let reason = std::ffi::CStr::from_bytes_until_nul(&err).unwrap().to_str().unwrap();
        assert!(reason.contains("size limit"), "{reason}");
        unsafe { kitty_rs_find_engine_free(engine) };
    }

    #[test]
    fn write_c_string_cuts_at_a_char_boundary() {
        let mut buf = [0xffu8; 4];
        write_c_string("aéb", &mut buf);
        assert_eq!(buf, *b"a\xc3\xa9\0");
        // two bytes of room would split é, so only "a" fits
        let mut buf = [0xffu8; 3];
        write_c_string("aéb", &mut buf);
        assert_eq!(buf[..2], *b"a\0");
    }
}
