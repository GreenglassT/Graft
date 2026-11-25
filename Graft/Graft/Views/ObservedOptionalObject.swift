import Combine
import SwiftUI

/// `@ObservedObject` for a store a view may not have.
///
/// `@ObservedObject` takes no optional, yet a view that reads an optional
/// store's state in `body` must redraw when that state changes, as it must
/// for any store it holds (`ContentView.updates`'s banner and alert,
/// `SettingsView.accountSetup`'s buttons). Nil observes nothing; a different
/// store observes the new one from the next update.
@propertyWrapper
struct ObservedOptionalObject<Object: ObservableObject>: DynamicProperty {
    let wrappedValue: Object?
    @StateObject private var relay = Relay()

    init(wrappedValue: Object?) {
        self.wrappedValue = wrappedValue
    }

    /// SwiftUI calls this before each `body`.
    func update() {
        relay.follow(wrappedValue)
    }

    /// Sends the store's changes on as its own, which redraws the view
    /// holding this.
    final class Relay: ObservableObject {
        /// Unpublished: which store is followed redraws nothing.
        private var followed: ObjectIdentifier?
        /// Unpublished: as `followed`.
        private var subscription: AnyCancellable?

        func follow(_ object: Object?) {
            let id = object.map(ObjectIdentifier.init)
            guard id != followed else { return }
            followed = id
            subscription = object?.objectWillChange.sink { [weak self] _ in
                self?.objectWillChange.send()
            }
        }
    }
}
