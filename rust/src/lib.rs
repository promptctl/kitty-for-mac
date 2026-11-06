//! kitty's Rust code, linked into the fast_data_types extension as a static library.
//! Every export is an `extern "C"` function declared in kitty/kitty_rs.h, which is the
//! C side's only view of this crate.

/// Number of Unicode codepoints in `text`.
fn codepoint_count(text: &str) -> usize {
    text.chars().count()
}

/// Wiring probe: proves a pointer + length crosses the C ABI and an answer comes back.
/// The find engine's exports replace it.
///
/// # Safety
/// `utf8` must point to `len` readable bytes (non-null even when `len` is 0).
#[unsafe(no_mangle)]
pub unsafe extern "C" fn kitty_rs_count_codepoints(utf8: *const u8, len: usize) -> usize {
    // SAFETY: the caller's contract above, stated again in kitty/kitty_rs.h.
    let bytes = unsafe { std::slice::from_raw_parts(utf8, len) };
    // [LAW:no-silent-failure] invalid UTF-8 is a caller bug; the panic aborts loudly.
    let text = std::str::from_utf8(bytes).expect("kitty_rs_count_codepoints: input is not UTF-8");
    codepoint_count(text)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn count_via_abi(text: &str) -> usize {
        unsafe { kitty_rs_count_codepoints(text.as_ptr(), text.len()) }
    }

    #[test]
    fn counts_ascii() {
        assert_eq!(count_via_abi("kitty"), 5);
    }

    #[test]
    fn counts_multibyte_codepoints_not_bytes() {
        assert_eq!(count_via_abi("héllo, 世界 🐱"), 11);
    }

    #[test]
    fn counts_empty() {
        assert_eq!(count_via_abi(""), 0);
    }
}
