import SwiftUI
import XCTest
@testable import Graft

/// The per-device pane has to fit the pane it is given.
///
/// This is the same guard `DevicesLayoutTests` carries, written before the view
/// it guards rather than after the bug. The reason is recorded in
/// `DeviceDecryptSection`, which is the card it happened to and now lives on
/// this pane: a long sentence with
/// `.fixedSize(horizontal: false, vertical: true)` and no `lineLimit`, *in this
/// pane*, resolved ~110 characters to ~78 stacked lines. The pane asked for
/// 1391pt inside a 700pt window, the excess came off the **top**, and the
/// toolbar and Refresh button were laid out above the window's edge (measured
/// at y = -61 and y = -66 in the running app). With a phone plugged in the
/// whole screen read as blank. The `undescribableDevices` card, same shape,
/// measured 3413pt.
///
/// The mechanism: `fixedSize` pins a `Text` to its ideal height for the width
/// it is *proposed*, and inside this pane the proposed width comes out as
/// effectively nothing. `lineLimit` is what bounds it, because it caps the
/// ideal height whatever width is proposed. So every long sentence on this pane
/// is `lineLimit`-ed, and this file is what stops that being decoration.
///
/// Two properties, and the second is not redundant: the pane fits the window,
/// and nothing in it is laid out outside the window's own bounds. A pane that
/// overflowed *downwards* would be caught by the first alone; the recorded
/// failure overflowed upwards, off the top, which is why the walk is here too.
///
/// **Every assertion here was made to fail on purpose before it was trusted.**
/// A test that guards a layout it cannot catch breaking is worse than no test,
/// because it reads like cover. Measured:
///
///  - Dropping one unbounded 200-character `Text` with `fixedSize` into
///    `DeviceHeader` failed every case below: the pane demanded **2267pt in a
///    560pt window** against a 600pt ceiling, and `NSTitlebarBackgroundView` —
///    the window's own chrome — was laid out at **y = -52**, which is the
///    reported symptom exactly, the top of the window pushed off the top of the
///    window. So the ceiling clears the defect by nearly four times over.
///  - A `.frame(minWidth: 2000)` on the header failed the width pair: **2308pt
///    demanded in a 900pt window**, with 1408pt of it outside.
///
/// Worth recording what did *not* reproduce, since it is the hazard this
/// header most obviously carries: a 130-character device name does not blow the
/// pane out, with or without `lineLimit`. A bare `Text` in this row is willing
/// to truncate. It is `fixedSize` that removes that willingness, which is why
/// the rule for this pane is written as "no unbounded sentence under
/// `fixedSize`" rather than "no long strings".
///
/// That paragraph used to say "nor an unbreakable one", which was true of the
/// pane and false about the fixture — the name it was measured against was
/// hyphen-separated, and hyphens are break opportunities. A name with no
/// separator in it has since been measured too, to 20 000 characters, and it
/// does not blow the pane out either. See
/// `test_the_pane_fits_with_a_device_name_that_cannot_wrap`.
///
/// **Above the divider and below it are not the same place, and the difference
/// decides which assertion can catch you.** The header is measured directly;
/// everything under the divider lives in a `ScrollView`, which absorbs height
/// unboundedly. So when "Your apps on this phone" was added, the injection that
/// took the header down — an unbounded 200-character `Text` under
/// `.fixedSize(horizontal: false, vertical: true)`, dropped into a row —
/// **passed every case in this file**, prose and unbreakable alike. That is not
/// the guard failing; `.fixedSize(horizontal: false, …)` never asks for width,
/// it asks for the *height* its content needs at the width it was proposed, and
/// height below the divider is exactly what a `ScrollView` is for.
///
/// What does reach the window from down there is a **width** demand, and the
/// same injection under a plain `.fixedSize()` (both axes) proves it: the pane
/// demanded **1796pt of width in a 900pt window** with **896pt of it laid out
/// outside**, and ten of the eleven cases below went red — every one that
/// renders a populated section. The eleventh, `no_apps_configured`, correctly
/// stayed green, which is its own small proof that the populated fixtures are
/// what carry the other ten.
///
/// So the rule for a section under the divider is "no unbounded string that can
/// demand width" — no `.fixedSize()` on prose, and `lineLimit` on anything the
/// CLI wrote — and the assertions that enforce it are the width pair, not the
/// height one. `AppDetailLayoutTests` records the same asymmetry one pane over
/// ("its cards live inside a `ScrollView`, so vertical overflow is absorbed").
///
/// **The decrypt card was checked the same way and behaves the same way**, which
/// matters because it is the card that caused all of this — the 1391pt was
/// *its* subtitle. One unbounded ~220-character `Text` under a plain
/// `.fixedSize()`, dropped into `DeviceDecryptSection`: **1606pt of width in a
/// 900pt window with 706pt laid out outside**, and all eighteen cases in this
/// file went red, since every one of them renders that card. At full screen it
/// was the same 1606pt with 136pt outside a 1470pt window. The height
/// assertion stayed green throughout — 592pt against a 600pt ceiling — which is
/// the asymmetry above stated as a measurement rather than an argument.
///
/// **What no case here measures is how long any string in it is, and several of
/// the doc comments below used to be written as though they did.** That was
/// settled by sweeping rather than by argument. Every content knob this file has
/// was driven at 1, 20, 60, 100, 200, 400, 800, 2 000 and 20 000 characters, in
/// four shapes — prose, hyphen-separated, slash-separated, and one unbroken run
/// of letters — and in all 190 combinations the pane measured **900 × 592pt with
/// 0pt laid out out of bounds**, identical to the digit. The knobs: the device
/// name, the delivery summary on each of twelve rows, the Refresh banner's
/// message, a readiness `detail`, `fix` and backend `reason`, a dump note, a
/// transcript line, an installed row's name and bundle id, the number of
/// stripped extension names (0, 5, 20, 60) and the length of the installed list
/// (0, 10, 60, 300 apps). Substituting `"x"` for a fixture gives the same three
/// numbers as substituting twenty thousand characters, so there is no threshold
/// to report: the assertions are indifferent to content length everywhere on
/// this pane.
///
/// The mechanism, per site rather than in general: every long `Text` here is
/// either `lineLimit`-ed with a truncation mode — and truncating is precisely
/// how a `Text` avoids demanding width — or under a vertical-only
/// `.fixedSize()` inside the `ScrollView`, where height is absorbed. The height
/// assertion in particular cannot be reached from content at all: the pane is a
/// `VStack` pinned to the window with the scroll view at the bottom, so anything
/// that grows takes its room out of the scroll area rather than out of the
/// window. It reads 592pt with the error banner's message shrunk to `"x"` and
/// 592pt with that message at its four-line cap.
///
/// So read each case below as **one rendering shape at one window size**, not as
/// one string length. The property they do hold is the structural one above — a
/// child that demands unbounded space, which in this codebase means a stray
/// `.fixedSize()` — and the injections recorded further up are the evidence that
/// property is live. The fixtures stay long because a long string is the input
/// through which such a regression escapes and a short one is not; that is the
/// fixture's job, and it is not a claim the assertion makes.
///
/// **What has *not* been measured, so that nobody reads these docs as a
/// guarantee: the threshold of that tripwire.** Finding it means injecting
/// `.fixedSize()` into a production view and growing a string until the
/// assertion trips — the way `AppDetailLayoutTests` found ~85 characters for the
/// build-target bar — and this pass was scoped to the two test files.
/// What *is* measured is the margin the assertion works in: the pane sits at
/// exactly 900pt in a 900pt window against a 902pt ceiling, so 3pt of excess
/// width demand goes red, while the height ceiling has 8pt of slack that no
/// content can reach.
///
/// Everything is measured inside a `NavigationSplitView`'s detail column, which
/// is where the pane actually ships. `DevicesLayoutTests` records that a pane
/// measured outside one never misbehaves — that is how its first attempt
/// managed to find nothing.
@MainActor
final class DeviceDetailLayoutTests: XCTestCase {

    // MARK: - Fixtures

    private static let udid = "00008030-0000000000000003"

    /// `isDefault` sets `is_default` on the line, which is the chip's whole
    /// input now — `DeviceHeader` reads `device.isDefault` off the decoded
    /// fixture rather than a udid passed down separately, so this is the only
    /// lever the chip cases below need.
    private static func deviceJSON(name: String = "iPhone 13 Pro", isDefault: Bool = false) -> String {
        #"{"type":"device","udid":"\#(udid)","name":"\#(name)","model":"iPhone14,2","ios_version":"16.3.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","is_default":\#(isDefault)}\n{"type":"devices-end","count":1,"failed":0}\n"#
    }

    private static let infoJSON = #"{"type":"device-info","udid":"\#(udid)","reachable":true,"total_bytes":128000000000,"free_bytes":35728093184,"battery_percent":70,"battery_charging":true}\n"#

    private func sh() -> CLIRunner {
        CLIRunner(executable: URL(fileURLWithPath: "/bin/sh"))
    }

    private func devicesStore(printing lines: String) async -> DevicesStore {
        let store = DevicesStore(runner: sh())
        _ = try? await store.load(argsOverride: ["-c", "printf '\(lines)'"])
        return store
    }

    // MARK: - The app section's fixtures

    /// One `app` event, with everything `AppRecord` requires — every field is
    /// non-optional there by design, so a shortened line decodes to nothing and
    /// the section measures an empty list.
    private static func appJSON(_ name: String, displayName: String? = nil) -> String {
        let display = displayName.map { "\"\($0)\"" } ?? "null"
        return #"{"type":"app","name":"\#(name)","bundle_id":"com.example.\#(name)","preferred_version":"latest","tweaks":["a.deb"],"display_name":\#(display),"delivery":"livecontainer","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}\n"#
    }

    /// One `delivery-state` event, as `--delivery-state --device <udid>` emits
    /// it. Deliberately built by hand rather than reused from the `--list`
    /// fixture: the whole point of the section is that these two answers can
    /// differ, so the test must be able to make them differ.
    private static func deliveryJSON(
        app: String, kind: String, summary: String, reasons: [String] = []
    ) -> String {
        let joined = reasons.map { "\"\($0)\"" }.joined(separator: ",")
        return #"{"type":"delivery-state","app":"\#(app)","kind":"\#(kind)","summary":"\#(summary)","reasons":[\#(joined)],"pushed_at":"2026-05-13T16:20:00+00:00"}\n"#
    }

    /// Four configured apps — the realistic shape of a bench: a couple of
    /// long-ish display names among short config keys.
    private static let appsJSON =
        appJSON("bravo", displayName: "Bravo (no ads)")
        + appJSON("delta")
        + appJSON("example", displayName: "Example+")
        + appJSON("charlie", displayName: "Charlie Premium-ish")

    /// This phone's answer for three of those four, spanning every kind that
    /// renders differently: one needing attention with reasons, one stale from
    /// a re-install, one current and quiet. `charlie` is deliberately absent —
    /// an app the CLI described nothing about is a real state and has its own
    /// status line.
    ///
    /// `example` is `never` on purpose. That is this section's whole reason for
    /// existing: the same app is `current` in `--list`'s unscoped answer below,
    /// because it was delivered to a *different* phone.
    private static let statesJSON =
        deliveryJSON(
            app: "example", kind: "never", summary: "Never delivered to a device."
        )
        + deliveryJSON(
            app: "bravo", kind: "stale",
            summary: "An archived build was re-installed; what went into it was never recorded.",
            reasons: ["nothing to compare — push again to re-establish the baseline"]
        )
        + deliveryJSON(
            app: "delta", kind: "current", summary: "Matches the last delivery."
        )

    /// The apps list *plus* an unscoped `delivery-state` for example that
    /// disagrees with the per-device one. `--list` really does emit both, and
    /// this is what a phone-A delivery looks like from phone B's pane.
    private static let listJSON =
        appsJSON
        + deliveryJSON(app: "example", kind: "current", summary: "Matches the last delivery.")

    private func configStore(printing lines: String) async -> ConfigStore {
        let store = ConfigStore(runner: sh(), cliArgs: ["-c", "printf '\(lines)'"], notify: { _ in })
        _ = try? await store.loadApps()
        return store
    }

    /// A delivery store already holding this phone's answer, or one that has
    /// never been asked — the `.pending` case every pane passes through before
    /// its first load lands.
    private func deliveryStore(printing lines: String?) async -> DeviceDeliveryStore {
        let store = DeviceDeliveryStore(runner: sh())
        if let lines {
            await store.load(udid: Self.udid, argsOverride: ["-c", "printf '\(lines)'"])
        }
        return store
    }

    /// An info store already holding a reading, or holding nothing at all —
    /// the locked-phone case, where lockdown declines the query and every cell
    /// in the strip has to fall back to a dash on its own.
    private func infoStore(printing lines: String?) async -> DeviceInfoStore {
        let store = DeviceInfoStore(runner: sh())
        if let lines {
            await store.load(udid: Self.udid, argsOverride: ["-c", "printf '\(lines)'"])
        }
        return store
    }

    /// Both stores the app section reads, loaded, **with their premises
    /// asserted here** so that every case measuring this pane measures a
    /// populated section rather than an empty one.
    ///
    /// This is the check the suite would otherwise be missing, and it is the
    /// exact failure `DevicesLayoutTests` was written after: a pane whose
    /// content never arrived fits every window trivially, and passes looking
    /// identical to a pane that fits because it is correct. The fixtures here
    /// go through `printf` into a real subprocess and are decoded by the real
    /// event decoder, so a typo in a JSON line yields *no* apps and *no*
    /// states — silently, since `String.buildEvents()` drops what it can't
    /// decode. Asserting the counts is what turns that into a red test.
    private func appSectionStores(
        file: StaticString = #filePath, line: UInt = #line
    ) async -> (ConfigStore, DeviceDeliveryStore) {
        let config = await configStore(printing: Self.listJSON)
        let delivery = await deliveryStore(printing: Self.statesJSON)
        XCTAssertEqual(
            config.apps.count, 4,
            "the app fixture never reached the store, so the section measured is empty "
            + "and every assertion about it proves nothing",
            file: file, line: line
        )
        guard case let .ready(states) = delivery.readout(for: Self.udid) else {
            XCTFail(
                "the per-device delivery fixture never loaded, so every row measured "
                + "reads 'Reading this iPhone's delivery log…' instead of a CLI summary",
                file: file, line: line
            )
            return (config, delivery)
        }
        XCTAssertEqual(
            states.count, 3, "three of the four apps should have a state on this phone",
            file: file, line: line
        )
        XCTAssertEqual(
            states["example"]?.kind, .never,
            "premise, and the whole point of the section: example is `current` in the "
            + "unscoped list and `never` on this phone",
            file: file, line: line
        )
        XCTAssertEqual(
            config.deliveryStates["example"]?.kind, .current,
            "premise: the unscoped answer disagrees, which is what makes this a "
            + "measurement of the per-device store rather than of ConfigStore's",
            file: file, line: line
        )
        return (config, delivery)
    }

    // MARK: - The decrypt card's fixtures

    /// One entry in a `decrypt-check` event's `checks` array.
    ///
    /// `detail` and `fix` are the CLI's own words and are the card's unbounded
    /// input — the same relationship the app section has with a delivery
    /// summary. Nothing on the CLI side caps either.
    private static func checkJSON(
        key: String, label: String, ok: Bool, detail: String,
        fix: String? = nil, blocking: Bool = true
    ) -> String {
        let fixField = fix.map { "\"\($0)\"" } ?? "null"
        return #"{"key":"\#(key)","label":"\#(label)","ok":\#(ok),"detail":"\#(detail)","fix":\#(fixField),"blocking":\#(blocking)}"#
    }

    /// One entry in the same event's `backends` array. A backend is only drawn
    /// when it is unusable or carries a note, so every one built here is.
    private static func backendJSON(
        name: String, usable: Bool, reason: String? = nil, note: String? = nil
    ) -> String {
        let reasonField = reason.map { "\"\($0)\"" } ?? "null"
        let noteField = note.map { "\"\($0)\"" } ?? "null"
        return #"{"name":"\#(name)","usable":\#(usable),"reason":\#(reasonField),"note":\#(noteField)}"#
    }

    private static func decryptCheckJSON(
        ready: Bool, frontmostName: String?, checks: [String], backends: [String]
    ) -> String {
        let front = frontmostName.map { "\"\($0)\"" } ?? "null"
        return #"{"type":"decrypt-check","device":"\#(udid)","device_name":"iPhone 13 Pro","ready":\#(ready),"can_dump_from_list":\#(ready),"recommended_backend":"frida-ios-dump","frontmost_bundle_id":"com.alpha.app","frontmost_name":\#(front),"checks":[\#(checks.joined(separator: ","))],"backends":[\#(backends.joined(separator: ","))]}\n"#
    }

    /// A phone that can dump right now: three passing checks and one backend
    /// carrying a caveat, which is the ordinary state of a working bench.
    private static let readyCheck = decryptCheckJSON(
        ready: true, frontmostName: "Alpha",
        checks: [
            checkJSON(key: "frida", label: "Jailbreak (frida-server)", ok: true,
                      detail: "reachable, 429 processes"),
            checkJSON(key: "ssh", label: "SSH login", ok: true,
                      detail: "authenticated as root"),
            checkJSON(key: "foreground", label: "App on screen", ok: true, detail: "Alpha"),
        ],
        backends: [
            backendJSON(name: "bagbak", usable: true,
                        note: "bagbak bundles frida 16.7.19, the phone runs 16.6.6"),
        ]
    )

    /// A phone that cannot: every check failed, so every `fix` is on screen at
    /// once, and both backends explain themselves. This is the tallest and
    /// wordiest the card gets from a readiness probe alone.
    private static let blockedCheck = decryptCheckJSON(
        ready: false, frontmostName: nil,
        checks: [
            checkJSON(key: "frida", label: "Jailbreak (frida-server)", ok: false,
                      detail: "nothing answered on port 27042",
                      fix: "open Sileo, add build.frida.re, install frida-server, then reboot "
                          + "the phone and re-check"),
            checkJSON(key: "ssh", label: "SSH login", ok: false,
                      detail: "no SSH server answered",
                      fix: "install OpenSSH from Sileo, then set the root password from the "
                          + "device card so the dump can pull the bundle"),
            // Non-blocking, which is the third state the card draws
            // differently: a route being unavailable, not the feature being
            // broken.
            checkJSON(key: "foreground", label: "App on screen", ok: false,
                      detail: "nothing on screen", fix: "unlock the phone and open the app",
                      blocking: false),
        ],
        backends: [
            backendJSON(name: "frida-ios-dump", usable: false,
                        reason: "it pulls the app bundle off the phone over SSH, and no SSH "
                            + "server answered"),
            backendJSON(name: "bagbak", usable: false,
                        reason: "it needs a frida-server on the phone, and none answered"),
        ]
    )

    /// The same event with the two shapes the CLI's own words arrive in: a
    /// sentence nobody capped, and a filesystem path.
    ///
    /// The paths below are named `…-that-has-nowhere-to-wrap`, and that is a
    /// fixture name rather than a measurement — CoreText breaks all three of them
    /// at their slashes and hyphens. See
    /// `test_the_pane_fits_with_pathological_readiness_text`, which records where.
    private static let pathologicalCheck = decryptCheckJSON(
        ready: false, frontmostName: nil,
        checks: [
            checkJSON(key: "frida", label: "Jailbreak (frida-server)", ok: false,
                      detail: "the phone answered on port 27042 and then failed the handshake "
                          + "part way through, which is what a frida-server built against a "
                          + "different major version looks like from this side of the wire",
                      fix: "the version this CLI is pinned to is recorded in "
                          + "/Users/exampleuser/Library/Application-Support/graft/state/"
                          + "frida-version-pin-that-has-nowhere-to-wrap and the phone must "
                          + "match its major"),
            checkJSON(key: "ssh", label: "SSH login", ok: false,
                      detail: "authentication failed for root against "
                          + "/var/run/usbmuxd-00008030-0000000000000003-lockdown-session-socket",
                      fix: "set the password from the device card"),
        ],
        backends: [
            backendJSON(name: "bagbak", usable: false,
                        reason: "the bundled agent could not be written to "
                            + "/private/var/mobile/Containers/Data/Application/"
                            + "B8C7E4A2-11FE-4C0D-9A31-4F2E7C1D8A55/tmp/agent-with-nowhere-to-wrap.js"),
        ]
    )

    /// A dump that finished, narrating itself on the way: two `graft`
    /// notes (which wrap rather than truncate, deliberately), a handful of
    /// backend transcript lines, and a `decrypt-end` that had to strip five
    /// extensions — the success line that has to admit what it gave up.
    private static let successfulDump =
        #"{"type":"log","source":"graft","line":"the main binary came back still encrypted — frida-ios-dump cannot attach to an app iOS has suspended, so this dump is being handed to bagbak, which launches it itself"}\n"#
        + #"{"type":"log","source":"graft","line":"five app extensions are encrypted and no tool can decrypt them — dropping them and keeping the rest, which is what makes a one-click dump possible at all"}\n"#
        + #"{"type":"log","source":"bagbak","line":"spawned app pid => 16280"}\n"#
        + #"{"type":"log","source":"bagbak","line":"pulling app bundle from /private/var/containers/Bundle/Application/9C2A1B77-4E31-4A0B-9C21-3D5F6E7A8B90/Alpha.app"}\n"#
        + #"{"type":"log","source":"bagbak","line":"downloaded 214 files"}\n"#
        + #"{"type":"decrypt-end","bundle_id":"com.alpha.app","version":"27.11.8","path":"/Users/exampleuser/Library/Caches/graft/com.alpha.app_27.11.8.ipa","binaries":123,"stripped":["Payload/Alpha.app/PlugIns/QuickLaunchWidgets.appex","Payload/Alpha.app/PlugIns/AlphaShareExtension.appex","Payload/Alpha.app/PlugIns/AlphaNotificationService.appex","Payload/Alpha.app/PlugIns/AlphaIntentsExtension.appex","Payload/Alpha.app/Watch/AlphaWatch.app/PlugIns/AlphaWatchExtension.appex"]}\n"#

    /// The dump above, as something `sh -c` will run.
    private static var successfulDumpCommand: String { "printf '\(successfulDump)'" }

    /// A dump that failed the one way the app can offer to fix. The stderr
    /// names `--keep-encrypted-extensions`, which is what
    /// `canRetryDroppingExtensions` keys off — it reads the CLI's own
    /// recommendation rather than second-guessing it — so this is what puts the
    /// retry row on screen. A real non-zero exit with a real stderr, through the
    /// real error formatting, rather than a hand-assigned `lastError`.
    private static var failedDumpCommand: String {
        let log =
            #"{"type":"log","source":"graft","line":"three app extensions are encrypted and no tool can decrypt them, and this dump was told to keep every extension"}\n"#
            + #"{"type":"log","source":"bagbak","line":"spawned app pid => 16280"}\n"#
            + #"{"type":"log","source":"bagbak","line":"downloaded 12 files"}\n"#
        let stderr = "decrypt failed: 3 app extensions are still encrypted and no backend can "
            + "decrypt them; re-run without --keep-encrypted-extensions to drop them and keep "
            + "the rest"
        return "printf '\(log)' ; printf '\(stderr)\\n' >&2 ; exit 1"
    }

    /// The one store both the decrypt card and the installed-apps list read,
    /// seeded with whichever of its three concerns a case needs: a readiness
    /// answer, the leavings of a dump that has already run, and a listing.
    ///
    /// One builder because there is one store — see `DeviceAppsStore` for why
    /// the two instances this replaces could not say which phone they were
    /// about. All three go through `printf` into a real subprocess and the real
    /// event decoder, so a typo in a fixture line yields *nothing* rather than
    /// an error — `String.buildEvents()` drops what it cannot decode. That is
    /// why every test below asserts its premises before measuring.
    private func deviceAppsStore(
        check: String? = nil, dumping command: String? = nil, listing: String? = nil
    ) async -> DeviceAppsStore {
        let store = DeviceAppsStore(runner: sh())
        if let command { await store.decryptFrontmost(argsOverride: ["-c", command]) }
        // After the dump, because `decryptFrontmost` clears the card's other
        // fields on the way in. The two do not otherwise overlap.
        if let check {
            await store.checkReadiness(
                udid: Self.udid, argsOverride: ["-c", "printf '\(check)'"]
            )
        }
        if let listing {
            await store.load(udid: Self.udid, argsOverride: ["-c", "printf '\(listing)'"])
        }
        return store
    }

    // MARK: - The installed-apps list's fixtures

    /// One `device-app` event, as `--device-apps --with-icons` emits it.
    ///
    /// No `icon_path` field here — a real device answers with one for most
    /// apps and omits it for the rest (a LiveContainer-delivered app, which
    /// SpringBoard never had an icon for), and this section treats an absent
    /// path as the ordinary case rather than a defect. Omitting it from every
    /// fixture below means every row here measures the lettered tile, same
    /// reason the `Harness`'s `ArtworkStore` is never loaded — a fixture that
    /// wrote real PNGs to disk would put file I/O inside a layout measurement
    /// for a case this section already treats as unremarkable.
    private static func deviceAppJSON(
        _ bundleId: String, name: String, version: String = "1.0",
        encrypted: Bool = true, frontmost: Bool = false
    ) -> String {
        #"{"type":"device-app","bundle_id":"\#(bundleId)","name":"\#(name)","version":"\#(version)","encrypted":\#(encrypted),"frontmost":\#(frontmost)}\n"#
    }

    /// Sixty apps — the realistic shape of a phone that has been in use for a
    /// while, past the four or twelve the app section above measures against.
    /// Every fifth one is on screen, and every other one encrypted, so the
    /// TAG chip and the lock glyph both draw somewhere in the list rather than
    /// nowhere in it.
    private static let installedAppsJSON: String = {
        let apps = (1...60).map { i in
            deviceAppJSON(
                "com.bench.app\(i)", name: "Bench App \(i)", version: "1.\(i % 9).0",
                encrypted: i.isMultiple(of: 2), frontmost: i.isMultiple(of: 5)
            )
        }.joined()
        return apps + #"{"type":"device-apps-end","count":60,"encrypted":30}\n"#
    }()

    // MARK: - Harness

    /// The pane in the container it ships in: a split view's detail column.
    private struct Harness: View {
        let device: DeviceRecord
        @ObservedObject var devices: DevicesStore
        @ObservedObject var info: DeviceInfoStore
        @ObservedObject var config: ConfigStore
        @ObservedObject var delivery: DeviceDeliveryStore
        /// The store both device sections read, populated by the caller.
        /// Required rather than optional: the pane is handed one now, and an
        /// empty store measures the never-asked state, which several cases
        /// below deliberately want.
        @ObservedObject var deviceApps: DeviceAppsStore
        /// The IPA cache. No case in this file can make a load here succeed —
        /// `staged.load()` with no override runs the real
        /// `/bin/sh --json --staged-ipas` (`config.runner`'s executable, not
        /// a JSON-emitting stub), which exits non-zero and leaves `hasLoaded`
        /// false — so every row this section measures keeps rendering its
        /// pre-feature shape. This file measures layout, not the join, which
        /// `InstalledAppsSectionTests` owns.
        @ObservedObject var staged: StagedIPAStore

        var body: some View {
            NavigationSplitView {
                List { Text("iPhone 13 Pro"); Text("Builds"); Text("Settings") }
                    .navigationSplitViewColumnWidth(min: 220, ideal: 260, max: 360)
            } detail: {
                DeviceDetailView(
                    device: device,
                    store: devices,
                    deviceInfo: info,
                    config: config,
                    delivery: delivery,
                    // Never loaded, so every `image(for:)` answers nil and each
                    // row measures the lettered tile. Same choice
                    // `AppDetailLayoutTests` makes and for its reasons: the tile
                    // and real artwork occupy the identical frame (`AppIcon`
                    // clips both), so an icon cannot be what pushes this pane
                    // past the window, and a fixture that fetched artwork would
                    // put a subprocess inside a layout measurement. The
                    // installed-apps list below draws real `NSImage`s from
                    // `iconPath` instead of this store, and its own fixture
                    // deliberately omits `icon_path` so every row there also
                    // measures the tile, for the same reason.
                    artwork: ArtworkStore(runner: config.runner),
                    deviceApps: deviceApps,
                    staged: staged,
                    // Never loads successfully (`/bin/sh --json …`), so the
                    // section measures its error state — the tallest it has
                    // before a device answers.
                    liveContainer: LiveContainerStore(runner: config.runner),
                    appStore: AppStoreStore(runner: config.runner, shell: NoAdminShell()),
                    // No sidebar to navigate in this harness — this file
                    // measures layout, not the `.configured` row's click,
                    // which `InstalledAppsSectionTests` already pins.
                    onSelectApp: { _ in },
                    onSetUpLiveContainer: {}
                )
            }
        }
    }


    private func host(
        _ device: DeviceRecord,
        _ devices: DevicesStore,
        _ info: DeviceInfoStore,
        _ config: ConfigStore,
        _ delivery: DeviceDeliveryStore,
        deviceApps: DeviceAppsStore? = nil,
        staged: StagedIPAStore? = nil,
        width: CGFloat,
        height: CGFloat
    ) -> HostedLayout {
        let view = Harness(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery,
            deviceApps: deviceApps ?? DeviceAppsStore(runner: config.runner),
            staged: staged ?? StagedIPAStore(runner: config.runner)
        )
        return laidOutForMeasurement(view, width: width, height: height)
    }

    /// Scaffolding that is in the view tree but never drawn, and so cannot be
    /// "clipped off the top" however it is positioned.
    ///
    /// One entry, and it is measured rather than assumed: with the error banner
    /// up — the banner's dismiss control is the pane's first focusable view —
    /// SwiftUI parks a `KeyViewProxy` for the key-view (tab focus) loop at
    /// (79.5, **-16.5**, 31.5, 24.5), inside the *sidebar* column's hosting
    /// view, in a pane that is laid out perfectly. Counting it is a red CI on a
    /// correct change, which is the fastest way to get a layout guard deleted.
    ///
    /// Narrowing the walk to the detail column instead does not work, and the
    /// reason is worth recording: this split view's `arrangedSubviews` are the
    /// sidebar's *background* chrome (268pt) and a **full-width** wrapper
    /// holding both columns (900pt in a 900pt window) — the representation
    /// `ContentView`'s doc comment warns reads like an overflow and is not one.
    /// So `arrangedSubviews.last` is not the detail pane, and a walk from it
    /// still descends into the sidebar.
    private static let notDrawn: Set<String> = ["KeyViewProxy"]

    /// The furthest any visible descendant is laid out outside the window —
    /// above it, left of it, or past its right edge — and what it was. The name
    /// is what makes a failure diagnosable, since the answer is usually a view
    /// deep inside AppKit's hierarchy.
    ///
    /// All three edges, because this app has shipped two of them: content laid
    /// out **120pt past the right edge** (`WindowLayoutTests`, a nested split
    /// view) and content laid out **above the top edge** (`DevicesLayoutTests`,
    /// this pane's predecessor). The right edge matters here in particular
    /// because the header puts an unbounded device name in an `HStack` with the
    /// Refresh button, and a name that refuses to truncate pushes the button
    /// off the pane rather than shortening itself.
    private func worstOutOfBounds(_ hosted: HostedLayout) -> (points: CGFloat, view: String) {
        var worst: CGFloat = 0
        var who = "nothing"
        func walk(_ view: NSView, _ path: String) {
            // Hidden subtrees are skipped rather than merely unmeasured:
            // AppKit parks full-size scaffolding under hidden parents, and
            // reporting that as clipped content is a red CI on a correct
            // change — see `WindowLayoutTests.worstOverflow`.
            guard !view.isHidden else { return }
            guard !Self.notDrawn.contains(String(describing: type(of: view))) else { return }
            let frame = view.convert(view.bounds, to: hosted.root)
            // Degenerate frames aren't on screen to be clipped, and SwiftUI
            // leaves plenty of them in the tree.
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

    /// Both properties, on one hosted pane.
    ///
    /// The height ceiling is the window plus 40pt, the same slack
    /// `DevicesLayoutTests` uses and for the same reason: a `NavigationSplitView`
    /// reserves a titlebar area above its columns, so a *correct* pane measures
    /// a little over the window it was framed to. 40pt cannot launder the defect
    /// this guards — that one was ~700pt over in a 700pt window, and a header
    /// carrying one unbounded sentence is worth hundreds of points on its own.
    private func assertFits(
        device: DeviceRecord,
        devices: DevicesStore,
        info: DeviceInfoStore,
        config: ConfigStore,
        delivery: DeviceDeliveryStore,
        deviceApps: DeviceAppsStore? = nil,
        staged: StagedIPAStore? = nil,
        _ label: String,
        width: CGFloat = Metrics.windowMinWidth,
        height: CGFloat = Metrics.windowMinHeight,
        file: StaticString = #filePath,
        line: UInt = #line
    ) {
        let hosted = host(
            device, devices, info, config, delivery,
            deviceApps: deviceApps, staged: staged,
            width: width, height: height
        )
        XCTAssertLessThanOrEqual(
            hosted.root.bounds.height, hosted.contentHeight + 40,
            "the device pane (\(label)) demanded \(Int(hosted.root.bounds.height))pt of "
            + "height in a \(Int(hosted.contentHeight))pt window — on screen the excess comes off the "
            + "top, taking the header and Refresh button with it and leaving the pane "
            + "blank with a phone plugged in",
            file: file, line: line
        )
        XCTAssertLessThanOrEqual(
            hosted.root.bounds.width, hosted.contentWidth + 2,
            "the device pane (\(label)) demanded \(Int(hosted.root.bounds.width))pt of width "
            + "in a \(Int(hosted.contentWidth))pt window — content asking for more width than "
            + "it has is what clipped Build & Install off the app pane, 120pt past the edge",
            file: file, line: line
        )
        // 2pt of tolerance, for rounding on a divider or a hairline. The
        // defects this guards were 120pt and ~60pt, so nothing here needs a
        // generous margin.
        let out = worstOutOfBounds(hosted)
        XCTAssertLessThanOrEqual(
            out.points, 2,
            "part of the device pane (\(label)) is laid out \(Int(out.points))pt outside the "
            + "window, where it is clipped and unreachable: \(out.view)",
            file: file, line: line
        )
    }

    // MARK: - The pane fits

    /// The ordinary case, at the size the window actually ships at: a phone
    /// with vitals, four configured apps, and this phone's delivery states.
    func test_the_pane_fits_at_the_minimum_window_size() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        // Anti-vacuity: without both fixtures landing this measures a pane with
        // nothing in it, and every assertion below passes whatever the layout
        // does. That is exactly how `WindowLayoutTests`' first version managed
        // to guard nothing. The app section's own premises are asserted in
        // `appSectionStores`.
        XCTAssertNotNil(
            info.info[Self.udid],
            "the device-info fixture never loaded, so the stat strip measured is all "
            + "dashes and the assertions prove nothing about a populated one"
        )
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "one device, full vitals, four apps"
        )
    }

    /// A locked phone: the device is listed, but lockdown declined the
    /// `--device-info` query, so every cell falls back on its own.
    func test_the_pane_fits_with_no_device_info_at_all() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: nil)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertTrue(info.info.isEmpty, "premise: nothing was read for this phone")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "no vitals"
        )
    }

    /// The header's one unbounded input — a device name is whatever the user
    /// typed into Settings on the phone — in its ordinary prose shape.
    ///
    /// **The length is the fixture, not the claim**, and this doc used to have
    /// that backwards: it called a long name "the string most likely to
    /// reproduce the ~78-stacked-lines failure this file exists for". It cannot
    /// reproduce it at any length. `DeviceHeader` renders the name as
    /// `Text(device.name)` under `.lineLimit(1)`, and a `Text` that truncates
    /// asks the pane for nothing. Measured through this harness at 1, 20, 60,
    /// 100, 200, 400, 800, 2 000 and 20 000 characters: 900 × 592pt with 0pt out
    /// of bounds at every one, the same three numbers as the 13-character name
    /// every other case in this file uses.
    ///
    /// What the case is for is the header's *arrangement*. The name shares an
    /// `HStack` with the DEFAULT TARGET chip and the Refresh button, both of
    /// which are `.fixedSize()`, so the name is the element that has to give up
    /// width — and a long one is the input that would expose that going wrong
    /// (the `lineLimit` dropped, or a `.fixedSize()` arriving beside it). A short
    /// name would expose nothing. The file doc measures what such an injection
    /// costs here: one unbounded 200-character `Text` under `fixedSize` in this
    /// header took the pane to 2267pt in a 560pt window.
    func test_the_pane_fits_with_a_very_long_device_name() async throws {
        let name = "Owners iPhone 13 Pro Max, the one on the bench by the window, "
            + "do not wipe this one, it has the good jailbreak on it"
        let devices = await devicesStore(printing: Self.deviceJSON(name: name))
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertEqual(device.name, name, "premise: the long name actually reached the view")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "a very long device name"
        )
    }

    /// The same input with genuinely no wrap opportunity in it — **and the
    /// fixture had to be changed to make that true.**
    ///
    /// It was hyphen-separated ("Owners-iPhone-13-Pro-Max-…") under a comment
    /// claiming a single unbreakable run. A hyphen is a line-break opportunity.
    /// Measured with CoreText (`CTTypesetterSuggestLineBreak` against
    /// `SuggestClusterBreak`, mono 11pt) at the widths this app lays out in: that
    /// string breaks after a hyphen at character 36 of 134 when offered 260pt,
    /// where the character-break fallback would not have come until 38. So it was
    /// prose with small words, and this case was testing the one above it twice.
    /// The name below has no separator in it at all, and the same measurement
    /// confirms the difference: word break equals cluster break at both 120 and
    /// 260pt, which is CoreText saying it found nothing to break at and fell back
    /// on characters.
    ///
    /// **What the case still cannot show is that the run's length matters.**
    /// Same sweep as the case above and the same answer: 900 × 592pt with 0pt out
    /// of bounds from 1 to 20 000 characters of unbroken letters. The mechanism
    /// `ErrorBanner.tokenLimit` records — a `Text` asking for the width its
    /// content would take on one line — cannot reach the window from here,
    /// because the name is `lineLimit(1)`.
    ///
    /// Worth recording, since it is the assumption underneath several comments in
    /// this file: given a bounded width SwiftUI breaks an unbreakable run rather
    /// than overflowing it. 130 characters of one letter offered 200pt lays out
    /// 70pt tall — five lines — exactly as 130 characters of prose does. The
    /// unbreakable-run hazard is about *ideal* width, which only an unbounded
    /// proposal asks for, and nothing on this pane makes one.
    ///
    /// It is kept because the shape is real and costs nothing to measure: a phone
    /// named without spaces is an ordinary thing to find on a bench, and it is
    /// the input a dropped truncation mode would show up in.
    func test_the_pane_fits_with_a_device_name_that_cannot_wrap() async throws {
        // No spaces, no hyphens, no slashes — measured above as having no break
        // opportunity at all, which the old hyphenated fixture did not have.
        let name = "OwnersiPhone13ProMaxTheOneOnTheBenchByTheWindow"
            + "DoNotWipeThisOneItHasTheGoodJailbreakOnItAndTheCableIsFine"
        let devices = await devicesStore(printing: Self.deviceJSON(name: name))
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertEqual(device.name, name, "premise: the long name actually reached the view")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "an unwrappable device name"
        )
    }

    /// With the error banner up — the pane's one component above the divider
    /// besides the header, and where a failed Refresh lands.
    ///
    /// **What the banner receives is not what this fixture says**, which is worth
    /// stating because the fixture is written to look pathological. `ErrorBanner`
    /// normalizes through `displayed()` before anything is laid out: each
    /// space-separated token longer than `tokenLimit` (60) is elided from the
    /// middle to 59 characters, and the whole message is then capped at
    /// `messageLimit` (420). Measured on this exact string: **221 characters in,
    /// 188 out; longest token 92 in, 59 out.** No message does better — driven at
    /// 800 characters of unbroken run, what reaches the `Text` is still 59.
    ///
    /// The token is not unbreakable either, whatever the fixture calls itself:
    /// CoreText breaks it after `/var/run/` — character 9 of 91 — at both 120 and
    /// 260pt, where a character break would not come until 17 and 38.
    ///
    /// Nor can the height fail from it, which is the other half of the old
    /// framing. The banner is above the `ScrollView`, so a taller banner takes
    /// its room out of the scroll area rather than out of the window: 592pt
    /// against a 600pt ceiling with the message shrunk to `"x"`, and 592pt with
    /// it at the four-line cap.
    ///
    /// What is left is worth a case. This is the only pane state that puts an
    /// `ErrorBanner` above the divider, where its `.fixedSize(horizontal: false,
    /// vertical: true)` is legitimate and the both-axes form would not be — and
    /// that regression escapes on width, which is the pair this file can catch.
    func test_the_pane_fits_with_a_long_error_banner() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        devices.lastError = "Load devices failed (exit 1): could not connect to lockdownd "
            + "on /var/run/usbmuxd-00008030-0000000000000003-lockdown-session-socket-that-"
            + "has-nowhere-to-wrap; unlock the device and tap 'Trust this Computer', then "
            + "try again."
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "a long error banner"
        )
    }

    /// With the DEFAULT TARGET chip up, which is the widest the header gets.
    func test_the_pane_fits_with_the_default_target_chip() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON(isDefault: true))
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertTrue(device.isDefault, "premise: the fixture actually set is_default")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "the default-target chip"
        )
    }

    /// Wide as well as small. The recorded defect was width-driven — a text
    /// block proposed effectively no width — so a pane that only fits at one
    /// size is not fixed.
    func test_the_pane_fits_at_full_screen_width() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        let device = try XCTUnwrap(devices.devices.first)
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "full screen",
            width: 1470, height: 922
        )
    }

    // MARK: - The app section fits

    /// Twelve rows, each carrying a sentence the *CLI* wrote — the section's
    /// unbounded input, at bench scale.
    ///
    /// **The summaries' length is inert, and this doc used to rest entirely on
    /// it.** It argued that the realistic ones "run past a hundred characters
    /// already" and that "nothing stops a future one being longer", as though the
    /// case measured that. It does not, and the production code says so in as
    /// many words: the row is `Text(status).lineLimit(1).truncationMode(.tail)`,
    /// and `DeviceAppDeliveryRow`'s own comment calls that `lineLimit` "what
    /// keeps a summary the CLI made arbitrarily long from asking this pane for
    /// arbitrary width". Confirmed from both ends rather than read off the code:
    /// replacing both summaries with `"x"` leaves this case green, and driving
    /// all twelve rows at 20 000 characters leaves the pane at the same
    /// 900 × 592pt with 0pt out of bounds. There is no length at which the
    /// assertion trips.
    ///
    /// The second fixture is not the unbreakable shape its comment claimed
    /// either: CoreText breaks that path after `/private/` — character 13 of 111
    /// offered 120pt, where a character break would come at 17. It is kept
    /// because it is what a real "the file is not on the device" summary looks
    /// like, not because it cannot wrap. See
    /// `test_the_pane_fits_with_a_device_name_that_cannot_wrap` for a fixture
    /// that actually cannot.
    ///
    /// What the case is for: the section in its populated rendering, twelve rows
    /// deep, with a string at each row's one CLI-written site — which is the
    /// input a structural regression escapes through. The file doc measures the
    /// escape: one row under a plain `.fixedSize()` demanded 1796pt of width in a
    /// 900pt window with 896pt of the pane laid out outside it. Twelve rows
    /// rather than four because that is the one count here that is not free — it
    /// makes the section as tall as it realistically gets *inside* the scroll
    /// view, which is a statement about how far the user has to scroll and not
    /// one this assertion can check.
    func test_the_pane_fits_with_pathological_delivery_summaries() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let names = (1...12).map { "app\($0)" }
        let apps = names.map { Self.appJSON($0) }.joined()
        let states = names.enumerated().map { index, name in
            // Alternating: an uncapped sentence, and one built around a
            // filesystem path. Both of them wrap — the path breaks at its
            // slashes, measured in this case's doc — so the pair is two prose
            // shapes rather than the prose/unbreakable pair this comment used to
            // claim. They are kept because they are the two shapes the CLI
            // actually emits, which is reason enough for both.
            index.isMultiple(of: 2)
                ? Self.deliveryJSON(
                    app: name, kind: "stale",
                    summary: "Changed since the last delivery, and the change is one this "
                        + "summary is going to describe at some length because nothing on "
                        + "the CLI side caps how much it may say about an app whose tweaks, "
                        + "options and source version all moved at once.",
                    reasons: ["rebuilt since delivery; app version changed: 21.16.2 to 21.17.1"]
                )
                : Self.deliveryJSON(
                    app: name, kind: "stale",
                    summary: "Our record says delivered; the file is not on the device: "
                        + "/private/var/mobile/Containers/Data/Application/"
                        + "B8C7E4A2-11FE-4C0D-9A31-4F2E7C1D8A55/Documents/\(name)-tweaked.ipa",
                    reasons: ["last delivery is no longer on the device"]
                )
        }.joined()

        let config = await configStore(printing: apps)
        let delivery = await deliveryStore(printing: states)
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertEqual(config.apps.count, 12, "premise: twelve apps reached the section")
        guard case let .ready(loaded) = delivery.readout(for: Self.udid) else {
            return XCTFail("premise: the twelve delivery states never loaded")
        }
        XCTAssertEqual(loaded.count, 12, "premise: every app got a long summary")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "twelve pathological summaries"
        )
    }

    /// Before the first `--delivery-state` lands, which is what the pane shows
    /// for its first few hundred milliseconds every time a phone is selected.
    func test_the_pane_fits_before_the_delivery_states_have_loaded() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let config = await configStore(printing: Self.listJSON)
        let delivery = await deliveryStore(printing: nil)
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertEqual(
            delivery.readout(for: Self.udid), .pending,
            "premise: this measures the pre-load state, not a loaded one"
        )
        XCTAssertFalse(config.apps.isEmpty, "premise: there are rows to render it on")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "delivery states still loading"
        )
    }

    /// The read failed, so the section carries an `ErrorBanner` of its own.
    ///
    /// The message shape carries the same caveat the Refresh banner's case
    /// spells out, and the numbers for this fixture are its own:
    /// `ErrorBanner.displayed()` normalizes it from **153 characters to 114 and
    /// its longest token from 98 to 59** before any of it is laid out, so the
    /// sqlite path the fixture is built around never reaches the view at length.
    /// It is not an unbreakable run to begin with — CoreText breaks it after
    /// `/Users/`, character 13 of 98 offered 120pt — and this banner is *inside*
    /// the `ScrollView`, so its height is absorbed on top of all that.
    ///
    /// What the case is for is the branch. `.failed` is a third rendering of this
    /// section — a banner above the rows and an em dash on every one of them —
    /// reached here through a real non-zero exit and the real error formatting
    /// rather than a hand-built `.failed`, so what is measured is a sentence this
    /// app can actually produce.
    func test_the_pane_fits_when_the_delivery_read_failed() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let config = await configStore(printing: Self.listJSON)
        let delivery = DeviceDeliveryStore(runner: sh())
        // A real non-zero exit with a real stderr, through the real error
        // formatting — not a hand-built `.failed`, which would measure a
        // sentence this app never produces.
        await delivery.load(
            udid: Self.udid,
            argsOverride: ["-c",
                "printf 'sqlite3.OperationalError: unable to open database file "
                + "/Users/exampleuser/Library/Application-Support/graft/state/"
                + "log.sqlite-wal-that-has-nowhere-to-wrap\\n' >&2; exit 1"]
        )
        let device = try XCTUnwrap(devices.devices.first)
        guard case .failed = delivery.readout(for: Self.udid) else {
            return XCTFail("premise: the failing fixture should have produced a .failed readout")
        }
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "the delivery read failed"
        )
    }

    /// No apps configured at all — the first-run state, kept so a fix that
    /// rescues a long list by breaking the empty one is caught rather than
    /// shipped.
    func test_the_pane_fits_with_no_apps_configured() async throws {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let config = await configStore(printing: "")
        let delivery = await deliveryStore(printing: "")
        let device = try XCTUnwrap(devices.devices.first)
        XCTAssertTrue(config.apps.isEmpty, "premise: nothing is configured")
        assertFits(
            device: device, devices: devices, info: info,
            config: config, delivery: delivery, "no apps configured"
        )
    }

    // MARK: - The decrypt card fits

    /// Everything on the pane *except* the decrypt card, at its ordinary
    /// settings — so that a decrypt case measures a whole pane rather than a
    /// card floating on its own. The sections stack, and the question this file
    /// asks is about the pane.
    private func populatedPane() async throws -> (
        DeviceRecord, DevicesStore, DeviceInfoStore, ConfigStore, DeviceDeliveryStore
    ) {
        let devices = await devicesStore(printing: Self.deviceJSON())
        let info = await infoStore(printing: Self.infoJSON)
        let (config, delivery) = await appSectionStores()
        return (try XCTUnwrap(devices.devices.first), devices, info, config, delivery)
    }

    /// The card at rest on a working bench: three passing checks, a backend
    /// caveat, and a button that names the app it will dump.
    func test_the_pane_fits_with_a_ready_decrypt_card() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(check: Self.readyCheck)
        XCTAssertEqual(
            apps.readiness[Self.udid]?.ready, true,
            "the readiness fixture never reached the store, so the card measured is the "
            + "empty one — a heading, a disabled button, and nothing else"
        )
        XCTAssertEqual(
            apps.readiness[Self.udid]?.checks.count, 3, "premise: three check rows are drawn"
        )
        XCTAssertEqual(
            apps.readiness[Self.udid]?.frontmostName, "Alpha",
            "premise: the button reads 'Decrypt Alpha' rather than the shorter fallback"
        )
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "a ready decrypt card"
        )
    }

    /// Wide as well as small, for the reason the rest of the file is: the
    /// recorded defect was width-driven, and a pane that only fits at one size
    /// is not fixed.
    func test_the_pane_fits_with_a_ready_decrypt_card_at_full_screen() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(check: Self.readyCheck)
        XCTAssertEqual(apps.readiness[Self.udid]?.ready, true, "premise: the card has an answer on it")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "a ready decrypt card, full screen",
            width: 1470, height: 922
        )
    }

    /// Nothing works: every check failed, so every `fix` the CLI wrote is on
    /// screen at once and both backends explain themselves. This is the card at
    /// its wordiest from a probe alone — and it is the ordinary state of a
    /// phone somebody just rebooted, not a rare one.
    ///
    /// "Wordiest" describes the fixture and not what is asserted. The card is
    /// inside the pane's `ScrollView`, so how tall it gets is absorbed: the
    /// height reads 592pt here exactly as it does with the card at rest. What
    /// this case adds over the ready one is the *rendering* — three failed
    /// checks, three `fix` lines, two backend lines, and the non-blocking third
    /// state the card draws differently — each of which is a `Text` a structural
    /// regression could land on and none of which the ready fixture draws.
    func test_the_pane_fits_with_a_blocked_decrypt_card() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(check: Self.blockedCheck)
        let readiness = try XCTUnwrap(apps.readiness[Self.udid], "premise: the blocked fixture loaded")
        XCTAssertFalse(readiness.ready, "premise: the card is in its blocked state")
        XCTAssertEqual(
            readiness.checks.filter { !$0.ok }.count, 3,
            "premise: three failures, so three `fix` lines are rendered"
        )
        XCTAssertTrue(
            readiness.checks.filter { !$0.ok }.allSatisfy { $0.fix != nil },
            "premise: each failure carries advice, which is the text being measured"
        )
        XCTAssertEqual(
            readiness.backends.filter { !$0.usable || $0.note != nil }.count, 2,
            "premise: both backend lines are drawn — the card filters to these"
        )
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "a blocked decrypt card"
        )
    }

    /// The card's four CLI-written sites at once — `detail`, `fix`, `reason` and
    /// `note` — none of which the CLI caps.
    ///
    /// **That uncapped-ness is a fact about the CLI, not something this case
    /// checks**, and the doc used to run the two together. Every one of those
    /// sites is `lineLimit`-ed in `DeviceDecryptSection` (2 on a detail, 3 on a
    /// fix, 3 on a backend line) under a vertical-only `.fixedSize()`, inside the
    /// pane's `ScrollView`. Measured by driving all four together at 1, 20, 60,
    /// 100, 200, 400, 800, 2 000 and 20 000 characters, in prose,
    /// hyphen-separated, slash-separated and single-run shapes: 900 × 592pt with
    /// 0pt out of bounds, every time.
    ///
    /// The "nowhere to wrap" halves of the fixture do wrap. CoreText breaks the
    /// frida-pin path after `/Users/` and the agent path after `/private/` —
    /// character 13 of 101 and of 118 respectively, offered 120pt, against a
    /// character-break fallback at 17.
    ///
    /// What survives is the sentence the old doc ended on, and it is the true
    /// one: **this is the case that goes red if a plain `.fixedSize()` ever lands
    /// on any of those four `Text`s.** The file doc measures that injection —
    /// 1606pt of width demanded in a 900pt window with 706pt laid out outside —
    /// and the margin in the other direction is thin, since the pane sits at
    /// exactly 900pt against a 902pt ceiling. What is *not* known is how short a
    /// string that injection could carry and still trip it; see the file doc.
    func test_the_pane_fits_with_pathological_readiness_text() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(check: Self.pathologicalCheck)
        let readiness = try XCTUnwrap(apps.readiness[Self.udid], "premise: the fixture loaded")
        XCTAssertEqual(readiness.checks.count, 2, "premise: both long checks are drawn")
        XCTAssertTrue(
            readiness.checks.contains { ($0.fix ?? "").contains("nowhere-to-wrap") },
            "premise: the long path reached the card, so what is measured is the "
            + "fixture described above rather than a card whose JSON decoded to nothing"
        )
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "pathological readiness text"
        )
    }

    func test_the_pane_fits_with_pathological_readiness_text_at_full_screen() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(check: Self.pathologicalCheck)
        XCTAssertNotNil(apps.readiness[Self.udid], "premise: the fixture loaded")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "pathological readiness, full screen",
            width: 1470, height: 922
        )
    }

    /// After a dump that worked: the success line naming five stripped
    /// extensions, the CLI's notes (which wrap rather than truncate, on
    /// purpose), and the backend transcript. Everything the card can show at
    /// once except a failure.
    ///
    /// The five names are the fixture's realism, not the assertion's reach.
    /// Measured at 0, 5, 20 and 60 stripped names, the pane is 900 × 592pt with
    /// 0pt out of bounds in all four — the success line is `lineLimit(4)` under a
    /// vertical-only `fixedSize` inside the `ScrollView`, so what its four-line
    /// limit protects is *legibility*, that the half admitting what the app is
    /// missing is not the half that gets cut. That is checked by reading the
    /// line, not by this file. What this case measures is the rendering: notes,
    /// transcript and success line on screen together, which is the fullest the
    /// card gets short of a failure.
    func test_the_pane_fits_after_a_dump_that_stripped_extensions() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(
            check: Self.readyCheck, dumping: Self.successfulDumpCommand
        )
        let last = try XCTUnwrap(apps.lastDecrypt, "premise: the decrypt-end fixture loaded")
        XCTAssertEqual(
            last.stripped.count, 5,
            "premise: five names past the sentence itself is the length the success "
            + "line's 4-line limit was chosen against — with fewer, this measures a "
            + "shorter line than the one that motivated the limit"
        )
        XCTAssertEqual(apps.dumpLog.notes.count, 2, "premise: both wrapping notes are on screen")
        XCTAssertEqual(apps.dumpLog.lines.count, 3, "premise: the transcript panel has content")
        XCTAssertNil(apps.lastError, "premise: this is the success case, not both at once")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "after a dump that stripped extensions"
        )
    }

    /// After a dump that failed the one way the app can offer to fix: an error
    /// banner, the retry row under it, and the output kept for reading.
    func test_the_pane_fits_after_a_dump_that_failed() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(
            check: Self.readyCheck, dumping: Self.failedDumpCommand
        )
        XCTAssertNotNil(apps.lastError, "premise: the failure fixture produced a real error")
        XCTAssertTrue(
            apps.canRetryDroppingExtensions,
            "premise: the CLI named --keep-encrypted-extensions, so the retry row is on "
            + "screen — without it this measures a banner and nothing else"
        )
        XCTAssertNil(apps.lastDecrypt, "premise: exactly one of the two is ever set")
        XCTAssertEqual(apps.dumpLog.notes.count, 1, "premise: the note survived the failure")
        XCTAssertEqual(apps.dumpLog.lines.count, 2, "premise: so did the transcript")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps, "after a dump that failed"
        )
    }

    // MARK: - The installed-apps list fits

    /// The ordinary case for this section, at bench scale: sixty apps, a
    /// working decrypt card above it, and every other section on the pane
    /// populated too — measuring the section on its own would prove nothing
    /// about whether it pushes the *pane* past the window once everything
    /// above it is also on screen.
    ///
    /// Sixty is realism, not reach. Measured at 0, 10, 60 and 300 apps the pane
    /// is 900 × 592pt with 0pt out of bounds in all four, because the section is
    /// inside the `ScrollView` and a longer list only means more scrolling. A
    /// row's own strings are inert too — name and bundle id driven to 800
    /// characters in four wrap shapes give the same three numbers. What the case
    /// measures is the section in its populated rendering, underneath a
    /// populated everything-else.
    func test_the_pane_fits_with_a_realistic_installed_apps_list() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        // One store carries both concerns now — the card's readiness answer and
        // the section's listing — which is what the pane is handed in the app.
        let apps = await deviceAppsStore(
            check: Self.readyCheck, listing: Self.installedAppsJSON
        )
        XCTAssertEqual(
            apps.apps[Self.udid]?.count, 60,
            "the sixty-app fixture never reached the store, so the section measured is "
            + "empty and every assertion about it proves nothing"
        )
        XCTAssertEqual(
            apps.lastLoad[Self.udid]?.count, 60, "premise: the listing reports its own count"
        )
        XCTAssertNotNil(apps.readiness[Self.udid], "premise: the decrypt card has an answer")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps,
            "sixty installed apps"
        )
    }

    /// Wide as well as small, for the reason the rest of this file is: the
    /// recorded defect was width-driven, and a pane that only fits at one
    /// size is not fixed.
    func test_the_pane_fits_with_a_realistic_installed_apps_list_at_full_screen() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore(
            check: Self.readyCheck, listing: Self.installedAppsJSON
        )
        XCTAssertEqual(apps.apps[Self.udid]?.count, 60, "premise: the sixty-app fixture loaded")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps,
            "sixty installed apps, full screen",
            width: 1470, height: 922
        )
    }

    /// Before "List apps" has ever been pressed — the section's own empty
    /// state, kept so a fix that rescues the populated list by breaking the
    /// empty one is caught rather than shipped. Same reasoning as
    /// `test_the_pane_fits_with_no_apps_configured` for the section above
    /// this one.
    func test_the_pane_fits_before_installed_apps_have_been_listed() async throws {
        let (device, devices, info, config, delivery) = try await populatedPane()
        let apps = await deviceAppsStore()
        XCTAssertNil(apps.lastLoad[Self.udid], "premise: nothing has been asked yet")
        assertFits(
            device: device, devices: devices, info: info, config: config,
            delivery: delivery, deviceApps: apps,
            "installed apps not yet listed"
        )
    }
}

/// The strip's four cells, tested as the values they render rather than
/// through the rendered view.
///
/// Same reason `DeviceSilhouetteTests` asserts on `Face` instead of the shape:
/// asserting on a SwiftUI body is expensive and brittle, and every decision
/// worth pinning here is a pure function of the record. The decision that
/// matters most: **each cell falls back independently**. A locked phone answers
/// one lockdown domain and declines the other, so a strip that blanked all four
/// cells because one of them came back nil would be throwing away readings it
/// actually has.
@MainActor
final class DeviceStatStripValueTests: XCTestCase {

    /// "35.73 GB free of 128 GB" did not fit the cell and read "35.73 GB f…f
    /// 128 GB". One unit, the total's, said once, and whole numbers from ten up.
    func test_storage_reads_free_of_total() {
        XCTAssertEqual(
            DeviceStatStrip.storageValue(free: 35_728_093_184, total: 128_000_000_000),
            "36 of 128 GB free"
        )
        XCTAssertEqual(
            DeviceStatStrip.storageValue(free: 8_460_000_000, total: 64_000_000_000),
            "8.5 of 64 GB free", "a decimal below ten, where a whole number would hide a gigabyte"
        )
        XCTAssertEqual(
            DeviceStatStrip.storageValue(free: 312_000_000_000, total: 1_000_000_000_000),
            "0.3 of 1 TB free", "the total's unit, for both"
        )
    }

    /// The value has to be readable in the narrowest cell the pane draws:
    /// four cells across a 900pt window with the sidebar dragged to its 360pt
    /// maximum leave ~117pt each (`AppDetailLayoutTests` measures the same
    /// strip shape), and the cell's own padding takes 20 of that. The storage
    /// and battery cells also claim at least their own value's width
    /// (`DeviceStatStrip.cellMinWidth`), taking it from the two that truncate
    /// anyway — a bundle id and a UDID, both in full on hover.
    func test_the_storage_and_battery_values_fit_the_width_they_claim() {
        let storage = DeviceStatStrip.storageValue(free: 128_000_000_000, total: 128_000_000_000)
        XCTAssertEqual(storage, "128 of 128 GB free", "premise: the widest a 128 GB phone reads")
        let battery = DeviceStatStrip.batteryValue(percent: 100, charging: true)
        let claimed = DeviceStatStrip.cellMinWidth(for: storage)
            + DeviceStatStrip.cellMinWidth(for: battery)
        // What a 900pt window at the widest sidebar leaves the strip, less the
        // two cells left to absorb it at a usable width each.
        let strip: CGFloat = 4 * 117 + 3 * Metrics.controlGap
        XCTAssertLessThanOrEqual(claimed + 2 * 80 + 3 * Metrics.controlGap, strip)
        XCTAssertGreaterThan(
            DeviceStatStrip.cellMinWidth(for: storage), 2 * Metrics.innerPadding + 60,
            "premise: the claim is the text's width plus the padding, not a constant"
        )
    }

    /// The strip, hosted at the narrowest width the pane gives it, lays the
    /// storage value out at least as wide as the text itself — read back off
    /// the accessibility tree (`AccessibilityTree`, which skips when macOS is
    /// not serving it), since a SwiftUI `Text` has no view of its own to
    /// measure. Equal cells gave it ~97pt for a ~130pt string.
    func test_the_storage_value_is_laid_out_whole_in_the_narrowest_strip() async throws {
        try AccessibilityTree.requireServed()
        let udid = "00008030-0000000000000001"
        let device = #"{"type":"device","udid":"\#(udid)","name":"Example Phone","model":"iPhone16,1","ios_version":"26.1","connection_type":"USB","livecontainer_bundle_id":"com.kdt.livecontainer.ABCDE12345","is_default":false}"#
        let info = #"{"type":"device-info","udid":"\#(udid)","reachable":true,"total_bytes":128000000000,"free_bytes":128000000000,"battery_percent":100,"battery_charging":true}"#
        guard case let .device(record)? = device.buildEvents().first,
              case let .deviceInfo(reading)? = info.buildEvents().first else {
            return XCTFail("fixtures did not decode")
        }
        let strip: CGFloat = 4 * 117 + 3 * Metrics.controlGap
        let hosted = hosted(DeviceStatStrip(device: record, info: reading), width: strip)
        settle(hosted)

        let value = DeviceStatStrip.storageValue(free: 128_000_000_000, total: 128_000_000_000)
        let node = try AccessibilityTree.nodes(in: hosted.window)
            .first { $0.role == kAXStaticTextRole && ($0.value as? String) == value }
        let element = try XCTUnwrap(node?.element, "no text reading \(value)")
        let width = try XCTUnwrap(AccessibilityTree.offMain { () -> Double? in
            var raw: CFTypeRef?
            guard AXUIElementCopyAttributeValue(element, kAXSizeAttribute as CFString, &raw) == .success,
                  let raw, CFGetTypeID(raw) == AXValueGetTypeID() else { return nil }
            var size = CGSize.zero
            AXValueGetValue(raw as! AXValue, .cgSize, &size)
            return Double(size.width)
        } ?? nil)
        let needed = (value as NSString).size(
            withAttributes: [.font: NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)]
        ).width
        XCTAssertGreaterThanOrEqual(width + 0.5, Double(needed), "the value was cut to \(width)pt")
    }

    /// Both halves come from one lockdown reading, so a half-answer is not a
    /// shape the CLI produces — but "35.73 GB free of — " would be worse than a
    /// dash if it ever did.
    func test_storage_falls_back_when_either_half_is_missing() {
        XCTAssertEqual(DeviceStatStrip.storageValue(free: nil, total: 128_000_000_000), "—")
        XCTAssertEqual(DeviceStatStrip.storageValue(free: 35_728_093_184, total: nil), "—")
        XCTAssertEqual(DeviceStatStrip.storageValue(free: nil, total: nil), "—")
    }

    /// The bar is the used fraction, not the free one — a nearly-full phone
    /// should read as a nearly-full bar.
    func test_the_used_fraction_is_used_over_total() throws {
        let fraction = try XCTUnwrap(
            DeviceStatStrip.usedFraction(free: 32_000_000_000, total: 128_000_000_000)
        )
        XCTAssertEqual(fraction, 0.75, accuracy: 0.0001)
    }

    /// No reading, no bar — an empty track drawn from missing data would claim
    /// the phone is empty.
    func test_there_is_no_used_fraction_without_both_numbers() {
        XCTAssertNil(DeviceStatStrip.usedFraction(free: nil, total: 128_000_000_000))
        XCTAssertNil(DeviceStatStrip.usedFraction(free: 35_000_000_000, total: nil))
    }

    /// A total of zero is a division by zero, and lockdown on a phone that is
    /// mid-restore has reported nonsense before.
    func test_a_zero_total_produces_no_fraction_rather_than_a_nan() {
        XCTAssertNil(DeviceStatStrip.usedFraction(free: 0, total: 0))
    }

    /// More free than total is impossible, and a bar drawn from it would run
    /// past its own track.
    func test_the_fraction_is_clamped_to_the_track() throws {
        let over = try XCTUnwrap(
            DeviceStatStrip.usedFraction(free: 200_000_000_000, total: 128_000_000_000)
        )
        XCTAssertEqual(over, 0, accuracy: 0.0001)
    }

    func test_battery_reads_percent_and_charging_state() {
        XCTAssertEqual(DeviceStatStrip.batteryValue(percent: 70, charging: true), "70% · charging")
        XCTAssertEqual(DeviceStatStrip.batteryValue(percent: 70, charging: false), "70%")
        XCTAssertEqual(
            DeviceStatStrip.batteryValue(percent: 70, charging: nil), "70%",
            "an unknown charging state is not a claim that it is discharging, but it is "
            + "not worth a dash either — the percentage is the reading"
        )
    }

    func test_battery_falls_back_when_the_percentage_is_missing() {
        XCTAssertEqual(DeviceStatStrip.batteryValue(percent: nil, charging: true), "—")
        XCTAssertEqual(DeviceStatStrip.batteryValue(percent: nil, charging: nil), "—")
    }

    /// nil here means "the scan looked and found no LiveContainer", not
    /// "unknown" — so it says so, rather than showing the strip's dash.
    func test_livecontainer_reads_the_bundle_id_or_says_it_is_absent() {
        XCTAssertEqual(
            DeviceStatStrip.liveContainerValue("com.kdt.livecontainer.ABCDE12345"),
            "com.kdt.livecontainer.ABCDE12345"
        )
        XCTAssertEqual(DeviceStatStrip.liveContainerValue(nil), "not installed")
    }
}

