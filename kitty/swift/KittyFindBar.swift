// The find bar: a search field overlaid on the top-right of an OS window's terminal.
//
// Focus moves only through NSWindow.makeFirstResponder:, which GLFW's window class
// overrides to track which view owns focus (glfw/cocoa_window.m), so showing the bar
// takes focus and dismissing it gives focus back to the terminal, the window's content view.

import AppKit

@MainActor
@objc(KittyFindBar)
public final class KittyFindBar: NSView, NSSearchFieldDelegate {
    private let field = NSSearchField()

    // [LAW:dataflow-not-control-flow] The same steps whether the bar is new, hidden or already open:
    // a repeated Cmd+F lands in the field with all of its text selected.
    @objc public static func show(in window: NSWindow) {
        // Every GLFW window has a content view: the terminal
        let bar = bar(in: window.contentView!)
        bar.isHidden = false
        window.makeFirstResponder(bar.field)
        bar.field.selectText(nil)
    }

    // [LAW:one-source-of-truth] The view hierarchy is the only record of a window's bar,
    // so the window owns it and closing the window frees it.
    private static func bar(in terminal: NSView) -> KittyFindBar {
        if let bar = terminal.subviews.lazy.compactMap({ $0 as? KittyFindBar }).first { return bar }
        let bar = KittyFindBar()
        terminal.addSubview(bar)
        NSLayoutConstraint.activate([
            bar.topAnchor.constraint(equalTo: terminal.topAnchor, constant: 8),
            bar.trailingAnchor.constraint(equalTo: terminal.trailingAnchor, constant: -8),
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
        field.sendsWholeSearchString = false
        field.sendsSearchStringImmediately = false
        field.delegate = self
        let done = NSButton(title: "Done", target: self, action: #selector(dismiss))
        done.controlSize = .small
        done.bezelStyle = .rounded

        let row = NSStackView(views: [field, done])
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

    // Esc in the field. Handling it here also stops NSSearchField's default of clearing the text,
    // which the next Cmd+F shows again.
    public func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        guard selector == #selector(cancelOperation(_:)) else { return false }
        dismiss()
        return true
    }

    // Give focus back before hiding, so the terminal, not AppKit's fallback, takes it.
    @objc private func dismiss() {
        window!.makeFirstResponder(window!.contentView)
        isHidden = true
    }
}
