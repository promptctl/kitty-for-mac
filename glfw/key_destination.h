/*
 * key_destination.h
 * Where each held key's press went, so that its repeats and its release reach
 * the same place whatever focus or the key window does in between.
 *
 * Pure C with no AppKit, so kitty's test suite can drive it
 * (kitty/keys.c). The key codes and modifier masks are
 * the macOS virtual key codes and NX_DEVICE* masks; glfw/cocoa_init.m checks
 * them against the system headers.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

typedef enum { KEY_DEST_NONE, KEY_DEST_TERMINAL, KEY_DEST_APPKIT, KEY_DEST_CONSUMED, KEY_DEST_SHORTCUT } KeyDestinationKind;

// TERMINAL: id is the windowNumber of the terminal's window. APPKIT: id is 0;
// every native responder is one destination, so a key whose press AppKit took
// keeps going to AppKit, which routes it as it does in any Cocoa app (a held Tab
// keeps moving focus). CONSUMED: a menu or macOS shortcut took the press, so its
// repeats and its release go nowhere. SHORTCUT: id is the windowNumber of the
// terminal window whose kitty shortcut took a press bound for its native view;
// its repeats go to AppKit like any native view's, where the view's unhandled
// keys reach the shortcuts again, and its release goes to that window's
// shortcut release, as a shortcut's release does in the terminal. NONE: nowhere
// to send it (no key window), or, in the table, not held.
typedef struct { KeyDestinationKind kind; uintptr_t id; } KeyDestination;

#define KEY_DESTINATION_TABLE_SIZE 128
typedef struct { KeyDestination held[KEY_DESTINATION_TABLE_SIZE]; } KeyDestinationTable;

static const KeyDestination KEY_DEST_NOT_HELD = {KEY_DEST_NONE, 0};

// The modifier keys that are held, each with the device-dependent flag that is
// set while it is down, so the two keys of a pair are told apart, the flag of
// the other key of its pair, and the device-independent flag of the pair.
// CapsLock is absent: each of its flagsChanged events is a whole toggle, not a
// press or a release, so it is never held.
static const struct { unsigned short vk; uint64_t down_flag, other_flag, either_flag; } key_destination_held_modifiers[] = {
    {0x38 /* kVK_Shift */, 0x00000002 /* NX_DEVICELSHIFTKEYMASK */, 0x00000004, 0x00020000 /* NX_SHIFTMASK */},
    {0x3C /* kVK_RightShift */, 0x00000004 /* NX_DEVICERSHIFTKEYMASK */, 0x00000002, 0x00020000},
    {0x3B /* kVK_Control */, 0x00000001 /* NX_DEVICELCTLKEYMASK */, 0x00002000, 0x00040000 /* NX_CONTROLMASK */},
    {0x3E /* kVK_RightControl */, 0x00002000 /* NX_DEVICERCTLKEYMASK */, 0x00000001, 0x00040000},
    {0x3A /* kVK_Option */, 0x00000020 /* NX_DEVICELALTKEYMASK */, 0x00000040, 0x00080000 /* NX_ALTERNATEMASK */},
    {0x3D /* kVK_RightOption */, 0x00000040 /* NX_DEVICERALTKEYMASK */, 0x00000020, 0x00080000},
    {0x37 /* kVK_Command */, 0x00000008 /* NX_DEVICELCMDKEYMASK */, 0x00000010, 0x00100000 /* NX_COMMANDMASK */},
    {0x36 /* kVK_RightCommand */, 0x00000010 /* NX_DEVICERCMDKEYMASK */, 0x00000008, 0x00100000},
    {0x3F /* kVK_Function */, 0x00800000 /* NSEventModifierFlagFunction */, 0, 0x00800000},
};
#define KEY_DESTINATION_HELD_MODIFIER_COUNT (sizeof(key_destination_held_modifiers) / sizeof(key_destination_held_modifiers[0]))

static inline bool
key_destination_in_table(unsigned short vk) { return vk < KEY_DESTINATION_TABLE_SIZE; }

// A press is always a new hold: it replaces any record whose release was never
// seen (it went to another app after Cmd+Tab, or was handed to AppKit early).
// A repeat follows its press, or starts a hold if the press was not seen; a
// shortcut's hold sends its repeats to AppKit and keeps its release.
static inline KeyDestination
key_destination_for_key_down(KeyDestinationTable *t, unsigned short vk, bool is_repeat, KeyDestination current) {
    if (!key_destination_in_table(vk)) return current;
    if (!is_repeat || t->held[vk].kind == KEY_DEST_NONE) t->held[vk] = current;
    return t->held[vk].kind == KEY_DEST_SHORTCUT ? (KeyDestination){KEY_DEST_APPKIT, 0} : t->held[vk];
}

// A shortcut that took a press replaces the key's hold; a repeat that a
// shortcut took leaves the hold to the release.
static inline void
key_destination_take_press(KeyDestinationTable *t, unsigned short vk, bool is_repeat, KeyDestination by) {
    if (!is_repeat && key_destination_in_table(vk)) t->held[vk] = by;
}

// A menu or macOS shortcut took the press: its repeats and release are dropped.
static inline void
key_destination_consume(KeyDestinationTable *t, unsigned short vk, bool is_repeat) {
    key_destination_take_press(t, vk, is_repeat, (KeyDestination){KEY_DEST_CONSUMED, 0});
}

// A kitty shortcut of the terminal window window_number took a press bound for
// its native view. It replaces the APPKIT hold the monitor recorded, since such
// a press reaches the shortcuts only after AppKit was given it.
static inline void
key_destination_take_for_shortcut(KeyDestinationTable *t, unsigned short vk, bool is_repeat, uintptr_t window_number) {
    key_destination_take_press(t, vk, is_repeat, (KeyDestination){KEY_DEST_SHORTCUT, window_number});
}

// A release goes where its press went and ends the hold; an unseen press's
// release goes where focus is now.
static inline KeyDestination
key_destination_for_key_up(KeyDestinationTable *t, unsigned short vk, KeyDestination current) {
    if (!key_destination_in_table(vk)) return current;
    const KeyDestination dest = t->held[vk].kind == KEY_DEST_NONE ? current : t->held[vk];
    t->held[vk] = KEY_DEST_NOT_HELD;
    return dest;
}

// Whether modifier i of the table is down in flags; the terminal's
// flagsChanged (glfw/cocoa_window.m) reads modifiers with it too. When the
// device-dependent flags disagree with the device-independent one, as in
// events posted by remapping tools, the device-independent one wins.
static inline bool
key_destination_modifier_down(uint64_t flags, unsigned i) {
    const bool target = (flags & key_destination_held_modifiers[i].down_flag) != 0;
    const bool other = (flags & key_destination_held_modifiers[i].other_flag) != 0;
    const bool either = (flags & key_destination_held_modifiers[i].either_flag) != 0;
    return either != (target || other) ? either : target;
}

// The index of a held modifier's key code in the table, or -1.
static inline int
key_destination_modifier_index(unsigned short vk) {
    for (unsigned i = 0; i < KEY_DESTINATION_HELD_MODIFIER_COUNT; i++) if (key_destination_held_modifiers[i].vk == vk) return (int)i;
    return -1;
}

// flags is the event's modifierFlags, device-dependent bits included. Every
// modifier whose flag is clear is released, whichever key this event is for,
// so a release kitty never saw cannot leave a hold behind.
static inline KeyDestination
key_destination_for_flags_changed(KeyDestinationTable *t, unsigned short vk, uint64_t flags, KeyDestination current) {
    KeyDestination dest = current;
    for (unsigned i = 0; i < KEY_DESTINATION_HELD_MODIFIER_COUNT; i++) {
        const unsigned short m = key_destination_held_modifiers[i].vk;
        const bool down = key_destination_modifier_down(flags, i);
        if (m == vk) dest = down ? key_destination_for_key_down(t, m, false, current) : key_destination_for_key_up(t, m, current);
        else if (!down) t->held[m] = KEY_DEST_NOT_HELD;
    }
    return dest;
}
