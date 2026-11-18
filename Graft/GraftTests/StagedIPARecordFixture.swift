import Foundation
@testable import Graft

/// A `StagedIPARecord` decoded from its own wire JSON — the superset of what
/// six independent copies of this builder, spread across the test target,
/// needed between them. `AddAppSheetPrefillTests`' own copy was the most
/// general before this file existed (it alone supported `lost` and
/// `importance`), so this one is lifted from it rather than invented fresh.
///
/// `DeviceDetailAddWiringTests` had **two** independent copies in one file —
/// `staged(_:version:filename:)` and `stagedForSheet(bundleId:displayName:)`
/// — differing only in which fields were parameterised; nobody had noticed
/// they already had one. `lost` and `importance` are the two fields most
/// worth sharing rather than re-adding by hand: they were the two most
/// recently added to the wire format, and updating six hand-written copies
/// for that meant six edits, where missing one fails silently — a decode
/// error inside `try Self.staged(...)` that `compactMap { try? }` (or a bare
/// `try?`) swallows, not a compile error naming the file that got missed.
///
/// **This is not the only builder in this file, and the second one is not a
/// variant of it.** `stagedIPAStreamLine` below feeds a
/// `"type":"staged-ipa"` line into a CLI-stream decoder
/// (`StagedIPAStore.load`), not `JSONDecoder().decode(StagedIPARecord.self,
/// from:)` directly, and it hands back the raw JSON `String` a `printf`
/// script needs to emit multiple lines — a `StagedIPARecord` value has
/// nothing to re-serialize into that shape. A genuinely different consumption
/// path, so the two stay separate functions rather than one forced to serve
/// both; they share this file because they are the same *kind* of thing to go
/// looking for, which is what stops a third copy being written by someone who
/// found neither.
func stagedIPARecord(
    bundleId: String = "com.a.one", displayName: String = "ExamplePlayer",
    version: String = "1.0", filename: String = "com.a.one_1.0.ipa",
    sizeBytes: Int = 128_299_008, preferred: Bool = true, stripped: String = "[]",
    lost: String? = nil, importance: String? = nil
) throws -> StagedIPARecord {
    let lostPart = lost.map { #","lost":\#($0)"# } ?? ""
    let importancePart = importance.map { #","importance":\#($0)"# } ?? ""
    let line = #"{"bundle_id":"\#(bundleId)","version":"\#(version)","display_name":"\#(displayName)","filename":"\#(filename)","path":"/c/\#(filename)","size_bytes":\#(sizeBytes),"preferred":\#(preferred),"stripped":\#(stripped)\#(lostPart)\#(importancePart)}"#
    return try JSONDecoder().decode(StagedIPARecord.self, from: Data(line.utf8))
}

/// One `staged-ipa` event, as the CLI writes it on stdout — the input side of
/// `StagedIPAStore.load`, where `stagedIPARecord` above is the output side.
///
/// Lifted here from `StagedIPAStoreTests`, which had the only copy, when the
/// Cache pane's tests needed the same two-version listing to render a pane
/// from. Writing a second copy is how the six copies of the *record* builder
/// this file replaced got started, and this one has a sharper edge than most:
/// the fields are the wire contract, so a copy is a place the contract can be
/// stale in silence — `String.buildEvents()` decodes with `try?`, so a line
/// this builder gets wrong is dropped rather than failing, and the test sees
/// an empty listing instead of an error.
///
/// `filename` and `path` are derived rather than parameters: every caller
/// wants them to agree with the bundle id and version, and no test so far has
/// wanted a listing where they do not.
func stagedIPAStreamLine(
    bundleId: String, version: String = "1.0", displayName: String = "Example",
    sizeBytes: Int = 4096, preferred: Bool = true, stripped: String = "[]",
    lost: String? = nil
) -> String {
    let lostPart = lost.map { #","lost":\#($0)"# } ?? ""
    return #"{"type":"staged-ipa","bundle_id":"\#(bundleId)","version":"\#(version)","display_name":"\#(displayName)","filename":"\#(bundleId)_\#(version).ipa","path":"/c/\#(bundleId)_\#(version).ipa","size_bytes":\#(sizeBytes),"preferred":\#(preferred),"stripped":\#(stripped)\#(lostPart)}"#
}

/// The sentinel that makes a run of `stagedIPAStreamLine`s a *listing*.
/// `StagedIPAStore.load` publishes nothing without it, and checks its count
/// against the lines it actually saw.
func stagedIPAsEndLine(_ count: Int) -> String {
    #"{"type":"staged-ipas-end","count":\#(count)}"#
}
