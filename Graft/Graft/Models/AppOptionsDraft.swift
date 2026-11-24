import Foundation

/// A pending edit to an app's options: what config said when the sheet opened,
/// and what the user has typed since.
///
/// The seven fields used to be written out four times over in EditAppSheet —
/// `@State` declarations, `init` seeds, the `hasChanges` disjunction, and the
/// `save()` ternaries — so adding an eighth meant four correct edits with
/// nothing to catch a miss. Forgetting `hasChanges` greys Save out forever on a
/// real change; forgetting `save()` drops the field silently. Neither is a
/// compile error and neither shows up in a test of the view.
///
/// Here the list exists once, `==` is synthesized, and both questions the sheet
/// asks — "is Save enabled" and "what goes on the wire" — are derived from the
/// same pair of values.
struct AppOptionsDraft: Equatable {
    /// The editable fields, as one `Equatable` value. Adding a field is an
    /// edit here and a line in `init(app:)`; everything else follows.
    struct Fields: Equatable {
        var displayName: String
        var preferredVersion: String
        var delivery: DeliveryMode
        var keepBundleId: Bool
        var stripExtensions: Bool
        var stripSupportedDevices: Bool
        var compressionLevel: Int
        /// Which LiveContainer a push goes into. See `LCPin`.
        var lcPin: LCPin
        /// Make the provisioning profile through App Store Connect
        /// (`auto_profile`, C-AUTOPROFILE).
        var autoProfile: Bool
        /// The App Store the app's lookups ask (`store_country`); nil while
        /// unset. The picker offers nil only to an app that has none, so an
        /// edit can set or change it but never send a clear.
        var storeCountry: String? = nil
    }

    /// The app's LiveContainer pin, as the Edit sheet's picker holds it.
    ///
    /// An enum rather than a `String?` because `changed(_:)` already returns
    /// an optional — "unchanged" — and `String??` would make "changed to
    /// Automatic" and "unchanged" one `nil` apart. `automatic` is
    /// `--no-lc-bundle-id`; `bundleId` is `--lc-bundle-id <id>` (C-LCPIN).
    enum LCPin: Hashable {
        case automatic
        case bundleId(String)

        init(_ bundleId: String?) {
            self = bundleId.map { .bundleId($0) } ?? .automatic
        }
    }

    /// What the app record said when the sheet opened — the write-back
    /// baseline, fixed for the life of the edit.
    let original: Fields
    /// What the user has typed since. Bound directly by the form.
    var edited: Fields

    init(app: AppRecord) {
        // The one `?? ""` in the codebase: an app with no display name edits as
        // an empty field, and leaving it empty must read as "unchanged", not as
        // "set it to the empty string".
        original = Fields(
            displayName: app.displayName ?? "",
            preferredVersion: app.preferredVersion,
            delivery: app.delivery,
            keepBundleId: app.keepBundleId,
            stripExtensions: app.stripExtensions,
            stripSupportedDevices: app.stripSupportedDevices,
            compressionLevel: app.compressionLevel,
            lcPin: LCPin(app.lcBundleId),
            autoProfile: app.autoProfile,
            storeCountry: app.storeCountry
        )
        edited = original
    }

    /// Whether anything at all differs — Save's enablement, and the guard on
    /// starting a write at all.
    var hasChanges: Bool { edited != original }

    /// Take back the edits the delivery now in the picker hides or locks, so
    /// none of them goes out on Save unseen. The Edit sheet calls this on
    /// every delivery switch.
    ///
    /// - The LiveContainer pin, whenever its picker is off the sheet
    ///   (`lcPinShown` false, `EditAppSheet.offersLCPin`): a switch to
    ///   Sideload hides it.
    /// - `auto_profile`, on a switch to LiveContainer, which locks its toggle
    ///   (`EditAppSheet.autoProfileLock`). One turned on under Sideload stayed
    ///   on, grayed, and Save sent `--auto-profile`.
    ///
    /// Each goes back to `original`, what config holds, so only an unsaved
    /// edit is dropped. A saved value is never changed by the switch.
    mutating func revertEditsHiddenByDelivery(lcPinShown: Bool) {
        if !lcPinShown { edited.lcPin = original.lcPin }
        if edited.delivery == .livecontainer { edited.autoProfile = original.autoProfile }
    }

    /// The edited value of `field`, or nil when it still matches the original.
    ///
    /// nil is precisely what `ConfigStore.updateAppOptions` reads as "don't
    /// send this flag", and sending an unchanged field is not merely wasteful:
    /// the CLI writes every field it receives, so an unchanged `delivery` or
    /// `preferred_version` rewrites itself for no reason, and the store throws
    /// if the CLI's reported `changed` list doesn't match what was asked for.
    func changed<Value: Equatable>(_ field: KeyPath<Fields, Value>) -> Value? {
        let value = edited[keyPath: field]
        return value == original[keyPath: field] ? nil : value
    }
}
