import Foundation

/// A pending edit to the global settings: what the CLI reported, plus whatever
/// the user has typed over it.
///
/// The rule this type exists to enforce is the one `AppOptionsDraft` enforces
/// for the per-app editor — send *only* what actually differs. A settings save
/// that posted every field would rewrite `signing.cert` every time somebody
/// nudged the retention stepper, and `--set-settings` is a PATCH precisely so
/// it doesn't have to. It also keeps the `changed` reconciliation meaningful:
/// asking for five fields and getting five back proves nothing if four of them
/// were never edited.
///
/// Modeled as baseline + edits rather than "current values plus a dirty flag"
/// because reverting has to be exact. A user who types into Team ID and then
/// deletes what they typed has changed nothing, and `hasChanges` says so.
struct SettingsDraft: Equatable {
    /// The editable fields, as one `Equatable` value — `AppOptionsDraft.Fields`,
    /// for the reason its own docstring gives.
    ///
    /// This started as five loose properties and a `hasChanges` that spelled
    /// out a five-way disjunction of `changed(_:) != nil`, which is exactly the
    /// hand-maintained list `AppOptionsDraft` exists to abolish: a sixth
    /// setting added to the view and forgotten there greys Save out forever on
    /// a real edit, and nothing catches it at compile time. Here the list
    /// exists once and `==` is synthesized.
    struct Fields: Equatable {
        var defaultDeviceUDID: String = ""
        var archiveKeep: Int = defaultArchiveKeep
        var signingCert: String = ""
        var signingProfile: String = ""
        var signingTeamId: String = ""
    }

    /// Mirrors `config.DEFAULT_ARCHIVE_KEEP`. Only reached when the value on
    /// disk is one the CLI refuses to read, which the pane says out loud rather
    /// than papering over — see `SettingsView.keepStepper`.
    static let defaultArchiveKeep = 2

    /// What the CLI last reported. nil before the first successful read, which
    /// is also when the pane refuses to offer editing at all.
    let baseline: SettingsRecord?

    /// The CLI reported `archive_keep` as null — a value on disk it refuses to
    /// read, which every build also refuses to run.
    ///
    /// It needs its own flag because the fallback hides it: seeding `original`
    /// and `edited` from the same default made them equal, so `changed` was
    /// always nil and Save could never send the field. The pane says "saving
    /// replaces it" directly above the stepper, and it did not — the only way
    /// to repair the value was to step it away and back again.
    private let countUnreadable: Bool

    /// The baseline as editable values — the write-back reference, fixed for
    /// the life of the edit.
    let original: Fields
    /// What the user has typed since. Bound directly by the form.
    var edited: Fields

    init(_ record: SettingsRecord? = nil) {
        baseline = record
        // Not when the whole file failed to parse (C-SETTINGS): every setting
        // is null then because nothing was read, and `--set-settings` refuses
        // that file — counting it lit Revert over a pane with nothing in it
        // anybody had changed, and nothing a save could replace.
        countUnreadable = record != nil && record?.archiveKeep == nil
            && record?.parseError == nil
        var fields = Fields()
        if let record {
            // "" is the wire form of "no default device" in both directions:
            // the picker's "Ask each time" row and the CLI's unset. Keeping
            // them the same string is what lets `changed(\.defaultDeviceUDID)`
            // treat clearing it as an edit rather than as an absence. The same
            // `?? ""` on the signing fields is what makes a half-filled
            // `signing:` block editable instead of unreadable.
            fields.defaultDeviceUDID = record.defaultDeviceUDID ?? ""
            fields.archiveKeep = record.archiveKeep ?? Self.defaultArchiveKeep
            fields.signingCert = record.signingCert ?? ""
            fields.signingProfile = record.signingProfile ?? ""
            fields.signingTeamId = record.signingTeamId ?? ""
        }
        original = fields
        // Seeded from the same value, never re-derived, so no field looks
        // edited before anybody has typed. The one deliberate exception is an
        // unreadable `archive_keep`, which `countUnreadable` reports as
        // changed — there the default is not a copy of the baseline, it is the
        // replacement for a value that has none.
        edited = fields
    }

    /// Whether anything would be sent. One comparison, so the Save button and
    /// the payload cannot disagree about whether there is an edit.
    ///
    /// The `baseline` test is the part `AppOptionsDraft` needs no equivalent of:
    /// that one is always built from a real `AppRecord`, while this one exists
    /// before the first read has landed. Without it, `Fields()`'s defaults
    /// become an accidental baseline, and typing into a pane that never
    /// successfully read anything would offer to write those defaults over the
    /// config — including `archive_keep: 2` on top of whatever is really there.
    var hasChanges: Bool {
        guard baseline != nil else { return false }
        // An unreadable count is itself something to write: there is a value on
        // disk that no build will accept, and the stepper is already showing
        // what would replace it.
        return countUnreadable || edited != original
    }

    /// The value to send for `field`, or nil when it matches the baseline.
    ///
    /// Generic over the key path so a new setting cannot be added to the view
    /// and forgotten here — the call site names the field once and gets the
    /// comparison for free.
    func changed<Value: Equatable>(_ field: KeyPath<Fields, Value>) -> Value? {
        guard baseline != nil else { return nil }
        let value = edited[keyPath: field]
        // There is no baseline to compare an unreadable count against, so the
        // displayed value always differs from what is on disk by definition.
        if countUnreadable, field == \Fields.archiveKeep { return value }
        return value == original[keyPath: field] ? nil : value
    }
}
