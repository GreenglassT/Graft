import XCTest
@testable import Graft

/// Preferences in memory, keyed by domain — the migration's whole view of
/// UserDefaults. A real `UserDefaults` would leave a plist in
/// ~/Library/Preferences for every throwaway domain a test names.
private final class MemoryDomains: PersistentDomainStore {
    var domains: [String: [String: Any]] = [:]

    func persistentDomain(forName domainName: String) -> [String: Any]? {
        domains[domainName]
    }

    func setPersistentDomain(_ domain: [String: Any], forName domainName: String) {
        domains[domainName] = domain
    }
}

/// The one-time copy of the pre-rename UserDefaults domain into Graft's.
///
/// Nothing here reads or writes a real preferences domain: the store is in
/// memory, and the launch hook is called with an explicit environment.
final class LegacyDefaultsTests: XCTestCase {
    private let legacy = "legacy.domain"
    private let current = "current.domain"
    private var store = MemoryDomains()

    override func setUp() {
        super.setUp()
        store = MemoryDomains()
    }

    private func seedLegacy(_ values: [String: Any]) {
        store.setPersistentDomain(values, forName: legacy)
    }

    private func migrate() -> Bool {
        LegacyDefaults.migrate(into: current, from: legacy, store: store)
    }

    func test_the_legacy_domain_is_the_old_bundle_id_exactly() {
        // The id in the pre-rename project.yml, case and all: a preferences
        // domain is a file name, and a near miss finds nothing to copy.
        XCTAssertEqual(LegacyDefaults.legacyDomain, "com.greenglasst.TweakBuild")
    }

    func test_an_empty_domain_receives_every_legacy_key_and_the_legacy_one_is_kept() {
        seedLegacy(["lastDeliveryMode": "livecontainer", "count": 3])

        XCTAssertTrue(migrate())

        let copied = store.persistentDomain(forName: current) ?? [:]
        XCTAssertEqual(copied["lastDeliveryMode"] as? String, "livecontainer")
        XCTAssertEqual(copied["count"] as? Int, 3)
        let kept = store.persistentDomain(forName: legacy) ?? [:]
        XCTAssertEqual(kept["lastDeliveryMode"] as? String, "livecontainer")
    }

    func test_it_happens_once() {
        seedLegacy(["lastDeliveryMode": "livecontainer"])
        XCTAssertTrue(migrate())

        // The user changes it under the new name; a later launch must not
        // put the old value back.
        var now = store.persistentDomain(forName: current) ?? [:]
        now["lastDeliveryMode"] = "sideload"
        store.setPersistentDomain(now, forName: current)

        XCTAssertFalse(migrate())
        XCTAssertEqual(
            store.persistentDomain(forName: current)?["lastDeliveryMode"] as? String, "sideload"
        )
    }

    func test_keys_the_new_domain_already_has_are_not_overwritten() {
        // Not hypothetical: until 2.2.1 the test suite ran the app as its
        // test host under the new bundle id, and AppKit saved window frames
        // into the new domain before any user ever opened it. A copy gated on
        // "the new domain is empty" would never run on such a machine.
        store.setPersistentDomain(["NSWindow Frame main": "1 2 3 4", "shared": "new"], forName: current)
        seedLegacy(["lastDeliveryMode": "livecontainer", "shared": "old"])

        XCTAssertTrue(migrate())

        let merged = store.persistentDomain(forName: current) ?? [:]
        XCTAssertEqual(merged["lastDeliveryMode"] as? String, "livecontainer")
        XCTAssertEqual(merged["shared"] as? String, "new")
        XCTAssertEqual(merged["NSWindow Frame main"] as? String, "1 2 3 4")
    }

    func test_nothing_to_copy_still_marks_the_check_done() {
        // An install that never was TweakBuild checks once and never again —
        // and a legacy domain that turns up later (the old app launched for
        // a look) is not copied over settings made since.
        XCTAssertFalse(migrate())
        let marked = store.persistentDomain(forName: current) ?? [:]
        XCTAssertEqual(marked[LegacyDefaults.migratedKey] as? Bool, true)
        XCTAssertEqual(marked.count, 1)

        seedLegacy(["lastDeliveryMode": "livecontainer"])
        XCTAssertFalse(migrate())
        XCTAssertNil(store.persistentDomain(forName: current)?["lastDeliveryMode"])
    }

    func test_a_domain_is_never_migrated_into_itself() {
        seedLegacy(["k": "v"])
        XCTAssertFalse(LegacyDefaults.migrate(into: legacy, from: legacy, store: store))
    }

    func test_the_launch_hook_stays_out_of_the_test_host() {
        // The test host *is* the app (its Debug build). Migrating there
        // would copy the developer's real TweakBuild preferences.
        seedLegacy(["k": "v"])
        XCTAssertFalse(LegacyDefaults.migrateOnLaunch(
            environment: ["XCTestConfigurationFilePath": "/tmp/x.xctestconfiguration"],
            domain: current, legacy: legacy, store: store
        ))
        XCTAssertNil(store.persistentDomain(forName: current))

        XCTAssertTrue(LegacyDefaults.migrateOnLaunch(
            environment: [:], domain: current, legacy: legacy, store: store
        ))
        XCTAssertEqual(store.persistentDomain(forName: current)?["k"] as? String, "v")
    }
}
