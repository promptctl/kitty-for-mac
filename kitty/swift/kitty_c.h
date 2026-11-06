/*
 * kitty_c.h
 * The C functions kitty's Swift module calls, imported into Swift as the KittyC module.
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

// Only system headers here: setup.py rebuilds the Swift module when a file in kitty/swift changes,
// so a kitty header included from here would be an input it cannot see.
// The definitions live in kitty's Objective-C, which includes this header so the compiler checks
// each one against the prototype Swift imports.

// The C end of the Objective-C -> Swift -> C wiring proof (KittySwiftWiring); the find bar replaces it.
const char* kitty_swift_wiring_c_side(void);
