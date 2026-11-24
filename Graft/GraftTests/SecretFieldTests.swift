import AppKit
import XCTest
@testable import Graft

/// The Apple ID password and the 2FA code are typed into `NoUndoSecureTextField`.
/// Release gate B.2: neither may reach undo history or the pasteboard.
@MainActor
final class SecretFieldTests: XCTestCase {
    private func focused() throws -> (NoUndoSecureTextField, NSTextView) {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 320, height: 80),
            styleMask: [.titled], backing: .buffered, defer: false
        )
        window.isReleasedWhenClosed = false
        addTeardownBlock { window.close() }
        let field = NoUndoSecureTextField(frame: NSRect(x: 10, y: 20, width: 280, height: 24))
        window.contentView?.addSubview(field)
        XCTAssertTrue(window.makeFirstResponder(field))
        let editor = try XCTUnwrap(field.currentEditor() as? NSTextView)
        editor.insertText("Example-Passw0rd", replacementRange: NSRange(location: NSNotFound, length: 0))
        XCTAssertEqual(field.stringValue, "Example-Passw0rd", "premise: the text went in")
        return (field, editor)
    }

    /// Review part 4, #20: no line break can be typed into a password or a code.
    func test_the_field_is_single_line() {
        let field = NoUndoSecureTextField(frame: NSRect(x: 0, y: 0, width: 200, height: 24))
        XCTAssertEqual(field.cell?.usesSingleLineMode, true)
        XCTAssertEqual(field.cell?.wraps, false)
    }

    func test_typing_leaves_nothing_to_undo() throws {
        let (_, editor) = try focused()
        XCTAssertFalse(editor.allowsUndo)
        XCTAssertFalse(editor.undoManager?.canUndo ?? false)
    }

    func test_copy_and_cut_are_refused() throws {
        let (_, editor) = try focused()
        editor.selectAll(nil)
        for action in [#selector(NSText.copy(_:)), #selector(NSText.cut(_:))] {
            let item = NSMenuItem(title: "", action: action, keyEquivalent: "")
            XCTAssertFalse(editor.validateMenuItem(item), "\(action) must be off in a secret field")
        }
    }

    func test_the_next_ordinary_field_in_the_window_keeps_its_undo() throws {
        let (field, _) = try focused()
        let window = try XCTUnwrap(field.window)
        let plain = NSTextField(frame: NSRect(x: 10, y: 50, width: 280, height: 24))
        window.contentView?.addSubview(plain)
        XCTAssertTrue(window.makeFirstResponder(plain))
        let editor = try XCTUnwrap(plain.currentEditor() as? NSTextView)
        XCTAssertTrue(editor.allowsUndo)
    }
}
