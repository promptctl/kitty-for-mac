/*
 * callback_kinds.h
 *
 * Distributed under terms of the GPL3 license.
 */

#pragma once

// [LAW:single-enforcer] Every callback GLFW stores for its client is one of two kinds, chosen at
// its declaration (kitty_tests/glfw.py fails on a callback declared as neither). An event tells the
// client something happened; the client changes state in response, so delivering one requests a
// tick of the client's main loop (see request_tick_callback in kitty/state.h). An event can only be
// invoked through _glfwDeliver: calling it directly does not compile. A _GLFW_NO_TICK callback
// (glfw3.h) is called directly and requests nothing.
#define _GLFW_EVENT(fun_type) struct { fun_type fn; }
// The request comes before the callback runs: it only marks a tick as due (or due again, inside the
// tick), so the order makes no difference, and it lets an event that answers stay an expression.
#define _glfwDeliver(event, ...) ((event).fn ? (void)(_glfwPlatformPostEmptyEvent(), (event).fn(__VA_ARGS__)) : (void)0)
// An event that answers: the client's result, or fallback when no callback is set.
#define _glfwDeliverResult(event, fallback, ...) ((event).fn ? (_glfwPlatformPostEmptyEvent(), (event).fn(__VA_ARGS__)) : (fallback))
void _glfwPlatformPostEmptyEvent(void);

typedef unsigned long long id_type;
typedef void (*timer_callback_func)(id_type, void *);
typedef void (*GLFWuserdatafreefun)(id_type, void *);

// A timer the client added with glfwAddTimer is an event; GLFW's own timers (key repeat, cursor
// animation, D-Bus timeouts, ...) are not, since the ones that change client state do it through
// an event of their own. The timer's free hook only releases data and never ticks.
typedef enum { INTERNAL_TIMER, CLIENT_TIMER } TimerKind;
typedef struct {
    TimerKind kind;
    union {
        timer_callback_func internal;
        _GLFW_EVENT(timer_callback_func) client;
    };
} TimerFire;

static inline void
fire_timer(TimerFire fire, id_type id, void *data) {
    switch (fire.kind) {
        case INTERNAL_TIMER: fire.internal(id, data); break;
        case CLIENT_TIMER: _glfwDeliver(fire.client, id, data); break;
    }
}
