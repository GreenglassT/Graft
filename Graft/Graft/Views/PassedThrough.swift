import SwiftUI

/// A store a view only hands on, to a child view or into a `Task`: held
/// without observing it, so the store's changes don't redraw this view. Each
/// child that reads the store observes it there.
///
/// `ObservableStoresTests` proves the holder never reads it while drawing:
/// every use is a label, the initializer's own assignment, an argument to a
/// view's initializer, or inside a `Task { }` or `.task { }`.
@propertyWrapper
struct PassedThrough<Value> {
    var wrappedValue: Value
}

/// A store a view makes and only hands on, as `PassedThrough`'s are: kept for
/// the view's life like `@State`, without observing it, and made once.
///
/// `@State(initialValue:)` evaluates its value on every init of the view and
/// keeps only the first; a view SwiftUI re-creates often (`ContentView`, each
/// time `GraftApp`'s body runs) made a store for each and threw it away. This
/// makes it from its autoclosure, through `@StateObject`, the first time the
/// view goes on screen, and never again.
///
/// The box it is kept in has nothing `@Published`, so the `@StateObject`
/// never redraws the view: the store's own changes redraw only the children
/// that observe it. `ObservableStoresTests` holds it to `@PassedThrough`'s
/// rule.
@propertyWrapper
struct KeptState<Value: AnyObject>: DynamicProperty {
    @StateObject private var box: Box

    init(wrappedValue make: @autoclosure @escaping () -> Value) {
        _box = StateObject(wrappedValue: Box(make()))
    }

    var wrappedValue: Value { box.value }

    /// Holds the value. Publishes nothing, so observing it is free.
    final class Box: ObservableObject {
        let value: Value
        init(_ value: Value) { self.value = value }
    }
}
