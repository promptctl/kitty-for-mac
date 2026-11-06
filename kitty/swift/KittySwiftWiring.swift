// Proves kitty's Swift module is wired both ways: Objective-C calls this AppKit code, and it calls
// back into kitty's C through KittyC. The find bar replaces it.

import AppKit
import KittyC

@MainActor
@objc(KittySwiftWiring)
public final class KittySwiftWiring: NSObject {
    @objc public static func roundTrip(_ message: String) -> String {
        let field = NSTextField(string: message)
        return "\(field.stringValue) -> swift -> \(String(cString: kitty_swift_wiring_c_side()))"
    }
}
