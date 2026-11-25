import SwiftUI
import XCTest
@testable import Graft

/// The sidebar's device rows: that they are drawn, and that they stay the size
/// of a row.
///
/// This file replaces `DevicesLayoutTests`, which measured the Devices *pane* —
/// a view that no longer exists. That file's property ("the pane fits the
/// window") is measured far more thoroughly by `DeviceDetailLayoutTests`
/// against the per-device pane, so keeping it alive under a new name would have
/// been a rename rather than a test. What genuinely needed a new guard is the
/// surface that took the device rows: this column, which now carries a device
/// name — unbounded user input — on every screen rather than on one pane.
///
/// **The most useful thing measured here is a negative result, and it is
/// recorded rather than hidden: a sidebar row cannot take this window down the
/// way a pane can.** Three injections into `DeviceSidebarRow`, each the exact
/// shape of a defect this codebase has actually shipped, and all three left
/// every width and out-of-bounds assertion below green:
///
///  - `.lineLimit(1)` removed from the 130-character name fixture — the
///    injection that took the *pane* to 1391pt in a 700pt window. The row
///    stayed **44pt** and the window stayed 900. A `List` row truncates text by
///    default, so the `lineLimit` here is a statement of intent rather than the
///    thing holding the line.
///  - A plain `.fixedSize()` on the name — the injection that demanded 1796pt
///    of width from inside `DeviceAppsSection`. No change: **900pt in a 900pt
///    window**.
///  - `.frame(minWidth: 2000)` on the whole row. Also no change.
///
/// The mechanism is worth knowing, because it decides what a guard on this
/// column can be: a `List` cell is *one* `NSView` (`ListTableCellView`), sized
/// by the enclosing `NSTableView` at the column's width, and everything SwiftUI
/// draws inside it is invisible to an NSView walk and clipped to that cell. The
/// window is structurally insulated from row content in a way the detail pane —
/// where each section is laid out by SwiftUI directly into the split view — is
/// not.
///
/// So the assertions that actually discriminate here are the other two, and
/// both were made to fail on purpose before they were trusted:
///
///  - **Row count**, read from the backing `NSTableView`. Dropping the
///    `ForEach` over `deviceErrors` from `SidebarView` failed two cases (12 rows
///    where 13 were expected, 11 where 12 were); dropping the empty-state row
///    failed a third (11 where 12 were). **Those figures are from when Library
///    had three rows**, before Cache; the mutations still fail the same cases
///    and every number in them is now one higher. Kept as measured rather than
///    silently re-based, since nothing about the argument turns on the
///    absolute count. Without this, every measurement in the
///    file would pass against a column with no device rows in it at all —
///    exactly how `WindowLayoutTests`' first version managed to guard nothing.
///  - **Row height.** A correct device row is 44pt and an unreadable device's is
///    42; adding one more line of text to the row takes it to 60 and fails
///    seven cases against the 52pt ceiling. That is the real bound on what a row
///    may become, since a row that grows pushes the app list, Library and
///    Activity down and off the bottom of a 560pt column.
///
/// The width and out-of-bounds pair is kept anyway, at the cost of a few
/// milliseconds: it is a guard on the *window* rather than on a row — a column
/// width constant, a split view nested by accident (`WindowLayoutTests` records
/// that one, 120pt past the right edge) — and this file hosts the real
/// `SidebarView` inside a real `NavigationSplitView`, which is where such a
/// mistake would show. It is not a guard on anything a row does, and it should
/// not be read as one.
///
/// **Nor does any case here measure the length of a string, and two of the docs
/// below used to say otherwise.** Swept rather than argued: a device name driven
/// at 1, 20, 60, 130, 400, 2 000 and 20 000 characters, in prose,
/// hyphen-separated, slash-separated and single-unbroken-run shapes, leaves the
/// device row at **44pt** and the window at 900 × 592pt with 0pt out of bounds
/// and 13 list rows — all 28 combinations identical to the digit. (The sweep
/// was run when Library had three rows and reported 12; the row count is the
/// whole column, so the Cache row moved it by one. Nothing else in the sweep
/// depends on it.)
///
/// The unreadable row is the same story with more of it. The CLI's own sentence
/// about the phone lives on that row's `help` and is never laid out at all, so
/// at 1, 130, 2 000 and 20 000 characters in the same four shapes the row stays
/// **42pt**; the UDID, which *is* rendered, is `lineLimit(1)` and
/// middle-truncated, and the row stays 42pt from a 4-character UDID to a
/// 4 000-character one.
///
/// So what the two discriminating assertions actually guard: the height ceiling
/// guards the row's *shape* — that it is two lines of text and not three, which
/// the 60pt measurement above is the evidence for — and the row count guards the
/// column being populated at all. Neither is a guard on what the user typed into
/// Settings or on what the CLI wrote, and no assertion in this file is.
@MainActor
final class SidebarDeviceLayoutTests: XCTestCase {

    // MARK: - Fixtures

    private static func deviceJSON(
        udid: String = "00008030-0000000000000003",
        name: String = "iPhone 13 Pro",
        model: String = "iPhone14,2",
        iosVersion: String = "16.3.1",
        connection: String = "USB",
        liveContainer: String? = nil
    ) -> String {
        let lc = liveContainer.map { "\"\($0)\"" } ?? "null"
        return #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"\#(model)","ios_version":"\#(iosVersion)","connection_type":"\#(connection)","livecontainer_bundle_id":\#(lc)}\n"#
    }

    private static let twoDevices =
        deviceJSON()
        + deviceJSON(
            udid: "00008030-0000000000000001", name: "iPhone 15 Pro",
            model: "iPhone16,1", iosVersion: "26.1", connection: "Network",
            liveContainer: "com.kdt.livecontainer.ABCDE12345"
        )
        + #"{"type":"devices-end","count":2,"failed":0}\n"#

    private static let noDevices = #"{"type":"devices-end","count":0,"failed":0}\n"#

    /// A phone that answered usbmux and then would not describe itself, with
    /// the CLI's own sentence attached — the row's `help` text, and the one
    /// string on it whose length is not ours.
    ///
    /// **It is never laid out.** `UndescribableDeviceRow` renders the UDID and a
    /// constant second line and puts this sentence on a tooltip, so nothing about
    /// its length can move a measurement here: at 1, 130, 2 000 and 20 000
    /// characters, in four wrap shapes, the row is 42pt in all sixteen. It is
    /// realistic input for the decoder and for the `help`, which is what the
    /// fixture is for; it is not a load on the layout.
    ///
    /// No apostrophes in the message, and that is a property of the *harness*
    /// rather than of the row: these fixtures go through `sh -c "printf '…'"`,
    /// so a single quote ends the string early and the event decodes to
    /// nothing. It fails loudly — the premise assertion below caught exactly
    /// that on the first run of this file — but the failure reads as "the store
    /// is broken" rather than "the fixture is unquotable", so it is worth
    /// naming here.
    private static let unreadableDevice =
        #"{"type":"device-error","udid":"00008130-001A2B3C0123456D","error":"lockdown query failed: device is passcode locked; unlock it and tap Trust This Computer, then try again"}\n"#
        + #"{"type":"devices-end","count":0,"failed":1}\n"#

    private func sh() -> CLIRunner {
        CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
    }

    private func devicesStore(printing lines: String) async -> DevicesStore {
        let store = DevicesStore(runner: sh())
        _ = try? await store.load(argsOverride: ["-c", "printf '\(lines)'"])
        return store
    }

    /// Two configured apps, so the column is measured with the list it actually
    /// ships beside rather than with the device rows alone.
    private func configStore() async -> ConfigStore {
        let apps =
            #"{"type":"app","name":"example","bundle_id":"com.example.app","preferred_version":"latest","tweaks":["a.deb"],"display_name":"Example+","delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n"#
            + #"{"type":"app","name":"delta","bundle_id":"com.delta.app","preferred_version":"44.7.0","tweaks":["v.deb"],"display_name":null,"delivery":"sideload","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n"#
        let store = ConfigStore(runner: sh(), cliArgs: ["-c", "printf '\(apps)'"], notify: { _ in })
        _ = try? await store.loadApps()
        return store
    }

    // MARK: - Harness

    /// The column in the container it ships in — a split view's sidebar.
    /// Measured outside one, a `List` never misbehaves, which is how the
    /// deleted Devices pane's first layout guard managed to find nothing.
    ///
    /// The selection is real `@State` rather than `.constant(nil)`, because a
    /// selected row draws differently: `DeviceSidebarRow` swaps its quiet text
    /// to `onAccentMuted` on the fill, and a harness that could never select
    /// one would measure only half the row's states.
    private struct Harness: View {
        @ObservedObject var config: ConfigStore
        @ObservedObject var devices: DevicesStore
        @ObservedObject var artwork: ArtworkStore
        var initialSelection: SidebarItem?

        var body: some View {
            StatefulHarness(
                config: config, devices: devices, artwork: artwork,
                selection: initialSelection
            )
        }
    }

    private struct StatefulHarness: View {
        @ObservedObject var config: ConfigStore
        @ObservedObject var devices: DevicesStore
        @ObservedObject var artwork: ArtworkStore
        @State var selection: SidebarItem?

        var body: some View {
            NavigationSplitView {
                SidebarView(
                    store: config, selection: $selection,
                    artwork: artwork, devices: devices,
                    appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell())
                )
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                Text("detail")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
    }


    private func host(
        config: ConfigStore,
        devices: DevicesStore,
        selection: SidebarItem? = nil,
        width: CGFloat,
        height: CGFloat
    ) -> HostedLayout {
        let view = Harness(
            config: config, devices: devices,
            // Never loaded, so every `image(for:)` answers nil and each app row
            // measures the lettered tile. Same choice `DeviceDetailLayoutTests`
            // makes and for its reason: the tile and real artwork occupy the
            // identical frame, so an icon cannot be what pushes this column
            // past its width, and a fixture that fetched artwork would put a
            // subprocess inside a layout measurement.
            artwork: ArtworkStore(runner: config.runner),
            initialSelection: selection
        )
        return laidOutForMeasurement(view, width: width, height: height)
    }

    /// Scaffolding that is in the view tree but never drawn, and so cannot be
    /// "clipped" however it is positioned. One entry, measured rather than
    /// assumed, and the same one `DeviceDetailLayoutTests` records: SwiftUI
    /// parks a `KeyViewProxy` for the key-view (tab focus) loop at a negative
    /// y inside the sidebar's hosting view even when the column is laid out
    /// perfectly. This column has a focusable control on it — the retry button
    /// on an unreadable device's row — so it is reachable here too. Counting it
    /// is a red CI on a correct change, which is the fastest way to get a
    /// layout guard deleted.
    private static let notDrawn: Set<String> = ["KeyViewProxy"]

    /// The furthest any visible descendant is laid out outside the window —
    /// above it, left of it, or past its right edge — and what it was.
    private func worstOutOfBounds(_ hosted: HostedLayout) -> (points: CGFloat, view: String) {
        var worst: CGFloat = 0
        var who = "nothing"
        func walk(_ view: NSView, _ path: String) {
            guard !view.isHidden else { return }
            guard !Self.notDrawn.contains(String(describing: type(of: view))) else { return }
            let frame = view.convert(view.bounds, to: hosted.root)
            if frame.width > 1, frame.height > 1 {
                let out = max(-frame.minY, -frame.minX, frame.maxX - hosted.contentWidth)
                if out > worst {
                    worst = out
                    who = "\(path) at \(frame)"
                }
            }
            for (i, sub) in view.subviews.enumerated() {
                walk(sub, path + "/" + String(describing: type(of: sub)) + "[\(i)]")
            }
        }
        walk(hosted.root, "root")
        return (worst, who)
    }

    /// How many rows the sidebar's list actually laid out.
    ///
    /// **This is the anti-vacuity check, and it is a real one rather than a
    /// premise assertion about the store.** A `List` in a hosted window is
    /// backed by an `NSTableView` subclass (`SwiftUIOutlineListView`), and its
    /// `numberOfRows` counts section headers *and* cells — so a sidebar that
    /// silently rendered no device rows would report a smaller number, and
    /// every width assertion in this file would pass while measuring a column
    /// the app never draws. That is exactly the failure `WindowLayoutTests`
    /// records from its own first version.
    ///
    /// Deliberately not an accessibility or text probe: SwiftUI populates no
    /// accessibility attributes here, so a search for "iPhone 15 Pro" finds
    /// nothing whatever is on screen. The walk itself now lives once, in
    /// `ClickHarness.backingTableView(in:)`, after a fourth copy of it was
    /// nearly written for the Cache pane; this keeps only the entry point that
    /// knows about the shared `HostedLayout` this file measures against.
    private func listTable(_ hosted: HostedLayout) -> NSTableView? {
        backingTableView(in: hosted.root)
    }

    private func listRowCount(_ hosted: HostedLayout) -> Int? {
        listTable(hosted)?.numberOfRows
    }

    /// How tall each device row is laid out.
    ///
    /// Row 0 is the "Devices" section header and the device rows follow it, so
    /// the section this file is about is `1...count`. Read through
    /// `rect(ofRow:)` rather than by walking `subviews`, which is both the
    /// documented API and stable against AppKit reordering its row views.
    private func deviceRowHeights(_ hosted: HostedLayout, count: Int) -> [CGFloat] {
        guard count > 0, let table = listTable(hosted), table.numberOfRows > count else {
            return []
        }
        return (1...count).map { table.rect(ofRow: $0).height }
    }

    /// The fixed part of the sidebar with two apps configured: the Devices
    /// header, the Apps header and its two rows, the Library header and its
    /// four rows, the Activity header and its two rows.
    ///
    /// Written as a sum rather than a literal so that a row added to Library or
    /// Activity later fails loudly here instead of quietly re-baselining what
    /// "the device rows rendered" means. Library went from three to four when
    /// the Cache row landed, and it did fail loudly — all nine `assertFits`
    /// cases, each reporting one row more than expected. That is the whole
    /// automated guard
    /// on the Cache row being in the column at all: nothing here can read a
    /// row's *label* (SwiftUI populates no accessibility attributes in this
    /// window — see the type doc), so what a count can say is that the column
    /// has the rows the app is supposed to draw, and deleting the row from
    /// `SidebarView` fails these same eight cases from the other direction.
    private static let fixedRows = 1 + (1 + 2) + (1 + 4) + (1 + 2)

    /// A device row's ceiling.
    ///
    /// Measured, at every window size in this file: a correct device row is
    /// **44pt** (two lines of text plus 2pt of padding a side), an unreadable
    /// device's row is 42, and adding one more line of text to either takes it
    /// to **60**. So 52 sits between them with 8pt of slack for a font or
    /// metrics change — enough that this cannot go red on a correct change, and
    /// not enough to launder a third line.
    private static let rowHeightCeiling: CGFloat = 52

    private func assertFits(
        config: ConfigStore,
        devices: DevicesStore,
        selection: SidebarItem? = nil,
        deviceRows: Int,
        _ label: String,
        width: CGFloat = Metrics.windowMinWidth,
        height: CGFloat = Metrics.windowMinHeight,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let hosted = host(
            config: config, devices: devices, selection: selection,
            width: width, height: height
        )
        XCTAssertEqual(
            listRowCount(hosted), Self.fixedRows + deviceRows,
            "the sidebar (\(label)) laid out a different number of rows than the fixture "
            + "should produce — the column being measured is not the one described, so "
            + "the assertions below prove nothing about it",
            file: file, line: line
        )
        // The assertion that actually guards this row's `lineLimit`s. See the
        // type doc for why the width pair below cannot: a `List` cell is one
        // NSView, sized by the table, so nothing a row asks for horizontally
        // ever reaches the window.
        for (index, rowHeight) in deviceRowHeights(hosted, count: deviceRows).enumerated() {
            XCTAssertLessThanOrEqual(
                rowHeight, Self.rowHeightCeiling,
                "device row \(index) (\(label)) is \(Int(rowHeight))pt tall — a row that "
                + "wraps instead of truncating pushes the app list, Library and Activity "
                + "down and off the bottom of the column, and a device name is unbounded "
                + "user input",
                file: file, line: line
            )
        }
        XCTAssertLessThanOrEqual(
            hosted.root.bounds.width, hosted.contentWidth + 2,
            "the window (\(label)) demanded \(Int(hosted.root.bounds.width))pt of width in a "
            + "\(Int(hosted.contentWidth))pt window — a sidebar row asking for more width than "
            + "the column has is what clipped Build & Install off the app pane, 120pt "
            + "past the edge",
            file: file, line: line
        )
        let out = worstOutOfBounds(hosted)
        XCTAssertLessThanOrEqual(
            out.points, 2,
            "part of the window (\(label)) is laid out \(Int(out.points))pt outside it, "
            + "where it is clipped and unreachable: \(out.view)",
            file: file, line: line
        )
    }

    // MARK: - The column fits

    /// The ordinary bench: two phones, one with LiveContainer and one without.
    func test_the_sidebar_fits_with_two_devices() async {
        let devices = await devicesStore(printing: Self.twoDevices)
        XCTAssertEqual(devices.devices.count, 2, "premise: both phones reached the store")
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 2, "two devices"
        )
    }

    /// With one of them selected, which is the state the row's second line
    /// changes color for.
    func test_the_sidebar_fits_with_a_device_selected() async {
        let devices = await devicesStore(printing: Self.twoDevices)
        await assertFits(
            config: configStore(), devices: devices,
            selection: .device("00008030-0000000000000001"),
            deviceRows: 2, "a selected device"
        )
    }

    /// The column's one unbounded input — a device name is whatever the user
    /// typed into Settings on the phone — in its ordinary prose shape.
    ///
    /// **The length is the fixture, not the claim**, and this doc used to be only
    /// the claim. `DeviceSidebarRow` renders the name `lineLimit(1)` and
    /// tail-truncated, and this harness measures the row at 44pt whether the name
    /// is one character or twenty thousand; the type doc records the sweep.
    ///
    /// What the case is for is the row's arrangement: the name shares an `HStack`
    /// with the silhouette, a `Spacer(minLength: 4)` and the live dot, and a long
    /// name is the input that would expose the `lineLimit` being dropped or a
    /// second line arriving under it. The assertion that would catch that is the
    /// height ceiling — 44pt against 52, going to 60 with a third line — and not
    /// the width pair, for the reason the type doc gives.
    func test_the_sidebar_fits_with_a_very_long_device_name() async {
        let name = "Owners iPhone 13 Pro Max, the one on the bench by the window, "
            + "do not wipe this one, it has the good jailbreak on it"
        let devices = await devicesStore(
            printing: Self.deviceJSON(name: name) + #"{"type":"devices-end","count":1,"failed":0}\n"#
        )
        XCTAssertEqual(
            devices.devices.first?.name, name,
            "premise: the long name actually reached the row"
        )
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 1, "a very long device name"
        )
    }

    /// The same input with genuinely no wrap opportunity in it — **and the
    /// fixture had to be changed to make that true.**
    ///
    /// It was hyphen-separated ("Owners-iPhone-13-Pro-Max-…") under a comment
    /// claiming a single unbreakable run, and a hyphen is a line-break
    /// opportunity. Measured with CoreText (`CTTypesetterSuggestLineBreak`
    /// against `SuggestClusterBreak`, mono 11pt): offered 260pt — this column's
    /// ideal width — that string breaks after a hyphen at character 36 of 134,
    /// where the character-break fallback would not have come until 38. So it was
    /// prose with small words, and this case was the one above it a second time.
    /// The name below has no separator at all, and the same measurement says so:
    /// word break equals cluster break at both 120 and 260pt, which is CoreText
    /// reporting nothing to break at.
    ///
    /// **What it still cannot show is that the run's length matters**, here least
    /// of all: the type doc records why a row's demands never reach the window,
    /// and the sweep behind it holds this row at 44pt to 20 000 characters of
    /// unbroken letters. The case is kept because the shape is real — a phone
    /// named without spaces is an ordinary thing to find — and because it is the
    /// input a dropped truncation mode would show up in.
    func test_the_sidebar_fits_with_a_device_name_that_cannot_wrap() async {
        // No spaces, no hyphens, no slashes — measured above as having no break
        // opportunity at all, which the old hyphenated fixture did not have.
        let name = "OwnersiPhone13ProMaxTheOneOnTheBenchByTheWindow"
            + "DoNotWipeThisOneItHasTheGoodJailbreakOnItAndTheCableIsFine"
        let devices = await devicesStore(
            printing: Self.deviceJSON(name: name) + #"{"type":"devices-end","count":1,"failed":0}\n"#
        )
        XCTAssertEqual(devices.devices.first?.name, name, "premise: the name reached the row")
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 1, "an unwrappable device name"
        )
    }

    /// A phone that enumerated and would not describe itself: a UDID, an amber
    /// dot carrying the CLI's own sentence, and a retry. It is a row here
    /// rather than the card it used to be — that card measured 3413pt and
    /// blanked the pane it was supposed to be explaining.
    ///
    /// What this measures of that history is the *shape* the remedy took: two
    /// one-line strings and a tooltip, which is a 42pt row. The CLI's sentence
    /// cannot be what breaks it — see the fixture's own doc, where its length is
    /// measured as inert because the row never lays it out — and neither can the
    /// UDID, which is `lineLimit(1)`. What would break it is the tooltip becoming
    /// a third line again, and that is what the 52pt ceiling catches.
    func test_the_sidebar_fits_with_an_unreadable_device() async {
        let devices = await devicesStore(printing: Self.unreadableDevice)
        XCTAssertEqual(
            devices.deviceErrors.count, 1,
            "premise: the device-error fixture reached the store, or this measures the "
            + "empty state instead of the row"
        )
        XCTAssertTrue(devices.devices.isEmpty, "premise: nothing was describable")
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 1, "one unreadable device"
        )
    }

    /// Both kinds at once, which is what a bench with a locked spare looks
    /// like — and the case where the two `ForEach`es render side by side.
    func test_the_sidebar_fits_with_a_listed_device_and_an_unreadable_one() async {
        let json = Self.deviceJSON()
            + #"{"type":"device-error","udid":"00008130-001A2B3C0123456D","error":"connection refused"}\n"#
            + #"{"type":"devices-end","count":1,"failed":1}\n"#
        let devices = await devicesStore(printing: json)
        XCTAssertEqual(devices.devices.count, 1, "premise: one phone was described")
        XCTAssertEqual(devices.deviceErrors.count, 1, "premise: one was not")
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 2, "one listed, one unreadable"
        )
    }

    /// Nothing plugged in — the empty-state row, kept so a fix that rescues the
    /// populated column by breaking the empty one is caught rather than
    /// shipped.
    func test_the_sidebar_fits_with_no_devices() async {
        let devices = await devicesStore(printing: Self.noDevices)
        XCTAssertTrue(devices.devices.isEmpty, "premise: nothing is connected")
        XCTAssertTrue(devices.hasLoaded, "premise: the empty row says 'No devices connected'")
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 1, "no devices"
        )
    }

    /// Wide as well as small: the recorded defects here were width-driven, and
    /// a column that only fits at one size is not fixed.
    func test_the_sidebar_fits_at_full_screen_width() async {
        let devices = await devicesStore(printing: Self.twoDevices)
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 2, "two devices, full screen",
            width: 1470, height: 922
        )
    }

    /// Below the app's own minimum, however that minimum is later retuned. A
    /// window cannot normally get here; the property under test is "the content
    /// fits", and a layout that only holds above a magic number is what failed
    /// last time. Same reasoning as `WindowLayoutTests`.
    func test_the_sidebar_fits_below_the_window_minimum() async {
        let devices = await devicesStore(printing: Self.twoDevices)
        await assertFits(
            config: configStore(), devices: devices,
            deviceRows: 2, "two devices, below the minimum",
            width: Metrics.windowMinWidth - 200, height: Metrics.windowMinHeight - 60
        )
    }

    // MARK: - What the row says

    /// The second line, as a value rather than through the rendered view.
    ///
    /// Same reason `DeviceStatStrip`'s cell values and `DeliveryKind.stateLabel`
    /// are functions on a type: asserting on a SwiftUI body is expensive and
    /// brittle, and the decision worth pinning here is a pure function of the
    /// record.
    func test_the_row_says_the_ios_version_and_the_livecontainer_state() async {
        let devices = await devicesStore(printing: Self.twoDevices)
        let withoutLC = try? XCTUnwrap(devices.devices.first)
        let withLC = devices.devices.last
        XCTAssertEqual(
            withoutLC.map(DeviceSidebarRow.subtitle(for:)),
            "iOS 16.3.1 · no LC",
            "a phone with no LiveContainer has to say so — nil on the record means the "
            + "scan looked and found none, not that it is unknown, and a phone without "
            + "it cannot take a LiveContainer delivery at all"
        )
        XCTAssertEqual(
            withLC.map(DeviceSidebarRow.subtitle(for:)),
            "iOS 26.1 · LC"
        )
    }
}

/// A phone appearing must not navigate you away from the phone you are looking
/// at.
///
/// This list is *live* — rows arrive and leave on a five-second poll — which no
/// other section of this sidebar is, and a row inserted above the selected one
/// shifts every index below it. If the selection followed the index rather than
/// the phone, plugging a second handset into the bench would silently swap the
/// pane under the user: they would go on reading a decrypt readiness card, a
/// storage figure and a delivery list belonging to a different device, with the
/// only clue being a name in a header they had no reason to re-read.
///
/// **Written after seeing exactly that in the running app, and it turned out to
/// be the harness rather than the app** — which is the reason this file says so
/// out loud. Driving the sidebar through Accessibility (`set selected of row 2
/// to true`, the documented way to navigate this app in a script) sets
/// `NSTableView`'s selected *index* directly, behind SwiftUI's binding. When a
/// second phone then appeared and was listed first, the highlight stayed on row
/// 2 and the pane switched to the new phone. Through the real binding — which
/// is what a click goes through — the tag is what is preserved: measured here,
/// the row count goes 11 → 12 and `selectedRow` moves 1 → 2 to follow the same
/// phone, and the binding never changes. (It was 10 → 11 until the Cache row
/// joined Library; the counts below are of the whole column, so every fixed row
/// is in them, and only the two device rows are what this case is about.) So a
/// script that drives this app via
/// AX cannot be used to judge selection behavior; the assertion below is what
/// judges it.
@MainActor
final class SidebarDeviceSelectionTests: XCTestCase {

    /// Somewhere the test can read the binding from. `@State` inside the
    /// harness would be private to it, and `.constant()` cannot record a write.
    final class Box: ObservableObject {
        @Published var selection: SidebarItem?
        init(_ selection: SidebarItem?) { self.selection = selection }
    }

    private struct Harness: View {
        @ObservedObject var config: ConfigStore
        @ObservedObject var devices: DevicesStore
        @ObservedObject var box: Box

        var body: some View {
            NavigationSplitView {
                SidebarView(
                    store: config, selection: $box.selection,
                    artwork: ArtworkStore(runner: config.runner), devices: devices,
                    appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell())
                )
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                Text("detail")
            }
        }
    }

    private static let fifteen = #"{"type":"device","udid":"UDID-15","name":"iPhone 15 Pro","model":"iPhone16,1","ios_version":"26.1","connection_type":"Network","livecontainer_bundle_id":"com.kdt.lc"}\n{"type":"devices-end","count":1,"failed":0}\n"#

    /// The new phone is listed *first*, which is not a contrivance: usbmux
    /// enumerated a newly-plugged handset ahead of the one already on the bus
    /// while this was being verified against real hardware.
    private static let both = #"{"type":"device","udid":"UDID-13","name":"iPhone 13 Pro","model":"iPhone14,2","ios_version":"16.3.1","connection_type":"USB","livecontainer_bundle_id":null}\n"# + fifteen

    func test_a_phone_appearing_above_the_selected_one_does_not_move_the_selection() async {
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let devices = DevicesStore(runner: sh)
        _ = try? await devices.load(argsOverride: ["-c", "printf '\(Self.fifteen)'"])
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "true"], notify: { _ in })
        let box = Box(.device("UDID-15"))

        let host = NSHostingView(rootView: Harness(config: config, devices: devices, box: box))
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 900, height: 560),
            styleMask: [.titled, .resizable], backing: .buffered, defer: false
        )
        window.contentView = host
        host.frame = NSRect(x: 0, y: 0, width: 900, height: 560)
        settle(Hosted(window: window, size: host.frame.size))

        let before = backingTableView(in: host)
        // One phone and no configured apps: the Devices header and its row, the
        // Apps header alone, the Library header and its four, the Activity
        // header and its two.
        XCTAssertEqual(
            before?.numberOfRows, 2 + 1 + 5 + 3, "premise: one phone, so eleven rows"
        )
        XCTAssertEqual(
            before?.selectedRow, 1,
            "premise: the selected phone is row 1 (row 0 is the Devices header) — "
            + "without this the assertion below could pass on a list that never "
            + "selected anything"
        )

        _ = try? await devices.load(argsOverride: ["-c", "printf '\(Self.both)'"])
        settle(Hosted(window: window, size: host.frame.size))

        let after = backingTableView(in: host)
        XCTAssertEqual(
            after?.numberOfRows, 2 + 1 + 5 + 3 + 1,
            "premise: the second phone's row arrived, or nothing below is being tested"
        )
        XCTAssertEqual(
            after?.selectedRow, 2,
            "the highlight stayed on row 1 while the phone it belongs to moved to row 2 "
            + "— the sidebar is now highlighting a different device than the one the "
            + "pane is showing"
        )
        XCTAssertEqual(
            box.selection, .device("UDID-15"),
            "a phone appearing on the bus navigated the window to it, away from the "
            + "phone the user had open"
        )
    }

}

/// Which section a fixed sidebar row actually navigates to.
///
/// `SidebarDeviceLayoutTests.fixedRows` counts this column's rows, so a row
/// deleted from Library or Activity fails all nine `assertFits` cases there.
/// What a count cannot say is *which* row it is or where it points:
/// `row(.repos, label: "Cache", symbol: Symbols.cacheSection)` has the right
/// count, the right icon and the right words, and sends the user to the Repos
/// pane. Measured: that mutation leaves every row-count case green and is
/// caught here alone.
///
/// **The instrument is `NSTableView.selectedRow`, not a synthetic click.**
/// `SidebarDeviceSelectionTests` above establishes that a `List(selection:)`
/// binding and the table's highlighted index track each other through the tag,
/// which is the whole mechanism this needs: set the binding to
/// `.section(.cache)` and the table highlights the row tagged `.cache`, or
/// highlights nothing (`-1`) if no row carries that tag. A click would prove
/// the same thing more expensively and with a coordinate to get wrong, and
/// `ClickHarness` records that a `List` cell tells a test nothing about its
/// contents anyway — so a click could not read the label it landed on either.
///
/// **Positions are asserted relative to Repos, never as literals.** An absolute
/// index bakes in the number of device rows, app rows and section headers
/// above, so every case here would go red the day a row is added to Devices —
/// a failure that names the wrong file. What is actually being pinned is that
/// Cache is the row immediately after Repos, which is Library's last entry:
/// `SidebarView`'s own comment argues Library over Activity, and this is that
/// argument in a form that fails if someone moves it.
@MainActor
final class SidebarSectionRowTests: XCTestCase {

    /// Somewhere to read and write the selection from — the same shape
    /// `SidebarDeviceSelectionTests.Box` is, and for the same reason: a
    /// `@State` inside the harness is private to it and `.constant()` cannot
    /// be driven.
    final class Box: ObservableObject {
        @Published var selection: SidebarItem?
        init(_ selection: SidebarItem?) { self.selection = selection }
    }

    private struct Harness: View {
        @ObservedObject var config: ConfigStore
        @ObservedObject var devices: DevicesStore
        @ObservedObject var box: Box

        var body: some View {
            NavigationSplitView {
                SidebarView(
                    store: config, selection: $box.selection,
                    artwork: ArtworkStore(runner: config.runner), devices: devices,
                    appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell())
                )
                .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                Text("detail")
            }
        }
    }

    /// The column on screen with `section` selected, and the index the table
    /// highlighted.
    ///
    /// No apps and no phones: this file is about the *fixed* rows, and every
    /// variable row above them only moves the indices the assertions already
    /// refuse to hard-code. `-1` is `NSTableView`'s "nothing is selected", and
    /// it is the answer when the tag does not exist — which is what a deleted
    /// or mistagged row produces.
    private func highlightedRow(selecting section: SidebarSection) async -> Int? {
        let sh = CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
        let devices = DevicesStore(runner: sh)
        _ = try? await devices.load(
            argsOverride: ["-c", #"printf '{"type":"devices-end","count":0,"failed":0}\n'"#]
        )
        let config = ConfigStore(runner: sh, cliArgs: ["-c", "true"], notify: { _ in })
        let box = Box(.section(section))

        let host = NSHostingView(rootView: Harness(config: config, devices: devices, box: box))
        let size = NSSize(width: 900, height: 560)
        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: size),
            styleMask: [.titled, .resizable], backing: .buffered, defer: false
        )
        window.contentView = host
        host.frame = NSRect(origin: .zero, size: size)
        settle(Hosted(window: window, size: size))
        return backingTableView(in: host)?.selectedRow
    }

    /// Cache is a row, it points at `.cache`, and it is the one directly under
    /// Repos.
    ///
    /// Three claims in two numbers. Repos is the premise — if *it* stops
    /// resolving, the comparison below is two `-1`s agreeing with each other,
    /// which is why its index is asserted positive before it is used as an
    /// origin.
    func test_the_cache_row_is_the_last_row_in_library() async {
        let repos = await highlightedRow(selecting: .repos)
        XCTAssertNotNil(repos)
        XCTAssertGreaterThan(
            repos ?? -1, 0,
            "premise: the Repos row itself did not resolve, so it cannot be used as "
            + "the origin for where Cache should be — nothing below would mean anything"
        )
        let cache = await highlightedRow(selecting: .cache)
        XCTAssertEqual(
            cache, (repos ?? -1) + 1,
            "the row tagged .cache is not directly below Repos. -1 means no row in the "
            + "column carries that tag at all (deleted, or tagged as some other "
            + "section); any other number means it moved out of Library's last "
            + "position, which is where the cache belongs — it is an artifact you have "
            + "acquired, like a tweak or a repo, not an activity like Builds"
        )
    }

    /// The negative control for the instrument itself.
    ///
    /// Without it, `highlightedRow` could be returning a number that has
    /// nothing to do with the tag it was handed — an off-by-one in the harness,
    /// or a table that highlights row 0 whatever it is asked. Settings is the
    /// far end of the column, so an instrument that ignores its argument cannot
    /// produce both this answer and the one above.
    func test_the_instrument_follows_the_tag_it_is_given() async {
        let cache = await highlightedRow(selecting: .cache)
        let settings = await highlightedRow(selecting: .settings)
        XCTAssertNotEqual(
            cache, settings,
            "asking for two different sections highlighted the same row, so this "
            + "harness is not reading the tag and neither case above proves anything"
        )
        XCTAssertGreaterThan(
            settings ?? -1, cache ?? -1,
            "Settings is the last row of the last section and Cache is in Library "
            + "above it — if that ordering has inverted, the column is not the one "
            + "these cases describe"
        )
    }
}
