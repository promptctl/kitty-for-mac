// The find bar: a search field overlaid on the top-right of an OS window's terminal.
//
// Focus moves only through NSWindow.makeFirstResponder:, which GLFW's window class
// overrides to track which view owns focus (glfw/cocoa_window.m), so showing the bar
// takes focus and dismissing it gives focus back to the terminal, the window's content view.

import AppKit

// What the bar tells kitty: it knows a query string, a step direction, the mode a query matches
// in and an "n of m" status, and nothing of the engine. kitty's Objective-C implements this and
// sets KittyFindBar.delegate.
@MainActor
@objc public protocol KittyFindBarDelegate: AnyObject {
    func findBar(_ window: NSWindow, queryChanged text: String)
    func findBar(_ window: NSWindow, step forward: Bool)
    func findBarClosed(_ window: NSWindow)
    func findBarModeChanged(caseRule: KittyFindCaseRule, syntax: KittyFindSyntax)
    // whether the window's search is kitty's own, the one the mode applies to
    func findBarHonorsMode(_ window: NSWindow) -> Bool
    // how far down from the top of the terminal, in pixels, the bar starts: below the rows of the
    // active kitty window that a program keeps its own marks in. kitty owns the number; the bar asks.
    func findBarClearance(_ window: NSWindow) -> Int
}

// How far a search's total can be trusted. Python's FindCount has the same values.
@objc public enum KittyFindCount: Int {
    case exact = 0
    case partial = 1
    case unavailable = 2
    case pending = 3
    case rejected = 4
}

// How a query matches. Python's FindCaseRule and FindSyntax, and kitty/kitty_rs.h's, have the same values.
@objc public enum KittyFindCaseRule: Int {
    case smart = 0
    case ignore = 1
    case match = 2
}

@objc public enum KittyFindSyntax: Int {
    case literal = 0
    case regex = 1
}

@MainActor
@objc(KittyFindBar)
public final class KittyFindBar: NSView, NSSearchFieldDelegate {
    // Set once at launch, before any bar exists; unset, the first edit crashes instead of going nowhere
    @objc public static var delegate: KittyFindBarDelegate!

    private static let margin: CGFloat = 8

    private let field = NSSearchField()
    private let status = NSTextField(labelWithString: "")
    private var topConstraint: NSLayoutConstraint!

    // [LAW:dataflow-not-control-flow] The same steps whether the bar is new, hidden or already open:
    // a repeated Cmd+F lands in the field with all of its text selected. Showing a hidden bar also
    // searches its text again, since hiding the bar cleared the highlights; an open bar's match stays.
    @objc public static func show(in window: NSWindow) {
        let bar = bar(in: window)
        let wasHidden = bar.isHidden
        bar.isHidden = false
        bar.needsUpdateConstraints = true
        window.makeFirstResponder(bar.field)
        bar.field.selectText(nil)
        if wasHidden { bar.queryChanged() }
    }

    // Where the bar sits depends on kitty's geometry, which changes with no find event (a layout, a tab
    // bar, a font size, the active kitty window), so kitty says when to ask again.
    @objc public static func seat(in window: NSWindow) {
        existingBar(in: window)?.needsUpdateConstraints = true
    }

    // Python owns the query and reports it with where the search stands: the current match's place
    // among the matches, 0 when none is current. A window without a bar has nothing to show it in.
    // count says how far the total can be trusted. The values are kitty/find_backends.py FindCount's.
    @objc public static func update(in window: NSWindow, query: String, current: Int, total: Int, count: KittyFindCount) {
        guard let bar = existingBar(in: window) else { return }
        // typing in the field is what usually changed the query, and rewriting the text would reset its cursor
        if bar.field.stringValue != query { bar.field.stringValue = query }
        bar.status.stringValue = switch (count, current, total) {
        // a search that could not be made is not a search that found nothing
        case (.unavailable, _, _): "Search unavailable"
        case (.rejected, _, _): "Invalid pattern"
        case (.pending, _, _): ""
        // a search still counting has found at least total: none yet is not none
        case (.partial, 0, _): "\(total)+ matches"
        case (.partial, _, _): "\(current) of \(total)+"
        case (_, _, 0): bar.field.stringValue.isEmpty ? "" : "0 of 0"
        case (_, 0, _): "\(total) matches"
        default: "\(current) of \(total)"
        }
    }

    // [LAW:one-source-of-truth] The mode is the app's saved setting, the same in every window: the
    // defaults hold it, and a change is told to kitty, which brings every window's search in line.
    private static let caseRuleKey = "KittyFindCaseRule", syntaxKey = "KittyFindSyntax"

    // An unset key reads 0, the default mode; a value no build wrote reads as the default too
    @objc public static var caseRule: KittyFindCaseRule {
        KittyFindCaseRule(rawValue: UserDefaults.standard.integer(forKey: caseRuleKey)) ?? .smart
    }

    @objc public static var syntax: KittyFindSyntax {
        KittyFindSyntax(rawValue: UserDefaults.standard.integer(forKey: syntaxKey)) ?? .literal
    }

    @objc public static func setMode(caseRule: KittyFindCaseRule, syntax: KittyFindSyntax) {
        UserDefaults.standard.set(caseRule.rawValue, forKey: caseRuleKey)
        UserDefaults.standard.set(syntax.rawValue, forKey: syntaxKey)
        delegate.findBarModeChanged(caseRule: caseRule, syntax: syntax)
    }

    // The text in the window's field, which a hidden bar keeps for the next Cmd+F; nil for a window
    // that has never had a bar.
    @objc public static func query(in window: NSWindow) -> String? {
        existingBar(in: window)?.field.stringValue
    }

    @objc public static func isShown(in window: NSWindow) -> Bool {
        existingBar(in: window).map { !$0.isHidden } ?? false
    }

    // How far down from the top of the terminal the bar covers it, in pixels: 0 while it is hidden,
    // so a match is never revealed under it.
    @objc public static func coveredPixels(in window: NSWindow) -> Int {
        guard let terminal = window.contentView, let bar = existingBar(in: window), !bar.isHidden else { return 0 }
        // a bar just shown has not been laid out yet, and its frame says nothing until it has
        terminal.layoutSubtreeIfNeeded()
        return Int(((terminal.bounds.maxY - bar.frame.minY) * window.backingScaleFactor).rounded(.up))
    }

    // [LAW:one-source-of-truth] The view hierarchy is the only record of a window's bar,
    // so the window owns it and closing the window frees it.
    private static func existingBar(in window: NSWindow) -> KittyFindBar? {
        window.contentView?.subviews.lazy.compactMap { $0 as? KittyFindBar }.first
    }

    private static func bar(in window: NSWindow) -> KittyFindBar {
        if let bar = existingBar(in: window) { return bar }
        // Every GLFW window has a content view: the terminal
        let terminal = window.contentView!
        let bar = KittyFindBar()
        terminal.addSubview(bar)
        bar.topConstraint = bar.topAnchor.constraint(equalTo: terminal.topAnchor, constant: margin)
        NSLayoutConstraint.activate([
            bar.topConstraint,
            bar.trailingAnchor.constraint(equalTo: terminal.trailingAnchor, constant: -margin),
        ])
        return bar
    }

    private init() {
        super.init(frame: .zero)
        translatesAutoresizingMaskIntoConstraints = false
        wantsLayer = true
        layer?.cornerRadius = 6

        field.controlSize = .small
        field.font = .systemFont(ofSize: NSFont.systemFontSize(for: .small))
        // the action runs on every edit, the field's clear button included
        field.sendsWholeSearchString = false
        field.sendsSearchStringImmediately = true
        field.target = self
        field.action = #selector(queryChanged)
        field.delegate = self
        field.searchMenuTemplate = modeMenu()
        status.controlSize = .small
        status.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize(for: .small), weight: .regular)
        status.textColor = .secondaryLabelColor
        let steps = NSSegmentedControl(
            images: ["chevron.up", "chevron.down"].map { NSImage(systemSymbolName: $0, accessibilityDescription: nil)! },
            trackingMode: .momentary, target: self, action: #selector(stepPressed))
        steps.controlSize = .small
        steps.setToolTip("Previous match", forSegment: 0)
        steps.setToolTip("Next match", forSegment: 1)
        let done = NSButton(title: "Done", target: self, action: #selector(dismiss))
        done.controlSize = .small
        done.bezelStyle = .rounded

        let row = NSStackView(views: [field, status, steps, done])
        row.edgeInsets = NSEdgeInsets(top: 4, left: 6, bottom: 4, right: 6)
        row.translatesAutoresizingMaskIntoConstraints = false
        addSubview(row)
        NSLayoutConstraint.activate([
            row.topAnchor.constraint(equalTo: topAnchor),
            row.bottomAnchor.constraint(equalTo: bottomAnchor),
            row.leadingAnchor.constraint(equalTo: leadingAnchor),
            row.trailingAnchor.constraint(equalTo: trailingAnchor),
            field.widthAnchor.constraint(equalToConstant: 220),
        ])
    }

    required init?(coder: NSCoder) { fatalError("KittyFindBar is created in code") }

    // [LAW:one-source-of-truth] kitty knows where the top of its screen is; the bar asks when its
    // layout is next worked out; the constraint's constant is redone from the answer every time,
    // never a value of the bar's own.
    public override func updateConstraints() {
        if let window {
            topConstraint.constant = CGFloat(Self.delegate.findBarClearance(window)) / window.backingScaleFactor + Self.margin
        }
        super.updateConstraints()
    }

    // AppKit calls this with the window's effective appearance current, which GLFW derives from
    // the terminal's background colour, and again whenever that appearance changes.
    public override var wantsUpdateLayer: Bool { true }
    public override func updateLayer() {
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
    }

    // The bar's own background and padding take the clicks over them; NSView would pass them up
    // the responder chain to the terminal, which reports them to the program running in it.
    public override func mouseDown(with event: NSEvent) {}
    public override func mouseUp(with event: NSEvent) {}
    public override func rightMouseDown(with event: NSEvent) {}
    public override func rightMouseUp(with event: NSEvent) {}
    public override func otherMouseDown(with event: NSEvent) {}
    public override func otherMouseUp(with event: NSEvent) {}
    public override func mouseDragged(with event: NSEvent) {}
    public override func rightMouseDragged(with event: NSEvent) {}
    public override func otherMouseDragged(with event: NSEvent) {}
    public override func scrollWheel(with event: NSEvent) {}

    // The magnifier's menu: how a query matches. The items' state is read from the saved mode
    // each time the menu opens, so it shows a change made in another window or by a key.
    private func modeMenu() -> NSMenu {
        let menu = NSMenu()
        for (rule, title) in [(KittyFindCaseRule.smart, "Smart Case"), (.ignore, "Ignore Case"), (.match, "Match Case")] {
            let item = NSMenuItem(title: title, action: #selector(chooseCaseRule), keyEquivalent: "")
            item.target = self
            item.tag = rule.rawValue
            menu.addItem(item)
        }
        menu.addItem(.separator())
        let regex = NSMenuItem(title: "Regular Expression", action: #selector(toggleRegex), keyEquivalent: "")
        regex.target = self
        menu.addItem(regex)
        return menu
    }

    @objc private func chooseCaseRule(_ sender: NSMenuItem) {
        Self.setMode(caseRule: KittyFindCaseRule(rawValue: sender.tag)!, syntax: Self.syntax)
    }

    @objc private func toggleRegex(_ sender: NSMenuItem) {
        Self.setMode(caseRule: Self.caseRule, syntax: Self.syntax == .regex ? .literal : .regex)
    }

    @objc private func queryChanged() {
        Self.delegate.findBar(window!, queryChanged: field.stringValue)
    }

    // 0 is the previous match, 1 the next
    @objc private func stepPressed(_ sender: NSSegmentedControl) {
        Self.delegate.findBar(window!, step: sender.selectedSegment == 1)
    }

    // Esc in the field closes the bar; handling it here also stops NSSearchField's default of
    // clearing the text, which the next Cmd+F shows again. Return steps, and Shift+Return steps back.
    public func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        switch selector {
        case #selector(cancelOperation(_:)):
            dismiss()
        case #selector(insertNewline(_:)):
            Self.delegate.findBar(window!, step: !(NSApp.currentEvent?.modifierFlags.contains(.shift) ?? false))
        default:
            return false
        }
        return true
    }

    // Give focus back before hiding, so the terminal, not AppKit's fallback, takes it.
    @objc private func dismiss() {
        let window = window!
        window.makeFirstResponder(window.contentView)
        isHidden = true
        Self.delegate.findBarClosed(window)
    }
}

extension KittyFindBar: NSMenuItemValidation {
    // tmux and Claude Code search by their own rules, so the menu offers no mode while one of them
    // holds the bar's search: it never shows a mode that is not the one searching
    public func validateMenuItem(_ item: NSMenuItem) -> Bool {
        let chosen: Bool
        switch item.action {
        case #selector(chooseCaseRule):
            chosen = item.tag == Self.caseRule.rawValue
        case #selector(toggleRegex):
            chosen = Self.syntax == .regex
        default:
            return true
        }
        let honored = window.map { Self.delegate.findBarHonorsMode($0) } ?? false
        item.state = honored && chosen ? .on : .off
        return honored
    }
}
