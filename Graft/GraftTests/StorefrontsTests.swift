import XCTest
@testable import Graft

/// The App Store storefronts the app's store pickers offer (`Storefronts`),
/// and the store an app is looked up in, seen and changed in Edit App.
///
/// The list itself is pinned to the CLI's (`graft_ipa/storefronts.py`) from
/// the Python side, by tests/test_storefronts.py.
@MainActor
final class StorefrontsTests: XCTestCase {
    // MARK: the list and the mapping

    func test_the_list_is_apples_175_storefronts_with_no_duplicates() {
        XCTAssertEqual(Storefronts.all.count, 175)
        XCTAssertEqual(Set(Storefronts.all).count, 175)
        XCTAssertEqual(Set(Storefronts.byName), Set(Storefronts.all))
    }

    func test_a_region_maps_to_the_store_it_shops_in() {
        XCTAssertEqual(Storefronts.forRegion("GB"), .init(code: "GB", note: nil))
        XCTAssertEqual(Storefronts.forRegion("PR"), .init(code: "US", note: nil))
        XCTAssertEqual(Storefronts.forRegion("GL"), .init(code: "DK", note: nil))
        XCTAssertEqual(Storefronts.forRegion("AX"), .init(code: "FI", note: nil))
        // A neighbour's store (combined fix check #24).
        XCTAssertEqual(Storefronts.forRegion("MC"), .init(code: "FR", note: nil))
        XCTAssertEqual(Storefronts.forRegion("JE"), .init(code: "GB", note: nil))
        XCTAssertEqual(Storefronts.forRegion("AQ"), .init(code: "US", note: Storefronts.noStoreNote))
        XCTAssertEqual(Storefronts.forRegion("419"), .init(code: "US", note: nil))
        XCTAssertEqual(Storefronts.forRegion(nil), .init(code: "US", note: nil))
    }

    func test_a_code_is_a_store_a_territory_or_nothing() {
        XCTAssertEqual(Storefronts.store(for: "de"), "DE")
        XCTAssertEqual(Storefronts.store(for: "UK"), "GB")
        XCTAssertNil(Storefronts.store(for: "ZZ"))
        XCTAssertNil(Storefronts.store(for: "IR"))
    }

    func test_a_picker_label_names_the_store_and_its_code() {
        XCTAssertTrue(Storefronts.label("GB").hasSuffix("(GB)"))
        XCTAssertGreaterThan(Storefronts.label("GB").count, "(GB)".count + 1, "a name before the code")
    }

    // MARK: Edit App

    private struct NotAnAppEvent: Error {}

    private func app(storeKey: String?) throws -> AppRecord {
        let store = storeKey.map { #","store_country":\#($0)"# } ?? ""
        let line = #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":[],"display_name":null,"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6,"lc_bundle_id":null\#(store)}"#
        guard case let .app(record) = try BuildEvent.decode(line: line) else { throw NotAnAppEvent() }
        return record
    }

    func test_the_app_record_carries_its_store_and_whether_the_cli_said() throws {
        let set = try app(storeKey: #""GB""#)
        XCTAssertEqual(set.storeCountry, "GB")
        XCTAssertTrue(set.reportsStoreCountry)
        let unset = try app(storeKey: "null")
        XCTAssertNil(unset.storeCountry)
        XCTAssertTrue(unset.reportsStoreCountry)
        XCTAssertFalse(try app(storeKey: nil).reportsStoreCountry, "a CLI too old to say: no picker")
    }

    func test_the_edit_pickers_choices_are_every_store_and_unset_only_while_unset() {
        let unset = EditAppSheet.storeChoices(current: nil)
        XCTAssertEqual(unset.first, .some(nil))
        XCTAssertEqual(unset.count, 176)
        let set = EditAppSheet.storeChoices(current: "GB")
        XCTAssertFalse(set.contains(nil), "a store once set is changed, never cleared, from here")
        XCTAssertEqual(set.compactMap { $0 }, Storefronts.byName)
    }

    func test_changing_the_store_sends_only_store_country() async throws {
        var draft = AppOptionsDraft(app: try app(storeKey: #""DE""#))
        draft.edited.storeCountry = "GB"
        let unchanged = AppOptionsDraft(app: try app(storeKey: #""DE""#))

        let cli = try ScriptCLI(#"""
        case "$*" in
          *"--set-app-options"*) printf '%s\n' '{"type":"app-updated","name":"example","changed":["store_country"]}' ;;
        esac
        """#, test: self)
        let store = ConfigStore(runner: cli.runner, notify: { _ in })
        _ = try await store.updateAppOptions(appName: "example", draft: draft)

        XCTAssertEqual(cli.argv().first, "--json --set-app-options --app example --store-country GB")
        XCTAssertFalse(unchanged.hasChanges)
    }

    func test_the_app_option_field_is_the_clis_spelling() {
        XCTAssertEqual(CLIContract.AppOptionField.storeCountry.rawValue, "store_country")
    }
}
