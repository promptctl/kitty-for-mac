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

typedef enum { KEY_DEST_NONE, KEY_DEST_TERMINAL, KEY_DEST_APPKIT } KeyDestinationKind;

// TERMINAL: id is the windowNumber of the terminal's window. APPKIT: id is 0;
// every native responder is one destination, so a key whose press AppKit took
// keeps going to AppKit, which routes it as it does in any Cocoa app (a held Tab
// keeps moving focus). NONE: nowhere to send it (no key window), or, in the
// table, not held.
typedef struct { KeyDestinationKind kind; uintptr_t id; } KeyDestination;

typedef struct { KeyDestination held[128]; } KeyDestinationTable;

static const KeyDestination KEY_DEST_NOT_HELD = {KEY_DEST_NONE, 0};

// The modifier keys that are held, each with the device-dependent flag that is
// set while it is down, so the two keys of a pair are told apart. CapsLock is
// absent: each of its flagsChanged events is a whole toggle, not a press or a
// release, so it is never held.
static const struct { unsigned short vk; uint64_t down_flag; } key_destination_held_modifiers[] = {
    {0x38 /* kVK_Shift */, 0x00000002 /* NX_DEVICELSHIFTKEYMASK */},
    {0x3C /* kVK_RightShift */, 0x00000004 /* NX_DEVICERSHIFTKEYMASK */},
    {0x3B /* kVK_Control */, 0x00000001 /* NX_DEVICELCTLKEYMASK */},
    {0x3E /* kVK_RightControl */, 0x00002000 /* NX_DEVICERCTLKEYMASK */},
    {0x3A /* kVK_Option */, 0x00000020 /* NX_DEVICELALTKEYMASK */},
    {0x3D /* kVK_RightOption */, 0x00000040 /* NX_DEVICERALTKEYMASK */},
    {0x37 /* kVK_Command */, 0x00000008 /* NX_DEVICELCMDKEYMASK */},
    {0x36 /* kVK_RightCommand */, 0x00000010 /* NX_DEVICERCMDKEYMASK */},
    {0x3F /* kVK_Function */, 0x00800000 /* NSEventModifierFlagFunction */},
};
#define KEY_DESTINATION_HELD_MODIFIER_COUNT (sizeof(key_destination_held_modifiers) / sizeof(key_destination_held_modifiers[0]))

static inline bool
key_destination_in_table(unsigned short vk) { return vk < sizeof(((KeyDestinationTable*)0)->held) / sizeof(KeyDestination); }

// A press is always a new hold: it replaces any record whose release was never
// seen (it went to another app after Cmd+Tab, or was handed to AppKit early).
// A repeat follows its press, or starts a hold if the press was not seen.
static inline KeyDestination
key_destination_for_key_down(KeyDestinationTable *t, unsigned short vk, bool is_repeat, KeyDestination current) {
    if (!key_destination_in_table(vk)) return current;
    if (!is_repeat || t->held[vk].kind == KEY_DEST_NONE) t->held[vk] = current;
    return t->held[vk];
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

// flags is the event's modifierFlags, device-dependent bits included. Every
// modifier whose flag is clear is released, whichever key this event is for,
// so a release kitty never saw cannot leave a hold behind.
static inline KeyDestination
key_destination_for_flags_changed(KeyDestinationTable *t, unsigned short vk, uint64_t flags, KeyDestination current) {
    KeyDestination dest = current;
    for (unsigned i = 0; i < KEY_DESTINATION_HELD_MODIFIER_COUNT; i++) {
        const unsigned short m = key_destination_held_modifiers[i].vk;
        const bool down = (flags & key_destination_held_modifiers[i].down_flag) != 0;
        if (m == vk) dest = down ? key_destination_for_key_down(t, m, false, current) : key_destination_for_key_up(t, m, current);
        else if (!down) t->held[m] = KEY_DEST_NOT_HELD;
    }
    return dest;
}
