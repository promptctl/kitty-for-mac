// License: GPLv3 Copyright: 2023, Kovid Goyal, <kovid at kovidgoyal.net>

package tui

import (
	"fmt"

	"github.com/emmansun/base64"

	"github.com/kovidgoyal/kitty/tools/utils"
)

var _ = fmt.Print

// KittyDCS is the DCS sequence for a kitty message, as sent to a terminal that
// is not behind tmux.
func KittyDCS(msgtype, payload string) string {
	return kitty_dcs_body(msgtype, payload) + "\033\\"
}

func kitty_dcs_body(msgtype, payload string) string {
	return "\x1bP@kitty-" + msgtype + "|" + base64.StdEncoding.EncodeToString(utils.UnsafeStringToBytes(payload))
}

func DCSToKitty(msgtype, payload string) (string, error) {
	if TmuxSocketAddress() == "" {
		return KittyDCS(msgtype, payload), nil
	}
	if err := TmuxAllowPassthrough(); err != nil {
		return "", err
	}
	return "\033Ptmux;\033" + kitty_dcs_body(msgtype, payload) + "\033\033\\\033\\", nil
}
