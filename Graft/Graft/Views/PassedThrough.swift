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
