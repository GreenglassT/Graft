import XCTest
@testable import Graft

final class BuildOptionsTests: XCTestCase {
    func test_default_options_render_no_arguments() {
        let options = BuildOptions()
        XCTAssertTrue(options.isDefault)
        XCTAssertEqual(options.cliArgs, [])
    }

    func test_each_toggle_renders_its_flag() {
        var options = BuildOptions()
        options.dryRun = true
        XCTAssertEqual(options.cliArgs, ["--dry-run"])

        options = BuildOptions()
        options.skipInstall = true
        XCTAssertEqual(options.cliArgs, ["--skip-install"])

        options = BuildOptions()
        options.skipDecryptCheck = true
        XCTAssertEqual(options.cliArgs, ["--skip-decrypt-check"])
    }

    func test_device_udid_renders_as_a_pair() {
        var options = BuildOptions()
        options.deviceUDID = "00008030-0000000000000001"
        XCTAssertEqual(options.cliArgs, ["--device", "00008030-0000000000000001"])
    }

    func test_nil_or_empty_device_renders_nothing() {
        var options = BuildOptions()
        options.deviceUDID = nil
        XCTAssertEqual(options.cliArgs, [])
        XCTAssertTrue(options.isDefault)

        options.deviceUDID = ""
        XCTAssertEqual(options.cliArgs, [], "an empty UDID must not produce a dangling --device")
        XCTAssertTrue(options.isDefault)
    }

    func test_combined_options_render_in_stable_order() {
        var options = BuildOptions()
        options.dryRun = true
        options.skipInstall = true
        options.skipDecryptCheck = true
        options.deviceUDID = "UDID1"
        XCTAssertEqual(
            options.cliArgs,
            ["--dry-run", "--skip-install", "--skip-decrypt-check", "--device", "UDID1"]
        )
        XCTAssertFalse(options.isDefault)
    }

    func test_active_options_are_typed_and_ordered() {
        var options = BuildOptions()
        options.dryRun = true
        options.skipInstall = true
        options.skipDecryptCheck = true
        options.deviceUDID = "UDID1"
        XCTAssertEqual(
            options.activeOptions,
            [.dryRun, .skipInstall, .skipDecryptCheck, .device(udid: "UDID1")]
        )
    }

    func test_each_option_renders_its_cli_flag() {
        XCTAssertEqual(BuildOptions.Option.dryRun.cliArgs, ["--dry-run"])
        XCTAssertEqual(BuildOptions.Option.skipInstall.cliArgs, ["--skip-install"])
        XCTAssertEqual(BuildOptions.Option.forPlaycover.cliArgs, ["--for-playcover"])
        XCTAssertEqual(
            BuildOptions.Option.device(udid: "UDID1").cliArgs,
            ["--device", "UDID1"]
        )
    }

    func test_skip_decrypt_check_renders_its_flag() {
        var options = BuildOptions()
        options.skipDecryptCheck = true
        XCTAssertEqual(options.cliArgs, ["--skip-decrypt-check"])
        XCTAssertEqual(options.activeOptions, [.skipDecryptCheck])
        XCTAssertFalse(options.isDefault)
    }

    func test_skip_decrypt_check_orders_before_device() {
        var options = BuildOptions()
        options.skipDecryptCheck = true
        options.deviceUDID = "UDID1"
        XCTAssertEqual(options.cliArgs, ["--skip-decrypt-check", "--device", "UDID1"])
    }

    func test_each_option_renders_its_cli_flag_including_skip_decrypt() {
        XCTAssertEqual(BuildOptions.Option.skipDecryptCheck.cliArgs, ["--skip-decrypt-check"])
        XCTAssertEqual(BuildOptions.Option.skipDecryptCheck.defaultLabel, "skip-decrypt-check")
    }

    // MARK: - Building for this Mac

    /// A Mac build must carry --skip-install, because the CLI refuses the pair
    /// without it — and refuses for a reason: the artifact points a tweak at a
    /// stub framework iOS has for real.
    func test_a_mac_build_asks_for_both_flags() {
        var options = BuildOptions()
        options.forPlaycover = true
        let args = options.cliArgs
        XCTAssertTrue(args.contains("--for-playcover"))
        XCTAssertTrue(args.contains("--skip-install"),
                      "the CLI refuses --for-playcover without it")
    }

    func test_an_ordinary_build_asks_for_neither() {
        XCTAssertFalse(BuildOptions().cliArgs.contains("--for-playcover"))
        XCTAssertFalse(BuildOptions().cliArgs.contains("--skip-install"))
    }

    /// The implication lives in `activeOptions`, not in `cliArgs`, so pin it
    /// there as well as on the argv.
    ///
    /// This is the assertion that would survive the flags being re-derived
    /// somewhere else: `activeOptions` is what `isDefault`,
    /// `deliverySuppressor`, `effectiveDelivery` and the header's pills all
    /// read, and an argv-only implication would have left every one of them
    /// describing a build that installs to a phone.
    func test_a_mac_build_activates_the_skip_as_a_typed_option_too() {
        var options = BuildOptions()
        options.forPlaycover = true
        XCTAssertEqual(options.activeOptions, [.forPlaycover, .skipInstall])
        XCTAssertEqual(options.cliArgs, ["--for-playcover", "--skip-install"])
        XCTAssertFalse(options.isDefault)
    }

    /// Ticking Skip install and then building for this Mac must not send the
    /// flag twice. Click accepts a repeated boolean flag, so this is tidiness
    /// rather than a failure — but `activeOptions` is a list two views iterate
    /// to draw one pill each, and a duplicate is a visible one.
    func test_a_mac_build_does_not_repeat_a_skip_the_user_already_set() {
        var options = BuildOptions()
        options.skipInstall = true
        options.forPlaycover = true
        XCTAssertEqual(options.activeOptions, [.forPlaycover, .skipInstall])
    }

    /// Not "Install skipped", which is what a suppressor derived from the
    /// implication alone would have said. A banner read away from the machine
    /// saying a phone install was skipped, about a run that built for this Mac,
    /// is the half-truth `EffectiveDelivery`'s own doc exists to prevent.
    func test_a_mac_build_names_the_mac_rather_than_the_skip_it_implies() {
        var options = BuildOptions()
        options.forPlaycover = true
        XCTAssertEqual(options.deliverySuppressor, .forPlaycover)
        XCTAssertEqual(
            options.effectiveDelivery(for: .livecontainer),
            .notDelivered(reason: "Built for this Mac")
        )
        XCTAssertEqual(
            options.effectiveDelivery(for: .sideload),
            .notDelivered(reason: "Built for this Mac")
        )
    }

    /// A dry run still outranks it: `cli.py` skips the PlayCover preparation
    /// entirely under `--dry-run` (`if for_playcover and not dry_run`), so the
    /// run does not even produce a Mac artifact, and "Dry run" is the more
    /// complete description of what happened.
    func test_a_dry_run_still_outranks_a_mac_build() {
        var options = BuildOptions()
        options.dryRun = true
        options.forPlaycover = true
        XCTAssertEqual(options.deliverySuppressor, .dryRun)
        XCTAssertEqual(options.activeOptions, [.dryRun, .forPlaycover, .skipInstall])
    }

    /// A Mac build never looks for a phone, so an empty bench cannot fail it —
    /// the same answer `--skip-install` gives, and for the same reason, since
    /// the CLI will not accept one without the other.
    func test_a_mac_build_does_not_resolve_a_device() {
        XCTAssertFalse(BuildOptions.Option.forPlaycover.stillResolvesDevice)
        XCTAssertEqual(BuildOptions.Option.forPlaycover.cliArgs, ["--for-playcover"])
        XCTAssertEqual(BuildOptions.Option.forPlaycover.defaultLabel, "for-playcover")
    }
}
