import XCTest
import Yams
@testable import Graft

final class AppConfigTests: XCTestCase {
    func test_decode_minimal_app_from_yaml() throws {
        let yaml = """
        bundle_id: com.example.app
        preferred_version: latest
        delivery: livecontainer
        tweaks:
          - a-tweak.deb
          - some-tweak.deb
        cyan:
          display_name: Example+
          keep_bundle_id: true
        """
        let app = try YAMLDecoder().decode(AppConfig.self, from: yaml)
        XCTAssertEqual(app.bundleId, "com.example.app")
        XCTAssertEqual(app.preferredVersion, "latest")
        XCTAssertEqual(app.delivery, .livecontainer)
        XCTAssertEqual(app.tweaks, ["a-tweak.deb", "some-tweak.deb"])
        XCTAssertEqual(app.cyan.displayName, "Example+")
        XCTAssertEqual(app.cyan.keepBundleId, true)
    }

    func test_decode_defaults_delivery_to_sideload() throws {
        let yaml = """
        bundle_id: com.example.app
        """
        let app = try YAMLDecoder().decode(AppConfig.self, from: yaml)
        XCTAssertEqual(app.delivery, .sideload)
    }
}
