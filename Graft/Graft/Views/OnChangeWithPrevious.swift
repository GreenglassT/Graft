import SwiftUI

extension View {
    /// `onChange(of:)` with the value before the change as well as after, as
    /// macOS 14's two-value form passes them. macOS 13's passes only the new
    /// value, so `ChangeWithPrevious` keeps the old one: the value when the
    /// view appeared, then each value it last saw.
    func onChangeWithPrevious<Value: Equatable>(
        of value: Value, _ action: @escaping (_ previous: Value, _ current: Value) -> Void
    ) -> some View {
        modifier(ChangeWithPrevious(value: value, action: action))
    }
}

/// See `View.onChangeWithPrevious(of:_:)`.
struct ChangeWithPrevious<Value: Equatable>: ViewModifier {
    let value: Value
    let action: (_ previous: Value, _ current: Value) -> Void
    /// Taken from `value` once, when the view first appears; a later init's
    /// is ignored, as `State`'s initial value always is.
    @State private var previous: Value

    init(value: Value, action: @escaping (_ previous: Value, _ current: Value) -> Void) {
        self.value = value
        self.action = action
        _previous = State(initialValue: value)
    }

    func body(content: Content) -> some View {
        content.onChange(of: value) { current in
            let before = previous
            previous = current
            action(before, current)
        }
    }
}
