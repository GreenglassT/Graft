import AppKit
import SwiftUI

/// An `NSSecureTextField` that keeps what is typed out of undo history.
///
/// A secure field already refuses Copy and Cut. Undo is the gap: the field
/// editor would register each keystroke, and ⌘Z, or the window's undo stack,
/// could bring a typed password back after it was cleared. Undo is switched
/// off the moment the field takes focus, before anything is typed, and its
/// undo actions dropped when editing ends. Turning it back on then is belt
/// and braces: AppKit sets the field editor up afresh for the next ordinary
/// field, which keeps its undo either way (SecretFieldTests).
final class NoUndoSecureTextField: NSSecureTextField {
    /// Single-line, so Option-Return or a paste can't put a line break in a
    /// password or a code (review part 4, #20). `ChildInput.send` refuses
    /// one anyway.
    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        cell?.usesSingleLineMode = true
        cell?.wraps = false
        cell?.isScrollable = true
    }

    required init?(coder: NSCoder) {
        super.init(coder: coder)
        cell?.usesSingleLineMode = true
    }

    override func becomeFirstResponder() -> Bool {
        let became = super.becomeFirstResponder()
        if became, let editor = currentEditor() as? NSTextView {
            editor.allowsUndo = false
        }
        return became
    }

    override func textDidEndEditing(_ notification: Notification) {
        if let editor = notification.object as? NSTextView {
            editor.undoManager?.removeAllActions(withTarget: editor)
            editor.allowsUndo = true
        }
        super.textDidEndEditing(notification)
    }
}

/// `NoUndoSecureTextField` for SwiftUI: the Apple ID password and the 2FA code.
struct SecretField: NSViewRepresentable {
    let placeholder: String
    @Binding var text: String
    var onSubmit: () -> Void = {}

    func makeNSView(context: Context) -> NoUndoSecureTextField {
        let field = NoUndoSecureTextField()
        field.placeholderString = placeholder
        field.setAccessibilityLabel(placeholder)
        field.delegate = context.coordinator
        field.target = context.coordinator
        field.action = #selector(Coordinator.submit)
        field.bezelStyle = .roundedBezel
        field.cell?.isScrollable = true
        field.cell?.wraps = false
        return field
    }

    func updateNSView(_ field: NoUndoSecureTextField, context: Context) {
        context.coordinator.parent = self
        field.isEnabled = context.environment.isEnabled
        // Clearing the binding clears the field: what the sheet does after a submit.
        if field.stringValue != text { field.stringValue = text }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: SecretField

        init(_ parent: SecretField) { self.parent = parent }

        func controlTextDidChange(_ note: Notification) {
            guard let field = note.object as? NSTextField else { return }
            parent.text = field.stringValue
        }

        @objc func submit() { parent.onSubmit() }
    }
}
