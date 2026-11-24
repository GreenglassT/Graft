import Foundation

/// The two calls the migration makes, so tests can run it against an
/// in-memory store: `UserDefaults` writes every domain it is handed to a
/// plist in ~/Library/Preferences, throwaway test domains included.
protocol PersistentDomainStore {
    func persistentDomain(forName domainName: String) -> [String: Any]?
    func setPersistentDomain(_ domain: [String: Any], forName domainName: String)
}

extension UserDefaults: PersistentDomainStore {}

/// Preferences saved before the rename, carried into Graft's domain once.
///
/// The app was `com.greenglasst.TweakBuild` and is now `com.greenglasst.graft`.
/// UserDefaults is keyed by bundle id, so without this every remembered choice
/// (the last delivery mode picked in Add App, for one) silently resets on the
/// first launch after upgrading.
///
/// Copied, not moved: the pre-rename app may still be installed, and its
/// domain is its own. Keys the new domain already has are left alone — they
/// were written by Graft and are newer — and a marker key makes it happen
/// once, so a value changed after the copy is never put back by a later
/// launch.
///
/// The marker rather than "copy when the new domain is empty", which was the
/// first design: the new domain is rarely empty by the time a user launches
/// the app. Running the test suite launches the app as its test host under
/// the new bundle id, and AppKit saves window frames into the domain then —
/// so on any machine that has run the tests, an emptiness check would have
/// skipped the migration for good.
enum LegacyDefaults {
    /// The bundle id in the pre-rename project.yml, case included.
    static let legacyDomain = "com.greenglasst.TweakBuild"

    /// Written into the new domain the first time the check runs, whether or
    /// not there was anything to copy.
    static let migratedKey = "migratedFromLegacyDefaults"

    /// Copy `legacy`'s keys into `domain`, once. True if anything was copied.
    ///
    /// The marker is set even when the legacy domain is empty: an install that
    /// never was TweakBuild checks once and never again, and a legacy domain
    /// that appears later (the old app launched for a look) is not copied
    /// over settings made since.
    @discardableResult
    static func migrate(
        into domain: String,
        from legacy: String = legacyDomain,
        store: PersistentDomainStore
    ) -> Bool {
        guard domain != legacy else { return false }
        var current = store.persistentDomain(forName: domain) ?? [:]
        guard current[migratedKey] == nil else { return false }
        let old = store.persistentDomain(forName: legacy) ?? [:]
        for (key, value) in old where current[key] == nil {
            current[key] = value
        }
        current[migratedKey] = true
        store.setPersistentDomain(current, forName: domain)
        return !old.isEmpty
    }

    /// The call `GraftApp` makes at launch.
    ///
    /// Skipped in the test host: that process is this app, under the real
    /// bundle id, and migrating there would copy the developer's own
    /// pre-rename preferences in the middle of a test run.
    @discardableResult
    static func migrateOnLaunch(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        domain: String? = Bundle.main.bundleIdentifier,
        legacy: String = legacyDomain,
        store: PersistentDomainStore
    ) -> Bool {
        guard environment["XCTestConfigurationFilePath"] == nil, let domain else { return false }
        return migrate(into: domain, from: legacy, store: store)
    }
}
