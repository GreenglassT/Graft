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
            compressionLevel: app.compressionLevel
        )
        edited = original
    }

    /// Whether anything at all differs — Save's enablement, and the guard on
    /// starting a write at all.
    var hasChanges: Bool { edited != original }

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
