import Foundation
@testable import Graft

/// Plan A's `update-plan` and run events, as the CLI writes them, for the
/// update tests: JSON lines for a `ScriptCLI` to print, and the records they
/// decode to. Each app has `targets`, one per device it is on (contract G):
/// `app` builds one from its device arguments unless `targets` are given,
/// and works out the app's own `install`, `manual_install`,
/// `blocked_reason`, `nothing_to_do` and `actionable` from them as the CLI
/// does (the contract's clarifications: a waiting or blocked device has
/// `install` false, and they count as work in `nothing_to_do` but not in
/// `actionable`, nor does an install only offered).
enum UpdatePlanFixture {
    static let phone = "00000000-0000000000000001"
    static let pad = "00000000-0000000000000002"
    static let spare = "00000000-0000000000000003"

    static let tweakSource = "https://repo.example.com"

    /// A tweak's `confirm_key` as the CLI writes one: its file, then a
    /// SHA-256 over what the person is shown (re-verify #2).
    static func key(_ filename: String, _ digit: Character = "a") -> String {
        "\(filename)@" + String(repeating: digit, count: 64)
    }

    /// One attached tweak. `verified`: its update comes from its own source,
    /// over HTTPS, with a published checksum (2.3.1 A1); `reason` says why
    /// not. `confirmed` is `verified` unless given (`--trust-tweak`). An
    /// unchecked one has no source, as the CLI writes it.
    static func tweak(
        _ filename: String, name: String? = nil, from: String = "1.0", to: String? = "1.1",
        unchecked: Bool = false, also: [String] = [], source: String? = tweakSource,
        verified: Bool = true, reason: String? = nil, confirmed: Bool? = nil, confirmKey: String? = nil,
        trustChanged: Bool = false
    ) -> [String: Any] {
        let known = !unchecked && to != nil
        return ["filename": filename, "name": name as Any? ?? NSNull(), "from": from,
                "to": to as Any? ?? NSNull(), "unchecked": unchecked, "also_affects": also,
                "source": (known ? source : nil) as Any? ?? NSNull(), "verified": known && verified,
                "unverified_reason": reason as Any? ?? NSNull(),
                "confirmed": known && (confirmed ?? verified),
                "confirm_key": (known ? confirmKey ?? key(filename) : nil) as Any? ?? NSNull(),
                "trust_changed": trustChanged]
    }

    /// One device an app is on. `waiting`: the sentence when it isn't
    /// connected; `blocked`: any other reason it can't install now. Either
    /// makes `install` false, as the CLI says it: nothing installs there in
    /// this run.
    /// `kind`: its delivery state ("current", "stale", ...); `connected`:
    /// on usbmux, which defaults to not `waiting`.
    static func target(
        _ device: String? = "Example iPhone", udid: String = phone, delivery: String = "livecontainer",
        install: Bool = true, installSkipped: String? = nil, manualInstall: Bool = false,
        waiting: String? = nil, blocked: String? = nil, readiness: String = "likely",
        readinessNote: String? = nil, kind: String = "current", connected: Bool? = nil
    ) -> [String: Any] {
        [
            "udid": udid, "device_name": device as Any? ?? NSNull(), "delivery": delivery,
            "lc_bundle_id": delivery == "livecontainer" ? "com.example.livecontainer" : NSNull(),
            "install": install && waiting == nil && blocked == nil,
            "install_skipped_reason": installSkipped as Any? ?? NSNull(),
            "manual_install": manualInstall, "waiting": waiting as Any? ?? NSNull(),
            "blocked_reason": blocked as Any? ?? NSNull(), "readiness": readiness,
            "readiness_note": readinessNote as Any? ?? NSNull(),
            "kind": kind, "connected": connected ?? (waiting == nil),
        ]
    }

    /// The sentence the CLI says for a device that isn't connected.
    static func notConnected(_ device: String) -> String {
        "\(device) isn't connected: it'll update when it is"
    }

    /// One app's plan. Without `targets`, one device from `device`,
    /// `delivery`, `install`, `installSkipped`, `manualInstall`, `waiting`
    /// and `blocked`; none for an app never delivered. `nothingToDo` and
    /// `actionable` are worked out as the CLI does unless given.
    static func app(
        _ name: String, displayName: String? = nil, from: String? = "1.0", to: String? = nil,
        skipped: String? = nil, tweaks: [[String: Any]] = [], install: Bool = true,
        installSkipped: String? = nil, manualInstall: Bool = false, blocked: String? = nil,
        waiting: String? = nil, readiness: String = "likely", readinessNote: String? = nil,
        device: String? = "Example iPhone", delivery: String = "livecontainer", nothingToDo: Bool? = nil,
        targets given: [[String: Any]]? = nil, actionable: Bool? = nil
    ) -> [String: Any] {
        let targets = given ?? (installSkipped == CLIContract.Update.notInstalledYet ? [] : [
            target(device, delivery: delivery, install: install, installSkipped: installSkipped,
                   manualInstall: manualInstall, waiting: waiting, blocked: blocked, readiness: readiness),
        ])
        func flag(_ target: [String: Any], _ key: String) -> Bool { target[key] as? Bool ?? false }
        func text(_ target: [String: Any], _ key: String) -> String? { target[key] as? String }
        let ready = targets.filter { text($0, "waiting") == nil && text($0, "blocked_reason") == nil }
        let getsFromStore = to != nil && skipped == nil
        let updatesTweaks = tweaks.contains {
            !($0["unchecked"] as? Bool ?? false) && $0["to"] is String && ($0["confirmed"] as? Bool ?? false)
        }
        let awaiting = tweaks.contains {
            !($0["unchecked"] as? Bool ?? false) && $0["to"] is String && !($0["confirmed"] as? Bool ?? false)
        }
        let anyWork = targets.contains {
            flag($0, "install") || flag($0, "manual_install") || text($0, "waiting") != nil
                || text($0, "blocked_reason") != nil
        }
        let blockedReason = !targets.isEmpty && ready.isEmpty
            ? targets.compactMap { text($0, "waiting") ?? text($0, "blocked_reason") }.joined(separator: "; ")
            : nil
        return [
            "app": name, "display_name": displayName as Any? ?? NSNull(),
            "store": ["from": from as Any? ?? NSNull(), "to": to as Any? ?? NSNull(),
                      "skipped_reason": skipped as Any? ?? NSNull()],
            "tweaks": tweaks, "install": targets.contains { flag($0, "install") },
            "install_skipped_reason": installSkipped as Any? ?? NSNull(),
            "manual_install": targets.contains { flag($0, "manual_install") },
            "blocked_reason": blockedReason as Any? ?? NSNull(), "readiness": readiness,
            "readiness_note": readinessNote as Any? ?? NSNull(),
            "targets": targets,
            "nothing_to_do": nothingToDo ?? !(getsFromStore || updatesTweaks || anyWork),
            "needs_confirmation": awaiting,
            "actionable": actionable ?? ready.contains { flag($0, "install") },
        ]
    }

    static func line(_ payload: [String: Any]) -> String {
        let data = try! JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        return String(data: data, encoding: .utf8)!
    }

    /// `checksFailed`, when given: the plan's lookups all failed
    /// (`checks_failed`); left out otherwise, as an older CLI leaves it.
    static func planLine(_ apps: [[String: Any]], quick: Bool = true, checksFailed: Bool? = nil) -> String {
        var payload: [String: Any] = ["type": "update-plan", "quick": quick, "apps": apps]
        if let checksFailed { payload["checks_failed"] = checksFailed }
        return line(payload)
    }

    static func record(_ apps: [[String: Any]], quick: Bool = false) -> UpdatePlanRecord {
        guard case let .updatePlan(record) = try! BuildEvent.decode(line: planLine(apps, quick: quick)) else {
            fatalError("not an update-plan line")
        }
        return record
    }

    static func plan(_ app: [String: Any]) -> UpdatePlanApp {
        record([app]).apps[0]
    }

    /// One device's result in an `update-app-end`.
    /// `waiting`, when given: its device went away during the run (review
    /// part 2, #9); left out otherwise, as an older CLI leaves it.
    static func targetEnd(_ udid: String = phone, device: String? = "Example iPhone", ok: Bool,
                          error: String? = nil, left: [String] = [], waiting: Bool? = nil) -> [String: Any] {
        var end: [String: Any] = ["udid": udid, "device_name": device as Any? ?? NSNull(), "ok": ok,
                                  "error": error as Any? ?? NSNull(), "left": left]
        if let waiting { end["waiting"] = waiting }
        return end
    }

    static func appEnd(_ app: String, ok: Bool, stopped: Bool = false, done: [String] = [],
                       left: [String] = [], error: String? = nil, targets: [[String: Any]] = []) -> String {
        line(["type": "update-app-end", "app": app, "ok": ok, "stopped": stopped, "done": done,
              "left": left, "error": error as Any? ?? NSNull(), "targets": targets])
    }

    static func end(ok: Bool, stopped: Bool = false, updated: [String] = [], failed: [String] = [],
                    blocked: [String] = [], notStarted: [String] = [], waiting: Int = 0) -> String {
        line(["type": "update-end", "ok": ok, "stopped": stopped, "updated": updated,
              "failed": failed, "blocked": blocked, "not_started": notStarted, "waiting": waiting])
    }

    static func step(_ app: String?, _ step: String, _ status: String, _ detail: String? = nil,
                     udid: String? = nil, device: String? = nil, waiting: Bool? = nil) -> String {
        var payload: [String: Any] = ["type": "update-step", "app": app as Any? ?? NSNull(), "step": step,
                                      "status": status, "detail": detail as Any? ?? NSNull(),
                                      "udid": udid as Any? ?? NSNull(), "device_name": device as Any? ?? NSNull()]
        if let waiting { payload["waiting"] = waiting }
        return line(payload)
    }

    /// An app line, as `--list` writes one.
    static func appLine(_ name: String, displayName: String? = nil, delivery: String = "livecontainer",
                        tweaks: [String] = []) -> String {
        let shown = displayName.map { "\"\($0)\"" } ?? "null"
        let attached = "[" + tweaks.map { "\"\($0)\"" }.joined(separator: ",") + "]"
        return #"{"type":"app","name":"\#(name)","bundle_id":"com.\#(name).app","preferred_version":"latest","tweaks":\#(attached),"display_name":\#(shown),"delivery":"\#(delivery)","auto_profile":false,"keep_bundle_id":true,"strip_extensions":false,"strip_supported_devices":true,"compression_level":6}"#
    }

    /// An app record, as `--list` writes one.
    static func appRecord(_ name: String, displayName: String? = nil, delivery: String = "livecontainer",
                          tweaks: [String] = []) -> AppRecord {
        let json = appLine(name, displayName: displayName, delivery: delivery, tweaks: tweaks)
        guard case let .app(record) = try! BuildEvent.decode(line: json) else { fatalError("not an app line") }
        return record
    }

    /// An app line, as `--list` writes one, with `fields` in place of
    /// `appLine`'s: one app's config as it stands after an edit.
    static func appLine(_ name: String, with fields: [String: Any]) -> String {
        var payload = try! JSONSerialization.jsonObject(with: Data(appLine(name).utf8)) as! [String: Any]
        payload.merge(fields) { _, given in given }
        return line(payload)
    }

    /// The record of `appLine(_:with:)`.
    static func appRecord(_ name: String, with fields: [String: Any]) -> AppRecord {
        guard case let .app(record) = try! BuildEvent.decode(line: appLine(name, with: fields)) else {
            fatalError("not an app line")
        }
        return record
    }
}
