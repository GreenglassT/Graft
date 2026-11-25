import SwiftUI
import XCTest
@testable import Graft

/// The app detail pane has to fit the pane it is given.
///
/// Written after the Devices pane was found rendering *blank* with a phone
/// connected: `.fixedSize(horizontal: false, vertical: true)` pins a `Text` to
/// its ideal height for the width it is proposed, and where that proposed width
/// comes out as nothing, a long sentence resolves to one character per line and
/// hundreds of points of height. The pane then overflows the window and the
/// excess comes off the *top*, taking the header with it.
///
/// This pane has ten of those, more than anywhere else in the app, and most of
/// them render strings the CLI supplied — a delivery reason, a build failure, a
/// list of still-encrypted binary paths. Those are the ones worth measuring:
/// their length is not ours to choose, and the codebase already notes that a
/// stale app's reasons "run past a hundred characters".
///
/// The fixtures are deliberately two-tiered. The realistic ones are what the
/// CLI actually emits today; the pathological one is what nothing stops it
/// emitting tomorrow. A pane that only survives the strings we happen to
/// produce is a pane waiting for a longer one.
@MainActor
final class AppDetailLayoutTests: XCTestCase {

    private func store(events: String) async -> ConfigStore {
        let s = ConfigStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", "printf '\(events)'"], notify: { _ in })
        _ = try? await s.loadApps()
        return s
    }

    /// `AppDetailView` now reads `ContentView`'s polled store rather than
    /// building its own — see `AppDetailView.devices`'s doc comment — so every
    /// harness below needs one.
    ///
    /// Unloaded by default, which is the `.scanning` state — `hasLoaded` is
    /// false until a `load()` completes, and the bar now reads it. (It was
    /// `.noDevice` until the resolver learned that an empty list before the
    /// first scan is not an answer.) An empty picker is the ordinary condition
    /// for a layout measurement, and loading one would put a subprocess inside
    /// it for no reason most of this suite cares about. The build-target bar
    /// cases below do load one, because the bar's whole content depends on what
    /// is in it.
    private func devicesStore(printing lines: String? = nil) async -> DevicesStore {
        let store = DevicesStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        )
        if let lines {
            _ = try? await store.load(argsOverride: ["-c", "printf '\(lines)'"])
        }
        return store
    }

    /// One `device` event, in the shape `--devices --json` emits.
    private static func deviceLine(
        udid: String = "00008030-0000000000000001",
        name: String = "iPhone 15 Pro",
        connection: String = "Network",
        isDefault: Bool = false
    ) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"iPhone16,1","#
            + #""ios_version":"26.1","connection_type":"\#(connection)","#
            + #""livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","#
            + #""is_default":\#(isDefault)}\n"#
    }

    /// `tweaks` is a parameter because the attached-tweak rows render one row
    /// per entry, and the filename in it is half of the join those rows do —
    /// the other half being what `tweaksStore(printing:)` puts in the library.
    ///
    /// `bundleId` and `delivery` are parameters for the header's identity line,
    /// which renders both: the mode is the token that line exists to keep on
    /// screen, and the bundle id is the unbounded string beside it.
    private static func appLine(
        _ name: String,
        tweaks: [String] = ["a.deb"],
        bundleId: String = "com.example.app",
        delivery: String = "livecontainer"
    ) -> String {
        let attached = tweaks.map { "\"\($0)\"" }.joined(separator: ",")
        return #"{"type":"app","name":"\#(name)","bundle_id":"\#(bundleId)","preferred_version":"latest","tweaks":[\#(attached)],"display_name":"Example+","delivery":"\#(delivery)","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n"#
    }

    /// The tweak library the pane is handed, holding whatever the fixture
    /// prints.
    ///
    /// `tweaksStore:` is production API — `ContentView` passes its own store,
    /// exactly as it does `devices` — so this is not a seam, it is the same
    /// argument the app supplies. What the fixture buys is the *contents*: the
    /// real store runs `--tweaks`, which a `printf` runner cannot answer, so a
    /// case named for a tweak with a version and an author would otherwise
    /// measure the filename fallback — a different row, and a shorter one.
    ///
    /// `loaded: false` returns it unloaded and un-run, which is what the pane
    /// itself is expected to fix: see the lifecycle cases. Every case that is
    /// not about the attached rows takes one of those, because an unloaded
    /// library is what every visit looks like for its first moment and what a
    /// library the CLI could not read looks like forever — so those cases
    /// measure the fallback row on purpose.
    private func tweaksStore(printing lines: String = "", loaded: Bool = true) async -> TweaksStore {
        let store = TweaksStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", "printf '\(lines)'"]
        )
        if loaded { _ = try? await store.loadTweaks() }
        return store
    }

    /// One `tweak` event, in the shape `--tweaks --json` emits.
    ///
    /// The four control fields are optional on the wire and absent *together*
    /// for a .deb the CLI could not parse, so they are omitted from the JSON
    /// rather than sent as nulls — which is what the CLI does, and what the
    /// fallback row has to be measured against.
    private static func tweakLine(
        filename: String,
        name: String? = nil,
        version: String? = nil,
        section: String? = nil,
        author: String? = nil
    ) -> String {
        let control = [("name", name), ("version", version),
                       ("section", section), ("author", author)]
            .compactMap { key, value in value.map { #","\#(key)":"\#($0)""# } }
            .joined()
        return #"{"type":"tweak","filename":"\#(filename)","size_bytes":19154,"#
            + #""used_by":["example"]\#(control)}\n"#
    }

    /// `kind` is a parameter because the meta strip's delivery-state cell
    /// renders it — the four kinds are four different strings, and "not
    /// delivered" is the longest of them.
    private static func deliveryLine(
        summary: String, reasons: [String], kind: String = "stale"
    ) -> String {
        let joined = reasons.map { "\"\($0)\"" }.joined(separator: ",")
        return #"{"type":"delivery-state","app":"example","kind":"\#(kind)","summary":"\#(summary)","reasons":[\#(joined)],"pushed_at":"2026-05-13T16:20:00+00:00"}\n"#
    }

    /// The pane in the container it ships in — a split view's detail column.
    /// Measured outside one it never misbehaves, which is what hid the same
    /// defect in the Devices pane.
    private struct Harness: View {
        let app: AppRecord
        @ObservedObject var store: ConfigStore
        /// Never loaded, so every `image(for:)` answers nil and the header
        /// measures the lettered tile. That is the branch this suite wants:
        /// the tile and real artwork occupy the identical 44pt frame
        /// (`AppIcon` clips both), so an icon cannot be what pushes this pane
        /// past the window, and a fixture that fetched artwork would put a
        /// subprocess in a layout measurement.
        @ObservedObject var artwork: ArtworkStore
        @ObservedObject var devices: DevicesStore
        /// An empty, unloaded store for every case that is not about the
        /// attached-tweak rows, which measures the filename fallback — see
        /// `tweaksStore(printing:loaded:)`.
        @ObservedObject var tweaks: TweaksStore
        /// Never loaded and never asked for anything: no case in this suite
        /// presses Build for this Mac, and the pane touches this store only
        /// from that action's handoff. It is here because the initializer
        /// requires it — see `AppDetailView.staged`.
        @ObservedObject var staged: StagedIPAStore
        /// Over the same fake CLI as everything else here, which answers
        /// `--appstore-check` with nothing: the row then reads "Couldn't check…",
        /// the longest thing it can say, and it is `lineLimit(1)` so it cannot
        /// widen the pane.
        @ObservedObject var appStore: AppStoreStore
        var body: some View {
            NavigationSplitView {
                List { Text("Bravo"); Text("Delta"); Text("Example+") }
                    .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                AppDetailView(
                    app: app, runner: store.runner, store: store,
                    artwork: artwork, devices: devices, tweaksStore: tweaks,
                    staged: staged,
                    appStore: appStore,
                    liveContainer: LiveContainerStore(runner: store.runner),
                    settings: SettingsStore(runner: store.runner),
                    // The bar's Change control. Nothing here observes it; the
                    // pane only needs it to be constructible.
                    showSettings: {},
                    // Never asked: no case here opens Build options.
                    playCover: .inert
                )
            }
        }
    }

    /// `NSHostingView` grows to fit content demanding more than it is given, so
    /// a host taller than the window it was framed to *is* the overflow — on
    /// screen that same excess is clipped instead.
    private func assertFits(
        _ s: ConfigStore,
        _ label: String,
        devices: DevicesStore? = nil,
        expectingTarget: BuildTarget? = nil,
        /// What the meta strip's delivery-state cell should read. Asserted
        /// rather than assumed for the same reason `expectingTarget` is: the
        /// cell's four values are four different widths, and a case named for
        /// the longest one that silently measured a dash would be measuring
        /// nothing.
        expectingDeliveryCell: String? = nil,
        /// Whether the fixture is expected to carry a delivery state at all.
        /// False for the one case that is *about* its absence — where the
        /// anti-vacuity check below has to be inverted, because there the
        /// missing state is the premise rather than a lost fixture.
        hasDeliveryState: Bool = true,
        /// The tweak library the attached rows are named from. nil hands the
        /// pane an empty unloaded one, so every row falls back to its filename.
        tweaks: TweaksStore? = nil,
        width: CGFloat = 1200,
        height: CGFloat = 700,
        file: StaticString = #filePath,
        line: UInt = #line
    ) async {
        guard let app = s.apps.first else {
            return XCTFail("fixture produced no app, so nothing was measured",
                           file: file, line: line)
        }
        // Spelled out rather than `devices ?? devicesStore()`: `??` takes its
        // right-hand side as a non-async autoclosure, so the fallback cannot be
        // awaited there.
        let deviceStore: DevicesStore
        if let devices {
            deviceStore = devices
        } else {
            deviceStore = await devicesStore()
        }
        // Anti-vacuity for the build-target bar, and the same kind of check the
        // delivery-state assertion below is. The bar's three states render
        // three different things, and a device fixture that failed to reach the
        // store would silently downgrade every one of them to `.noDevice` — the
        // shortest of the three — so the measurement would be of a bar the case
        // is not about.
        if let expectingTarget {
            XCTAssertEqual(
                // The same arguments `AppDetailView.buildTargetBar` passes,
                // minus the one it cannot: `buildOptions` is `@State` inside
                // the pane, and a harness with default options never suppresses
                // delivery.
                BuildTargetBar.target(
                    devices: deviceStore.devices, delivery: app.delivery,
                    deviceErrors: deviceStore.deviceErrors, hasLoaded: deviceStore.hasLoaded
                ),
                expectingTarget,
                "the device fixture did not put the bar in the state this case is named "
                + "for, so what was measured is a different bar",
                file: file, line: line
            )
        }
        // Anti-vacuity. Every case here passed on its first run, which on this
        // pane is the right answer — its cards live inside a `ScrollView`, so
        // vertical overflow is absorbed rather than pushed at the window. But
        // "passed immediately" and "measured nothing" look identical from the
        // outside, and an earlier layout test in this suite did turn out to be
        // measuring an empty placeholder. So assert the fixture actually
        // reached the store: no delivery state means the amber card never
        // rendered and none of the long strings under test were on screen.
        if hasDeliveryState {
            XCTAssertNotNil(
                s.deliveryStates[app.name],
                "the delivery-state fixture never reached the store, so the card "
                + "carrying the long text was not in the measured tree",
                file: file, line: line
            )
        } else {
            XCTAssertNil(
                s.deliveryStates[app.name],
                "premise: this case is about an app the CLI said nothing about, and "
                + "a state reached the store anyway",
                file: file, line: line
            )
        }
        // The meta strip's first cell. Same anti-vacuity role as the two checks
        // above, aimed at the one thing on this pane whose value the harness
        // cannot otherwise be shown to have rendered.
        if let expectingDeliveryCell {
            XCTAssertEqual(
                AppDetailView.deliveryStateValue(s.deliveryStates[app.name]),
                expectingDeliveryCell,
                "the delivery-state cell did not read what this case is named for, "
                + "so what was measured is a different strip",
                file: file, line: line
            )
        }
        let library: TweaksStore
        if let tweaks {
            library = tweaks
        } else {
            library = await tweaksStore(loaded: false)
        }
        assertHostFits(
            Harness(
                app: app, store: s, artwork: ArtworkStore(runner: s.runner),
                devices: deviceStore, tweaks: library,
                staged: StagedIPAStore(runner: s.runner),
                appStore: AppStoreStore(runner: s.runner, shell: NoAdminShell())
            ),
            label, width: width, height: height, file: file, line: line
        )
    }

    /// Host a view in a window of a given size and assert it did not demand
    /// more than it was given, on either axis.
    ///
    /// Split out of `assertFits` so the build-target bar can be measured on its
    /// own — see `test_fits_with_an_unconnected_chosen_device` for why one of
    /// its states cannot go through the whole-pane harness. Both callers frame
    /// the same way, so a change to the tolerances applies to both.
    private func assertHostFits(
        _ root: some View,
        _ label: String,
        width: CGFloat,
        height: CGFloat,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let (host, window) = hosted(root, width: width, height: height)
        // Held only so the window outlives the assertions below: it owns the
        // hosting view, and a released window takes the tree being measured
        // with it.
        _ = window
        // 40pt of slack for the titlebar area a NavigationSplitView reserves.
        // The defect this guards against is measured in hundreds of points.
        XCTAssertLessThanOrEqual(
            host.bounds.height, height + 40,
            "the app detail pane (\(label)) demanded \(Int(host.bounds.height))pt of "
            + "height in a \(Int(height))pt window — on screen the excess is clipped "
            + "off the top, taking the header and Build & Install with it",
            file: file, line: line
        )
        // The other axis, kept because this pane now has content *outside* the
        // scroll view. Height is the hazard above a `ScrollView` and width is
        // the hazard inside one — the scroll view absorbs its content's height
        // and passes its width demand straight out — so the header and the
        // build-target bar are guarded by the assertion above, and everything
        // in the scrolling column by this one.
        XCTAssertLessThanOrEqual(
            host.bounds.width, width + 2,
            "the app detail pane (\(label)) demanded \(Int(host.bounds.width))pt of "
            + "width in a \(Int(width))pt window — the overhang is clipped, and it is "
            + "the right-hand edge of the pane that goes: Build & Install, Check "
            + "device, every tweak row's detach control",
            file: file, line: line
        )
    }

    /// Put a view in a real window at a given size and lay it out.
    ///
    /// Shared by the two things this file hosts for: the fit assertions above,
    /// and the lifecycle cases below, which host the pane not to measure it but
    /// to make its `.task`s run. Those had their own copy of these ten lines,
    /// which is how the window came to be dropped on the floor in one of them
    /// and retained in the other.
    ///
    /// Generic over the root view rather than taking `AnyView`, so the caller
    /// gets back an `NSHostingView` it can assign a new `rootView` to — which is
    /// what a sidebar click looks like from here: `ContentView` builds the pane
    /// without an `.id(app.id)`, so selecting another app replaces the root
    /// view's value and SwiftUI reuses the same `AppDetailView`.
    ///
    /// The window is returned, not discarded, because it owns the hosting view
    /// — letting it go releases the tree the caller is about to measure or wait
    /// on.
    private func hosted<Root: View>(
        _ root: Root, width: CGFloat, height: CGFloat
    ) -> (host: NSHostingView<Root>, window: NSWindow) {
        let host = NSHostingView(rootView: root)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: width, height: height),
            styleMask: [.titled, .resizable], backing: .buffered, defer: false
        )
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: width, height: height)
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        return (host, window)
    }

    /// The ordinary case, so a fix that rescues the long strings by breaking
    /// the common one is caught.
    func test_fits_with_an_ordinary_delivery_state() async {
        let events = Self.appLine("example")
            + Self.deliveryLine(summary: "Changed since the last delivery.",
                                reasons: ["d-tweak.deb added"])
        await assertFits(store(events: events), "ordinary")
    }

    /// The real shape the codebase already documents for a stale app.
    func test_fits_with_a_realistic_long_reason() async {
        let events = Self.appLine("example")
            + Self.deliveryLine(
                summary: "Changed since the last delivery.",
                reasons: ["rebuilt since delivery; app version changed: 21.16.2 → 21.17.1"])
        await assertFits(store(events: events), "realistic long reason")
    }

    /// Several reasons at once, which is what a genuinely stale app produces.
    func test_fits_with_several_reasons() async {
        let events = Self.appLine("example")
            + Self.deliveryLine(summary: "Changed since the last delivery.", reasons: [
                "rebuilt since delivery; app version changed: 21.16.2 → 21.17.1",
                "com.example.atweak_1.3.2_iphoneos-arm64.deb added",
                "com.example.sometweak_1.7.20_iphoneos-arm64.deb updated",
            ])
        await assertFits(store(events: events), "several reasons")
    }

    /// Nothing bounds what the CLI can put in a reason, and a filesystem path
    /// or a URL has nowhere to wrap. This is the case the Devices pane failed.
    func test_fits_with_a_pathological_reason() async {
        let events = Self.appLine("example")
            + Self.deliveryLine(
                summary: "Changed since the last delivery.",
                reasons: ["/Users/someone/Library/Application Support/"
                          + String(repeating: "nested/", count: 30) + "payload.deb replaced"])
        await assertFits(store(events: events), "pathological reason")
    }

    /// At the app's own minimum height, where there is least room to absorb a
    /// pane that sizes to its content.
    func test_fits_at_the_minimum_window_height() async {
        let events = Self.appLine("example")
            + Self.deliveryLine(summary: "Changed since the last delivery.", reasons: [
                "rebuilt since delivery; app version changed: 21.16.2 → 21.17.1",
                "com.example.atweak_1.3.2_iphoneos-arm64.deb added",
            ])
        await assertFits(store(events: events), "several reasons, minimum height",
                         height: Metrics.windowMinHeight)
    }

    // MARK: - The build-target bar
    //
    // The bar sits *above* the `ScrollView`, which is what makes it worth
    // measuring separately from everything else on this pane: the cards below
    // it are inside the scroll view and the bar is not.
    //
    // **The axis that catches it is width, and that was measured rather than
    // reasoned to — the first guess was height and it was wrong.** A 200-
    // character `Text` with a plain `.fixedSize()` injected into the bar takes
    // this pane to 1590pt of width in a 900pt window while its height stays
    // inside tolerance, so the height assertion above — the one this file
    // shipped with — passes straight through the injection and the width
    // assertion beside it is what fails. The mechanism is that the bar is a
    // single-row `HStack` in which nothing wraps: an over-wide item makes the
    // row wider, never taller. The scrolling sections below still risk height,
    // which is why both assertions are kept.
    //
    // **What these cases can and cannot show, corrected after a review found
    // this block asserting the opposite.** It used to say every state is
    // measured "because the renderings are different lengths and the shortest
    // one passing says nothing about the longest". That is not a property any
    // assertion here can have. Every sentence renders through
    // `BuildTargetBar.missing(_:)` or `pending(_:)`, both `lineLimit(1)` with
    // `.truncationMode(.tail)`, and a `Text` that truncates demands no width —
    // truncating is precisely how it avoids demanding any. Substituting a
    // 504-character string for the `.notDelivering` and `.overrideMissing`
    // sentences left both of the cases below green. There is no falsifying
    // input for a claim about sentence length, so the claim is not made here;
    // the inventory of what the sentences actually are lives on `BuildTarget`,
    // where it describes the strings instead of pretending to guard them.
    //
    // What is left is worth having, and is a different sentence: **one case per
    // rendering shape**. The bar renders in three — the `.device` triple-`Text`
    // `HStack`, the amber `missing`, the muted `pending` — and a structural
    // regression, an unbounded `.fixedSize()` child or a fixed oversize frame,
    // escapes on width from whichever branch it is added to. Measured, so the
    // limit is on the record too: with `.fixedSize()` injected, the pane in a
    // 900pt window holds at 900pt through an 84-character sentence and reaches
    // 907pt at 86, so the tripwire is ~180pt clear of the longest sentence the
    // bar renders. Big regressions are caught; a `.fixedSize()` on a short
    // label would not be.
    //
    // Every state is measured at the app's minimum window size, where that
    // margin is smallest. Two of them — `.overrideMissing` and
    // `.notDelivering` — cannot be reached through the whole-pane harness; see
    // their cases for why.
    //
    // `//` rather than `///`, because a doc comment separated from the next
    // declaration by a blank line documents nothing: this block sat above
    // `ordinaryDelivery` and read as that constant's documentation.

    private static let ordinaryDelivery =
        deliveryLine(summary: "Changed since the last delivery.",
                     reasons: ["d-tweak.deb added"])

    /// A phone is marked: the full sentence, plus the Change control.
    func test_fits_with_a_build_target() async {
        let devices = await devicesStore(printing: Self.deviceLine(isDefault: true)
                                         + #"{"type":"devices-end","count":1,"failed":0}\n"#)
        await assertFits(
            store(events: Self.appLine("example") + Self.ordinaryDelivery),
            "a resolved build target",
            devices: devices,
            expectingTarget: .device(name: "iPhone 15 Pro", mode: "LiveContainer"),
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// Two phones and neither is marked — the bar's amber "nothing is set"
    /// line, in the `missing` shape, with the Change control beside it.
    ///
    /// The character-count inventory that used to live here has moved to
    /// `BuildTarget`, where it documents the sentences rather than posing as
    /// this case's reason for existing. Nothing in this file could ever have
    /// checked it; see the block above.
    func test_fits_with_no_default_device() async {
        let devices = await devicesStore(
            printing: Self.deviceLine()
                + Self.deviceLine(udid: "00008030-0000000000000003",
                                  name: "iPhone 13 Pro", connection: "USB")
                + #"{"type":"devices-end","count":2,"failed":0}\n"#
        )
        XCTAssertEqual(devices.devices.count, 2, "premise: both phones reached the store")
        await assertFits(
            store(events: Self.appLine("example") + Self.ordinaryDelivery),
            "no default device",
            devices: devices,
            expectingTarget: .noDefault,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// Nothing connected. Kept as its own case rather than left to the other
    /// tests' unloaded store, so the state is asserted rather than assumed.
    func test_fits_with_no_device_connected() async {
        let devices = await devicesStore(
            printing: #"{"type":"devices-end","count":0,"failed":0}\n"#
        )
        XCTAssertTrue(devices.devices.isEmpty, "premise: nothing is connected")
        await assertFits(
            store(events: Self.appLine("example") + Self.ordinaryDelivery),
            "no device connected",
            devices: devices,
            expectingTarget: .noDevice,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// Before the first scan lands — the state the pane is in for the first
    /// second of every launch, and indefinitely if the window loses focus
    /// mid-scan.
    ///
    /// The store is deliberately never loaded here, which is what leaves
    /// `hasLoaded` false. That is also the default harness the five cases at
    /// the top of this file use, so this case is what pins what *they* have
    /// been measuring all along.
    func test_fits_while_the_first_device_scan_is_running() async {
        let devices = await devicesStore()
        XCTAssertFalse(devices.hasLoaded, "premise: no scan has completed")
        await assertFits(
            store(events: Self.appLine("example") + Self.ordinaryDelivery),
            "scanning for devices",
            devices: devices,
            expectingTarget: .scanning,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// One phone on the bus that the CLI could not describe, and nothing it
    /// could — the locked-handset case, which must not read as "no device
    /// connected".
    func test_fits_with_only_an_unreadable_device() async {
        let devices = await devicesStore(
            printing: #"{"type":"device-error","udid":"00008130-001A2B3C0123456D","#
                + #""error":"lockdown query failed: device is passcode locked"}\n"#
                + #"{"type":"devices-end","count":0,"failed":1}\n"#
        )
        XCTAssertEqual(devices.deviceErrors.count, 1, "premise: the failure reached the store")
        XCTAssertTrue(devices.devices.isEmpty, "premise: nothing was describable")
        await assertFits(
            store(events: Self.appLine("example") + Self.ordinaryDelivery),
            "one unreadable device",
            devices: devices,
            expectingTarget: .unreadable(count: 1),
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// The two states the pane cannot reach through the whole-pane harness.
    ///
    /// `AppDetailView` owns its `buildOptions` as `@State` with no injection
    /// point — deliberately, they are per-build and reset on selection — so a
    /// test cannot put the Device picker or the Dry run switch into the states
    /// that produce `.overrideMissing` and `.notDelivering`. Measuring the bar
    /// on its own inside the same split view's detail column is what is left,
    /// and it is the honest half: the bar's width demand is the property at
    /// risk, and the sidebar column beside it is what makes the detail column
    /// narrow.
    ///
    /// Worth saying what this cannot catch, so nobody reads more into it than
    /// it measures. Two things: it says nothing about the strip and cards below
    /// the bar, which the whole-pane cases above cover in every other bar state
    /// — and it says nothing about the *length* of the sentence it renders,
    /// because that sentence is `lineLimit(1)` and tail-truncated. It measures
    /// the `missing` shape, at the width where the shape has least room.
    func test_fits_with_an_unconnected_chosen_device() {
        let udid = "00008030-0000000000000004"
        assertHostFits(
            BarHarness(target: .overrideMissing(udid: udid)),
            "a chosen device that is not connected",
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// The `pending` shape — the muted sentence, which no other case here
    /// reaches at the minimum width with a reason in it.
    ///
    /// This case's doc used to call it "the longest sentence the bar can render
    /// — 'Install skipped …' at 55 characters, second only to `.noDevice`'s
    /// 58". Both counts were right and the sentence around them was not: a
    /// tail-truncating `Text` makes the assertion below indifferent to how long
    /// its string is, so nothing here was ever measuring "the longest".
    func test_fits_with_a_build_that_installs_nothing() {
        assertHostFits(
            BarHarness(target: .notDelivering(option: .skipInstall, device: .noDevice)),
            "a build that installs nothing",
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// The other `pending`/`missing` fork inside the same case: a dry run that
    /// is going to die resolving a device renders amber, through `missing`,
    /// with a differently-shaped sentence built from two pieces.
    func test_fits_with_a_dry_run_that_cannot_resolve_a_device() {
        let target = BuildTarget.notDelivering(option: .dryRun, device: .noDevice)
        XCTAssertTrue(target.needsAttention,
                      "premise: this is the amber fork, or it measures the muted one twice")
        assertHostFits(
            BarHarness(target: target),
            "a dry run that cannot resolve a device",
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// The chosen phone is on the bus and could not be described — the third
    /// state the whole-pane harness cannot reach, since it needs Build options'
    /// Device picker set.
    func test_fits_with_a_chosen_device_that_could_not_be_read() {
        assertHostFits(
            BarHarness(target: .overrideUnreadable(udid: "00008130-001A2B3C0123456D")),
            "a chosen device that could not be read",
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// The bar in the detail column it ships in, without the pane around it.
    private struct BarHarness: View {
        let target: BuildTarget
        var body: some View {
            NavigationSplitView {
                List { Text("Bravo"); Text("Delta"); Text("Example+") }
                    .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                VStack(alignment: .leading, spacing: 0) {
                    BuildTargetBar(target: target, onChange: {})
                    Spacer(minLength: 0)
                }
            }
        }
    }

    /// The `.device` shape — three `Text`s in a row, the only sentence on this
    /// bar assembled from more than one — carrying the bar's one unbounded
    /// input, a device name the user typed into Settings on the phone.
    ///
    /// **The length is the fixture, not the claim**, and this doc used to have
    /// that backwards. A 128-character name cannot move either assertion: the
    /// `HStack` is `lineLimit(1)` and the name alone carries
    /// `.truncationMode(.tail)` and `layoutPriority(-1)`, which makes it the
    /// *first* thing to give up width, so it shrinks to nothing before the row
    /// asks the window for anything. What the case is really for is the
    /// arrangement — that the middle element is the one that yields, and that
    /// "via LiveContainer" and the Change control keep their room. The long
    /// name is the input that would expose a regression in that arrangement,
    /// such as the negative priority being dropped or a `.fixedSize()` arriving
    /// beside it; a short name would expose nothing.
    func test_fits_with_a_build_target_named_at_length() async {
        let name = "Owners-iPhone-15-Pro-the-one-on-the-bench-by-the-window-do-not-"
            + "wipe-this-one-it-has-the-good-jailbreak-on-it-and-the-cable-is-fine"
        let devices = await devicesStore(
            printing: Self.deviceLine(name: name, isDefault: true)
                + #"{"type":"devices-end","count":1,"failed":0}\n"#
        )
        XCTAssertEqual(devices.devices.first?.name, name, "premise: the name reached the bar")
        await assertFits(
            store(events: Self.appLine("example") + Self.ordinaryDelivery),
            "an unwrappable device name",
            devices: devices,
            expectingTarget: .device(name: name, mode: "LiveContainer"),
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    // MARK: - The header's identity line

    /// The delivery mode is on the header's second line, and the line reports
    /// which of the two modes this app uses.
    ///
    /// Not a layout case: this is the copy, pinned where a test can read it
    /// without hosting anything — the same arrangement as the meta strip's
    /// delivery-state value. `DeliveryMode.displayName`'s two spellings are
    /// already pinned in `BuildTargetBarTests`; what this pins is that the pane
    /// spends one of this line's three tokens on the mode at all, which it had
    /// stopped doing everywhere else on the pane. See
    /// `AppDetailView.identityLine` for the three commits that took it away.
    func test_the_identity_line_names_the_delivery_mode() async {
        let live = await store(events: Self.appLine("example"))
        let sideloaded = await store(events: Self.appLine("example", delivery: "sideload"))
        guard let lc = live.apps.first, let sl = sideloaded.apps.first else {
            return XCTFail("fixture produced no app, so nothing was read")
        }
        XCTAssertEqual(
            AppDetailView.identityLine(lc),
            "LiveContainer · com.example.app · latest"
        )
        XCTAssertEqual(
            AppDetailView.identityLine(sl),
            "Sideload · com.example.app · latest",
            "both modes have to reach this line, or it is naming a constant"
        )
    }

    /// The header at the app's minimum window size, carrying a bundle id long
    /// enough that the line has to truncate.
    ///
    /// **Height is the axis, and the header is where a *string* can still
    /// escape on it.** The header is above the `ScrollView`, and unlike the
    /// build-target bar — a single-row `HStack` in which nothing wraps, which is
    /// why the bar's guard is the width assertion — it is a vertical column of
    /// text that yields width to the control cluster beside it. That is the
    /// shape behind this file's opening rule and behind the 2267pt figure in
    /// `BuildTargetBar`'s doc.
    ///
    /// **What it catches, measured on this checkout rather than assumed, in
    /// both directions:**
    ///
    ///  - `.fixedSize(horizontal: false, vertical: true)` added to the identity
    ///    line — this pane's own idiom, used ten times in the scrolling column
    ///    below it — takes the pane to **4566pt of height in a 560pt window**
    ///    with this case's 279-character fixture, and to 1101pt with a
    ///    48-character one. That is the regression this case exists for, and
    ///    the fixture clears the 40pt tolerance by thousands of points.
    ///  - Removing `lineLimit(1)` **on its own is not caught**: the pane holds
    ///    at 900×592 with a 2655-character bundle id. Nothing in the header is
    ///    `fixedSize`d, so a wrapped line is absorbed — the header takes the
    ///    room off the scroll view under it rather than demanding it from the
    ///    window. On screen that is still a defect (the header grows a line,
    ///    exactly as the title above it once did mid-build) and no assertion in
    ///    this file can see it. Said here rather than left for someone to
    ///    discover by trusting the suite.
    ///
    /// So the length is the fixture and not the claim, the same way it is for
    /// `test_fits_with_a_build_target_named_at_length`. The two-tier fixture is
    /// this file's own: a bundle id nobody types, because nothing bounds what
    /// config.yaml can hold — `bundle_id` is a string the CLI passes through.
    func test_fits_with_a_long_identity_line_in_the_header() async {
        let bundleId = "com.example."
            + String(repeating: "deeply.nested.internal.component.", count: 8) + "app"
        let s = await store(
            events: Self.appLine("example", bundleId: bundleId) + Self.ordinaryDelivery
        )
        guard let app = s.apps.first else { return XCTFail("fixture produced no app") }
        XCTAssertTrue(
            AppDetailView.identityLine(app).hasPrefix("LiveContainer · com.example."),
            "premise: the long id reached the line under test, ahead of the mode"
        )
        await assertFits(
            s, "a long identity line in the header",
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    // MARK: - The delivery-state cell

    /// The meta strip's first cell, carrying its longest *value*.
    ///
    /// "not delivered" is the longest of `DeliveryKind`'s four labels. What
    /// this case measures is the pane with that value rendered — the
    /// `expectingDeliveryCell` assertion is the load-bearing half, since it is
    /// what proves the fixture reached the cell at all.
    ///
    /// It does not measure the value's length, and an earlier version of this
    /// doc claimed it did ("the axis that catches an oversized one is width").
    /// `MetaCell`'s value is `lineLimit(1)` with `.truncationMode(.middle)` and
    /// the four cells are `frame(maxWidth: .infinity)`, so they split the row
    /// equally whatever is in them: a longer value truncates harder and demands
    /// nothing. The one length in this strip that *can* change a measurement is
    /// the label above the value, which wraps rather than truncates — see
    /// `test_the_longest_meta_label_does_not_wrap_at_the_narrowest_cell`.
    func test_fits_with_a_delivery_state_in_the_meta_strip() async {
        let events = Self.appLine("example")
            + Self.deliveryLine(summary: "Never delivered to a device.",
                                reasons: [], kind: "never")
        await assertFits(
            store(events: events), "the longest delivery-state label",
            expectingDeliveryCell: "not delivered",
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// **The tight end of the meta strip, which nothing else here reaches.**
    ///
    /// `MetaCell`'s label sets font, tracking and case (`labelCaps()`) and no
    /// `lineLimit`, so unlike everything else on this pane it *wraps* rather
    /// than truncating. Wrapping makes the cell taller, the strip lives inside
    /// the pane's `ScrollView`, and the scroll view absorbs height — so no
    /// `assertFits` case in this file can see it, at any window size. It has to
    /// be measured directly.
    ///
    /// It also cannot be seen at the width the rest of this file measures. The
    /// harness pins the sidebar to its *ideal* 260, where a cell is 142pt wide.
    /// The sidebar is draggable to `max: 360` (`ContentView.swift:166`), and
    /// there a cell is 117pt — so the pane the user can actually produce is
    /// 25pt tighter than the one every other case measures.
    ///
    /// Measured, at 900pt of window with the sidebar at 360: "DELIVERY STATE"
    /// wants 91pt, a cell wraps below 110pt and does not at 110pt or above, and
    /// the cell is 117pt. So it does **not** wrap — with about 7pt, call it one
    /// character, in hand. That margin is the reason this test exists rather
    /// than a comment saying it is fine: a fifth cell, a longer label, or a
    /// tracking change would eat it, and nothing else would notice.
    ///
    /// Deliberately *not* fixed by adding `lineLimit(1)` to the label. Wrapping
    /// is the better failure here — a label that wraps to two lines is still
    /// readable, and one that truncates to "DELIVERY ST…" is not.
    func test_the_longest_meta_label_does_not_wrap_at_the_narrowest_cell() {
        let width = Self.narrowestMetaCellWidth()
        XCTAssertLessThan(
            width, 142,
            "premise: this is the sidebar-at-maximum width, which must be tighter than "
            + "the 142pt a cell gets at the ideal width every other case here measures"
        )
        // A one-word label cannot wrap at this width, so it is the height a
        // non-wrapping cell has — no literal to drift.
        let unwrapped = Self.fittingHeight(label: "Tweaks", width: width)
        XCTAssertEqual(
            Self.fittingHeight(label: "Delivery state", width: width), unwrapped,
            "DELIVERY STATE wrapped to a second line in a \(Int(width))pt cell, which is "
            + "what the strip gets with the sidebar dragged to its maximum. The strip is "
            + "inside the ScrollView, so the extra line is absorbed and no other test in "
            + "this file can see it — the row just quietly grows taller than its siblings"
        )
        // The premise for the assertion above: this harness can tell the two
        // apart at all. Without it, a `fittingHeight` that had stopped
        // responding to its argument would make the case unfalsifiable.
        XCTAssertGreaterThan(
            Self.fittingHeight(label: "Delivery state", width: 100), unwrapped,
            "premise: at a width narrow enough to force the wrap, this measurement does "
            + "report it — otherwise the assertion above proves nothing"
        )
    }

    /// The width one of the meta strip's four cells gets in the tightest pane
    /// the app can actually be put into: minimum window width, sidebar dragged
    /// to its maximum.
    ///
    /// Read off a real `NavigationSplitView` rather than computed from
    /// `Metrics`, because the arithmetic would have to guess at the split
    /// view's own divider and insets — the numbers this returns (117pt at a 360
    /// sidebar, against 142 at the ideal 260) are 1–2pt off any hand-derived
    /// figure, and it is the real one that decides whether a label wraps.
    ///
    /// The value is captured from inside the `GeometryReader`'s body because
    /// the settled figure only arrives on the second layout pass; `onAppear`
    /// fires on the first, and reports the 20pt placeholder.
    private static func narrowestMetaCellWidth() -> CGFloat {
        final class Box { var width: CGFloat = 0 }
        let box = Box()
        struct Probe: View {
            let record: (CGFloat) -> Void
            var body: some View {
                NavigationSplitView {
                    // The maximum `ContentView` allows the sidebar to be
                    // dragged to, pinned so the probe cannot drift back to the
                    // ideal width the rest of this file measures.
                    List { Text("Bravo") }.navigationSplitViewColumnWidth(360)
                } detail: {
                    VStack(spacing: 0) {
                        HStack(spacing: Metrics.controlGap) {
                            MetaCell(label: "Delivery state", value: "not delivered")
                                .background(GeometryReader { (geo: GeometryProxy) -> Color in
                                    record(geo.size.width)
                                    return Color.clear
                                })
                            MetaCell(label: "Tweaks", value: "3")
                            MetaCell(label: "Profile", value: "auto")
                            MetaCell(label: "Last build", value: "2m ago")
                        }
                        .padding(.horizontal, Metrics.paneGutter)
                        Spacer(minLength: 0)
                    }
                }
            }
        }
        let host = NSHostingView(rootView: Probe(record: { box.width = $0 }))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0,
                                width: Metrics.windowMinWidth, height: Metrics.windowMinHeight),
            styleMask: [.titled, .resizable], backing: .buffered, defer: false
        )
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0,
                            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight)
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        return box.width
    }

    /// The height a `MetaCell` wants at a given width — which is 14pt taller
    /// when its label has wrapped to a second line.
    private static func fittingHeight(label: String, width: CGFloat) -> CGFloat {
        let host = NSHostingView(
            rootView: MetaCell(label: label, value: "not delivered").frame(width: width)
        )
        host.layoutSubtreeIfNeeded()
        return host.fittingSize.height
    }

    /// An app the CLI said nothing about, which is what an older `graft`
    /// on PATH produces for every app at once.
    ///
    /// The cell falls back to the em dash the device pane's stat strip uses,
    /// and the amber card is absent — so this also measures the pane in the
    /// shape none of the cases above it have, with no delivery state anywhere.
    func test_fits_with_no_delivery_state_at_all() async {
        await assertFits(
            store(events: Self.appLine("example")), "no delivery state",
            expectingDeliveryCell: "—", hasDeliveryState: false,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    // MARK: - The attached-tweak rows
    //
    // These rows used to render one filename each, and now render a name over a
    // `version · section · author` line joined from the tweak library. That
    // makes them a third source of CLI-supplied text on this pane whose length
    // is not ours to choose — a package's Name, Version and Author are whatever
    // the packager typed into a control file.
    //
    // The axis is width, for the reason the build-target bar's block above
    // records: the rows live inside the pane's `ScrollView`, which absorbs
    // height and passes width straight out. Measured at the app's minimum
    // window width, where the row has least to give.
    //
    // Each case asserts the join before measuring. Both halves of it are
    // fixtures — the app's filename list and the library's records — and a
    // mismatch between them measures the fallback row under a name that
    // promises a described one, which is the vacuous pass this file has already
    // caught once elsewhere.

    /// The whole point of the change: a tweak the CLI could read a control
    /// stanza out of, showing its own name and version.
    func test_fits_with_a_described_tweak() async {
        let filename = "com.example.sometweak_1.7.20_iphoneos-arm64.deb"
        let s = await store(
            events: Self.appLine("example", tweaks: [filename]) + Self.ordinaryDelivery
        )
        let library = await tweaksStore(
            printing: Self.tweakLine(
                filename: filename, name: "Some Tweak", version: "1.7.20",
                section: "Tweaks", author: "example"
            )
        )
        let rows = AppDetailView.attachedTweaks(s.apps.first?.tweaks ?? [], from: library.tweaks)
        XCTAssertEqual(rows.first?.displayName, "Some Tweak",
                       "premise: the record reached the row, which is what this case measures")
        XCTAssertEqual(rows.first?.detailLine, "1.7.20 · Tweaks · example",
                       "premise: the second line this case is named for was rendered")
        await assertFits(
            s, "a described tweak", tweaks: library,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// Both ways a row ends up with nothing to say, in one pane — they render
    /// identically and arrive differently.
    ///
    /// `truncated…deb` is in the library with every control field absent, which
    /// is exactly what the CLI emits for a .deb it could not parse: a truncated
    /// download, a renamed zip, a compression its Python lacks. `vanished.deb`
    /// is not in the library at all — the file was deleted or renamed under a
    /// config.yaml that still lists it, and it is also every row's state for the
    /// moment before the library load lands.
    ///
    /// Both keep their row and fall back to the filename. Dropping either would
    /// hide a tweak the *build* will still ship, leaving the pane quietly
    /// disagreeing with config.yaml about what this app is.
    ///
    /// The library is deliberately not empty either: it holds a fully described
    /// record for a file this app does not list, so the case fails if the join
    /// ever starts iterating the library instead of the app.
    func test_fits_with_tweaks_the_library_cannot_describe() async {
        let unparseable = "truncated_download_iphoneos-arm64.deb"
        let missing = "vanished.deb"
        let s = await store(
            events: Self.appLine("example", tweaks: [unparseable, missing])
                + Self.ordinaryDelivery
        )
        let library = await tweaksStore(
            printing: Self.tweakLine(filename: unparseable)
                + Self.tweakLine(filename: "notattached.deb", name: "Some Other Tweak",
                                 version: "3.0", section: "Tweaks", author: "someone")
        )
        XCTAssertEqual(library.tweaks.map(\.filename), ["notattached.deb", unparseable],
                       "premise: the library holds the unparseable file and one unattached one")
        let rows = AppDetailView.attachedTweaks(s.apps.first?.tweaks ?? [], from: library.tweaks)
        XCTAssertEqual(rows.map(\.filename), [unparseable, missing],
                       "premise: both rows survived, and the library's own record is not one")
        XCTAssertEqual(rows.map(\.displayName), [unparseable, missing],
                       "premise: both fall back to the filename, which is what is measured")
        XCTAssertEqual(rows.compactMap(\.detailLine), [],
                       "premise: neither row has a second line to measure")
        await assertFits(
            s, "tweaks the library cannot describe", tweaks: library,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// Two builds of one tweak attached at once, which is what puts a filename
    /// back on the row — see `AppDetailView.AttachedTweak.filenameLine`. Worth
    /// measuring because it is the one row shape with three lines in it.
    func test_fits_with_two_builds_of_the_same_tweak() async {
        let plain = "com.example.btweak_1.1.8_iphoneos-arm64.deb"
        let jailed = "com.example.btweak_1.1.8-jailed_iphoneos-arm64.deb"
        let s = await store(
            events: Self.appLine("example", tweaks: [plain, jailed]) + Self.ordinaryDelivery
        )
        let library = await tweaksStore(
            printing: Self.tweakLine(filename: plain, name: "BTweak", version: "1.1.8",
                                     section: "Tweaks", author: "example")
                + Self.tweakLine(filename: jailed, name: "BTweak", version: "1.1.8",
                                 section: "Tweaks", author: "example")
        )
        let rows = AppDetailView.attachedTweaks(s.apps.first?.tweaks ?? [], from: library.tweaks)
        XCTAssertEqual(rows.map(\.displayName), ["BTweak", "BTweak"],
                       "premise: the two rows are indistinguishable by headline")
        XCTAssertEqual(rows.compactMap(\.filenameLine), [plain, jailed],
                       "premise: both rows carry the third line this case measures")
        await assertFits(
            s, "two builds of the same tweak", tweaks: library,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    /// Nothing bounds a control field. A Name is free text, a Version is free
    /// text, and an Author is commonly an unbroken `handle <email>` token with
    /// nowhere to wrap — the shape that has taken this window down before
    /// (`ErrorBanner.tokenLimit` records the mechanism).
    func test_fits_with_a_pathological_tweak_detail_line() async {
        let filename = "com.example.verbose_iphoneos-arm64.deb"
        let author = String(repeating: "example-and-friends-", count: 12) + "maintainers"
        let s = await store(
            events: Self.appLine("example", tweaks: [filename]) + Self.ordinaryDelivery
        )
        let library = await tweaksStore(
            printing: Self.tweakLine(
                filename: filename,
                name: String(repeating: "Ultimate ", count: 20) + "Tweak",
                version: "1.7.20-beta.4+build.20260814.macos.arm64.nightly.rebuilt",
                section: "Tweaks (Universal, jailed and jailbroken, TrollStore)",
                author: author
            )
        )
        let rows = AppDetailView.attachedTweaks(s.apps.first?.tweaks ?? [], from: library.tweaks)
        XCTAssertGreaterThan(rows.first?.detailLine?.count ?? 0, 200,
                             "premise: the detail line under test is actually pathological")
        await assertFits(
            s, "a pathological tweak detail line", tweaks: library,
            width: Metrics.windowMinWidth, height: Metrics.windowMinHeight
        )
    }

    // MARK: - Loading the library
    //
    // Not layout. These host the pane in order to make its `.task(id: app.id)`
    // run, and assert on what it spawned — the one thing the pane's own state
    // cannot show, because the two failure modes below leave the library empty
    // exactly as "not loaded yet" does.
    //
    // **The evidence is a count of subprocesses**, kept in a file the fixture's
    // `sh` appends to. Nothing else distinguishes a gate that holds from one
    // that re-runs `--tweaks` on every sidebar click: the rows read identically,
    // the store reads identically, and the only symptom on screen is a red
    // banner re-raised per navigation (the per-app reset task nils `tweakError`
    // each time), which is `@State` this harness cannot reach.
    //
    // Each case walks three apps in one hosting view, the way a sidebar click
    // does — `ContentView` builds the pane without an `.id(app.id)`, so the view
    // is reused and only the task's id changes. The first app has nothing
    // attached and the second does, which is what makes the third step's
    // negative assertion mean something: if swapping the root view did not
    // re-fire the task at all, the second step would never load and the case
    // fails there rather than passing vacuously at the end.

    /// The library the pane is handed, wired to count what it spawns.
    ///
    /// `failing:` is `--tweaks` off PATH, the documented failure mode for this
    /// GUI, reduced to its observable shape: a non-zero exit with nothing on
    /// stdout.
    private func countingTweaksStore(
        counter: URL, printing lines: String = "", failing: Bool = false
    ) -> TweaksStore {
        TweaksStore(
            runner: CLIRunner(executable: URL(fileURLWithPath: "/bin/sh")),
            cliArgs: ["-c", "printf 'run\\n' >> '\(counter.path)'; printf '\(lines)'"
                      + (failing ? "; exit 1" : "")]
        )
    }

    /// A path the fixture appends one line to per run, cleaned up afterward.
    private func spawnCounter() -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("tweaks-spawns-\(UUID().uuidString)")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    /// How many `--tweaks` runs the counter has recorded. A missing file is
    /// none — `sh` creates it on the first append.
    private func spawns(_ counter: URL) -> Int {
        guard let text = try? String(contentsOf: counter, encoding: .utf8) else { return 0 }
        return text.split(separator: "\n", omittingEmptySubsequences: true).count
    }

    /// Poll a condition to a deadline. Polled rather than slept on a fixed
    /// interval, because what it waits for is a subprocess.
    private func waitUntil(
        _ condition: @autoclosure () -> Bool, timeout: TimeInterval = 5
    ) async throws -> Bool {
        let deadline = Date().addingTimeInterval(timeout)
        while !condition() && Date() < deadline {
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        return condition()
    }

    /// How long a step that must *not* spawn anything is given to prove it.
    ///
    /// Sized against the load it is watching for: each case below spends 2.0s of
    /// this waiting and finishes in ~2.3s, so hosting the pane and running a
    /// `--tweaks` to completion — the middle step, which waits on `hasLoaded`
    /// rather than on a clock — costs a few hundred milliseconds. A second is
    /// several times that, and both mutants of the gate were caught with it.
    private static let quietWindow: UInt64 = 1_000_000_000

    /// Walk three apps through one hosted pane and hand back the counter.
    ///
    /// Returns after the third app, having asserted the first two steps: no
    /// spawn for an app with nothing attached, exactly one for the app that has
    /// something. The caller asserts what the third step cost.
    private func walkThreeApps(
        library: TweaksStore, counter: URL,
        file: StaticString = #filePath, line: UInt = #line
    ) async throws {
        let s = await store(
            events: Self.appLine("alpha", tweaks: [])
                + Self.appLine("beta", tweaks: ["some-tweak.deb"])
                + Self.appLine("gamma", tweaks: ["some-tweak.deb"])
        )
        guard s.apps.count == 3 else {
            return XCTFail("fixture produced \(s.apps.count) apps, not three",
                           file: file, line: line)
        }
        let artwork = ArtworkStore(runner: s.runner)
        let devices = await devicesStore()
        let staged = StagedIPAStore(runner: s.runner)
        let appStore = AppStoreStore(runner: s.runner, shell: NoAdminShell())
        func harness(_ app: AppRecord) -> Harness {
            Harness(app: app, store: s, artwork: artwork, devices: devices, tweaks: library,
                    staged: staged, appStore: appStore)
        }

        let (host, window) = hosted(harness(s.apps[0]), width: 1200, height: 700)
        _ = window  // owns the hosting view; see `hosted`
        try await Task.sleep(nanoseconds: Self.quietWindow)
        XCTAssertEqual(
            spawns(counter), 0,
            "an app with no tweaks attached has no row to name, so it must not pay for "
            + "the library — the Attach sheet loads it when opened",
            file: file, line: line
        )

        host.rootView = harness(s.apps[1])
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        let loaded = try await waitUntil(library.hasLoaded)
        XCTAssertTrue(
            loaded,
            "selecting an app with a tweak attached must load the library, and this step "
            + "is also what proves the swap below re-fires the pane's per-app task",
            file: file, line: line
        )
        XCTAssertEqual(spawns(counter), 1, "exactly one --tweaks so far",
                       file: file, line: line)
        XCTAssertTrue(
            library.tweaks.isEmpty,
            "premise: this case is about a library that came back with nothing, so "
            + "`tweaks.isEmpty` still cannot tell it from one nobody has loaded",
            file: file, line: line
        )

        host.rootView = harness(s.apps[2])
        host.layoutSubtreeIfNeeded()
        window.displayIfNeeded()
        try await Task.sleep(nanoseconds: Self.quietWindow)
    }

    /// The pane loads the library itself, which is the whole reason any of the
    /// rows above have anything to render.
    ///
    /// This is the defect the layout cases would not have caught: the join can
    /// be perfect and the rows still read as filenames forever. `ContentView`
    /// owns this store but loads it only on entering the Tweaks section, and
    /// the app the window opens on is selected before the user has been
    /// anywhere; `AttachTweaksSheet` loads it too, but only when opened. So this
    /// hosts the pane with a store nobody has touched and waits for it to fill.
    /// Delete the `.task` and every layout case above still passes on its
    /// seeded store.
    func test_the_pane_loads_the_tweak_library_for_itself() async throws {
        let s = await store(
            events: Self.appLine("example", tweaks: ["some-tweak.deb"]) + Self.ordinaryDelivery
        )
        let library = await tweaksStore(
            printing: Self.tweakLine(filename: "some-tweak.deb", name: "Some Tweak"), loaded: false
        )
        guard let app = s.apps.first else { return XCTFail("fixture produced no app") }
        XCTAssertEqual(
            app.tweaks, ["some-tweak.deb"],
            "premise: the app has a tweak attached, which is what the pane loads the "
            + "library for — an empty list is a case that correctly loads nothing"
        )

        let (_, window) = hosted(
            Harness(app: app, store: s, artwork: ArtworkStore(runner: s.runner),
                    devices: await devicesStore(), tweaks: library,
                    staged: StagedIPAStore(runner: s.runner),
                    appStore: AppStoreStore(runner: s.runner, shell: NoAdminShell())),
            width: 1200, height: 700
        )
        _ = window
        _ = try await waitUntil(!library.tweaks.isEmpty)
        XCTAssertEqual(
            library.tweaks.map(\.displayName), ["Some Tweak"],
            "the pane must load the tweak library itself — without it these rows read "
            + "as filenames until the user happens to open the Attach sheet"
        )
    }

    /// A `--tweaks` that fails must cost one subprocess, not one per click.
    ///
    /// The CLI missing from PATH is documented for this GUI, and it leaves the
    /// library empty — indistinguishable, to a `tweaks.isEmpty` gate, from a
    /// store nobody has loaded. That gate re-spawned the doomed subprocess on
    /// every navigation onto an app with tweaks attached, and re-raised the
    /// error banner with it, because the per-app reset task nils `tweakError`
    /// on each app change. Five apps clicked through, five banners.
    func test_a_failed_library_load_is_not_retried_on_every_app_switch() async throws {
        let counter = spawnCounter()
        let library = countingTweaksStore(counter: counter, failing: true)
        try await walkThreeApps(library: library, counter: counter)
        XCTAssertEqual(
            spawns(counter), 1,
            "a failed library load must not be retried on every sidebar click — each "
            + "retry is a doomed subprocess and a re-raised error banner"
        )
    }

    /// The same for a library that is genuinely empty.
    ///
    /// A `config.yaml` listing tweaks whose `tweaks_dir` has moved, or whose
    /// .debs were deleted, produces a perfectly successful `--tweaks` with
    /// nothing in it — forever. Nothing is on screen to notice the re-scan, so
    /// this one is silent where the failing case at least shows a banner.
    func test_a_genuinely_empty_library_is_not_re_read_on_every_app_switch() async throws {
        let counter = spawnCounter()
        let library = countingTweaksStore(counter: counter)
        try await walkThreeApps(library: library, counter: counter)
        XCTAssertEqual(
            spawns(counter), 1,
            "an empty tweak library reads as \"not loaded yet\" to a result-shaped gate, "
            + "so the pane re-ran --tweaks on every sidebar click with nothing to show "
            + "for it"
        )
    }
}

// MARK: - The join behind those rows
//
// `app.tweaks` is a list of filenames out of config.yaml and the library is a
// list of records out of the tweaks directory; the rows are the join of the
// two. Tested without hosting anything, because the rule is not a layout
// property: which rows exist, what each one reads, and what happens to the
// halves that do not match.

extension AppDetailLayoutTests {

    private static func record(
        _ filename: String, name: String? = nil, version: String? = nil,
        section: String? = nil, author: String? = nil
    ) -> TweakRecord {
        TweakRecord(filename: filename, sizeBytes: 19154, usedBy: ["example"],
                    name: name, version: version, section: section, author: author)
    }

    func test_a_matched_filename_takes_the_records_name_and_detail() {
        let rows = AppDetailView.attachedTweaks(
            ["com.example.sometweak_1.7.20_iphoneos-arm64.deb"],
            from: [Self.record("com.example.sometweak_1.7.20_iphoneos-arm64.deb", name: "Some Tweak",
                               version: "1.7.20", section: "Tweaks", author: "example")]
        )
        XCTAssertEqual(rows.map(\.displayName), ["Some Tweak"])
        XCTAssertEqual(rows.first?.detailLine, "1.7.20 · Tweaks · example")
        XCTAssertFalse(rows.first?.isUnnamed ?? true,
                       "a named row is set in the body face, not the filename face")
    }

    /// The degradation that matters. A .deb the CLI could not parse has no
    /// record at all, and dropping its row would hide a tweak that is attached
    /// — the build will still ship it, and the pane would stop saying so.
    func test_an_unmatched_filename_keeps_its_row_and_falls_back() {
        let rows = AppDetailView.attachedTweaks(
            ["mystery.deb"], from: [Self.record("something.else.deb", name: "Something Else")]
        )
        XCTAssertEqual(rows.map(\.filename), ["mystery.deb"],
                       "an attached tweak must not vanish because the library cannot describe it")
        XCTAssertEqual(rows.first?.displayName, "mystery.deb")
        XCTAssertNil(rows.first?.detailLine, "a lonely separator is worse than no line")
        XCTAssertTrue(rows.first?.isUnnamed ?? false, "the headline is a filename, so: mono")
    }

    /// The library is the whole tweaks directory — fifty .debs for a config
    /// that attaches three. Iterating it instead of the app's list would list
    /// every tweak the user owns under an app that uses one of them, and the
    /// detach button beside each would offer to remove what was never attached.
    ///
    /// Order is asserted here too, and it is the app's: config.yaml's order is
    /// the one the user (or `--attach-tweak`) chose, and it is also the order
    /// the tweaks are injected in.
    func test_a_library_record_for_an_unattached_file_is_not_a_row() {
        let rows = AppDetailView.attachedTweaks(
            ["zzz.deb", "aaa.deb"],
            from: [
                Self.record("aaa.deb", name: "First"),
                Self.record("unattached.deb", name: "Not This One"),
                Self.record("zzz.deb", name: "Last"),
            ]
        )
        XCTAssertEqual(rows.map(\.displayName), ["Last", "First"],
                       "the app's list decides both which rows exist and their order")
        XCTAssertFalse(rows.contains { $0.filename == "unattached.deb" })
    }

    /// Two builds of one tweak attached at once — `1.1.8` and `1.1.8-jailed`,
    /// which is what this library actually holds. Both rows read `BTweak`
    /// over the same version, so the only thing separating them is the filename,
    /// and the control beside them detaches.
    func test_rows_that_share_a_headline_put_their_filenames_on_screen() {
        let plain = "com.example.btweak_1.1.8_iphoneos-arm64.deb"
        let jailed = "com.example.btweak_1.1.8-jailed_iphoneos-arm64.deb"
        let rows = AppDetailView.attachedTweaks(
            [plain, jailed],
            from: [Self.record(plain, name: "BTweak", version: "1.1.8"),
                   Self.record(jailed, name: "BTweak", version: "1.1.8")]
        )
        XCTAssertEqual(rows.map(\.displayName), ["BTweak", "BTweak"],
                       "premise: the headlines really are identical")
        XCTAssertEqual(rows.map(\.detailLine), ["1.1.8", "1.1.8"],
                       "premise: the second line does not separate them either")
        XCTAssertEqual(rows.compactMap(\.filenameLine), [plain, jailed],
                       "the row a tooltip cannot identify has to say which file it is")
    }

    /// The ordinary row keeps its filename in the detach tooltip. Showing it
    /// always would put back the filename line this change removed.
    func test_a_row_with_a_headline_of_its_own_shows_no_filename_line() {
        let rows = AppDetailView.attachedTweaks(
            ["com.example.sometweak_1.7.20_iphoneos-arm64.deb", "other.deb"],
            from: [Self.record("com.example.sometweak_1.7.20_iphoneos-arm64.deb", name: "Some Tweak"),
                   Self.record("other.deb", name: "Something Else")]
        )
        XCTAssertEqual(rows.compactMap(\.filenameLine), [],
                       "two rows with different names identify themselves already")
    }

    /// A filename headline is not repeated under itself. The collision needs a
    /// config.yaml listing one .deb twice, which is hand-editable and reaches
    /// here as two rows with the same fallback headline.
    func test_an_unnamed_row_does_not_print_its_filename_twice() {
        let rows = AppDetailView.attachedTweaks(["dup.deb", "dup.deb"], from: [])
        XCTAssertEqual(rows.map(\.displayName), ["dup.deb", "dup.deb"],
                       "premise: both rows fall back to the same filename headline")
        XCTAssertEqual(rows.compactMap(\.filenameLine), [],
                       "the headline is already the filename; a second copy says nothing")
    }
}
