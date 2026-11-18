import XCTest
@testable import Graft

final class RepoStoreTests: XCTestCase {
    @MainActor
    func test_loadRepos_parses_repo_events() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"repo","url":"https://a.example.com","name":"A","origin":null,"package_count":3,"last_refreshed":"2026-05-22T10:00:00"}\n{"type":"repo","url":"https://b.example.com","name":"B","origin":null,"package_count":5,"last_refreshed":null}\n{"type":"repos-end","count":2}\n'
        """#
        let store = RepoStore(runner: runner)
        let repos = try await store.loadRepos(argsOverride: ["-c", script])
        XCTAssertEqual(repos.count, 2)
        XCTAssertEqual(repos[0].name, "A")
        XCTAssertEqual(repos[1].packageCount, 5)
    }

    @MainActor
    func test_loadRepos_throws_when_stream_truncated() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // a repo event but NO repos-end terminator => truncated stream
        let script = #"""
        printf '{"type":"repo","url":"https://a.example.com","name":"A","origin":null,"package_count":3,"last_refreshed":null}\n'
        """#
        let store = RepoStore(runner: runner)
        do {
            _ = try await store.loadRepos(argsOverride: ["-c", script])
            XCTFail("expected loadRepos to throw on a truncated stream")
        } catch {
            // expected
        }
    }

    /// The rule every other mutating store already follows: the write landed
    /// in config.yaml, so a refresh that fails afterwards is "it worked, the
    /// list is stale" — `lastError`, not a throw. This used to rethrow, which
    /// made ReposView clear nothing, keep the URL in the field, and tell the
    /// user the repo wasn't added, about a repo that was.
    @MainActor
    func test_addRepo_reports_reload_failure_without_throwing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let addScript = #"""
        printf '{"type":"repo-added","url":"https://a.example.com","name":"A","origin":null,"package_count":3,"last_refreshed":null}\n'
        """#
        let store = RepoStore(runner: runner, cliArgs: ["-c", "printf 'boom\n' >&2; exit 1"])

        try await store.addRepo(
            url: "https://a.example.com", argsOverride: ["-c", addScript]
        )

        XCTAssertEqual(
            store.fetchResult, "Added A — 3 packages", "the add itself succeeded"
        )
        XCTAssertTrue(
            store.lastError?.contains("couldn't refresh") == true,
            "got: \(store.lastError ?? "nil")"
        )
    }

    @MainActor
    func test_removeRepo_reports_reload_failure_without_throwing() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let listScript = #"""
        printf '{"type":"repo","url":"https://a.example.com","name":"A","origin":null,"package_count":1,"last_refreshed":null}\n{"type":"repos-end","count":1}\n'
        """#
        let pkgScript = #"""
        printf '{"type":"repo-package","package_id":"com.example.alpha","name":"Alpha","version":"1.0","description":"d","author":"a","section":"Tweaks","filename":"debs/alpha.deb","size_bytes":100,"sha256":null,"depiction":null,"download_url":"https://a.example.com/debs/alpha.deb"}\n{"type":"repo-packages-end","url":"https://a.example.com","count":1}\n'
        """#
        // The reload command is the one that fails; the remove itself succeeds.
        let store = RepoStore(runner: runner, cliArgs: ["-c", "printf 'boom\n' >&2; exit 1"])
        _ = try await store.loadRepos(argsOverride: ["-c", listScript])
        _ = try await store.loadPackages(
            repoURL: "https://a.example.com", refresh: false, argsOverride: ["-c", pkgScript]
        )
        XCTAssertFalse(store.packages.isEmpty, "precondition: a repo is open")

        let removeScript = #"printf '{"type":"repo-removed","url":"https://a.example.com"}\n'"#
        try await store.removeRepo(
            url: "https://a.example.com", argsOverride: ["-c", removeScript]
        )

        XCTAssertTrue(
            store.lastError?.contains("couldn't refresh") == true,
            "got: \(store.lastError ?? "nil")"
        )
        // The pane for the removed repo still has to close: a stale reload is
        // no reason to keep showing packages from a repo that is gone.
        XCTAssertTrue(store.packages.isEmpty)
        XCTAssertNil(store.packagesRepoURL)
    }

    /// The other half: a mutation that genuinely fails must still throw, or
    /// the view has nothing to report.
    @MainActor
    func test_addRepo_throws_when_the_cli_fails() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let store = RepoStore(runner: runner)
        do {
            try await store.addRepo(
                url: "https://a.example.com",
                argsOverride: ["-c", "printf 'not a repo\n' >&2; exit 1"]
            )
            XCTFail("expected addRepo to throw when the CLI exits non-zero")
        } catch {
            XCTAssertNil(store.lastError, "a failed add is the thrown error, not a stale note")
        }
    }

    @MainActor
    func test_loadPackages_parses_repo_package_events() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let script = #"""
        printf '{"type":"repo-package","package_id":"com.example.alpha","name":"Alpha","version":"1.0","description":"d","author":"a","section":"Tweaks","filename":"debs/alpha.deb","size_bytes":100,"sha256":"abc","depiction":null,"download_url":"https://a.example.com/debs/alpha.deb"}\n{"type":"repo-packages-end","url":"https://a.example.com","count":1}\n'
        """#
        let store = RepoStore(runner: runner)
        let pkgs = try await store.loadPackages(
            repoURL: "https://a.example.com", refresh: false, argsOverride: ["-c", script]
        )
        XCTAssertEqual(pkgs.count, 1)
        XCTAssertEqual(pkgs[0].downloadURL, "https://a.example.com/debs/alpha.deb")
    }

    @MainActor
    func test_loadPackages_throws_when_stream_truncated() async throws {
        let runner = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        // a repo-package event but NO repo-packages-end terminator
        let script = #"""
        printf '{"type":"repo-package","package_id":"com.example.alpha","name":"Alpha","version":"1.0","description":"d","author":"a","section":"Tweaks","filename":"debs/alpha.deb","size_bytes":100,"sha256":"abc","depiction":null,"download_url":"https://a.example.com/debs/alpha.deb"}\n'
        """#
        let store = RepoStore(runner: runner)
        do {
            _ = try await store.loadPackages(
                repoURL: "https://a.example.com", refresh: false, argsOverride: ["-c", script]
            )
            XCTFail("expected loadPackages to throw on a truncated stream")
        } catch {
            // expected
        }
    }
}
