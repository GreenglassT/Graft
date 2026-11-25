import Darwin
import Foundation
import SwiftUI

/// Whether this Mac has Apple silicon: the hardware, whatever this process
/// runs as. Half of what `MacSupport` decides from; the other half is which
/// download of Graft this is (`BundleFlavor`).
enum AppleSilicon {
    /// 1 on Apple silicon, including for a process running under Rosetta,
    /// where `uname` and `#if arch` both say x86_64. Intel Macs either don't
    /// have it or answer 0.
    static let sysctlName = "hw.optional.arm64"

    /// What `read` says about `sysctlName`: Apple silicon only for exactly 1.
    /// Absent (nil), 0 or anything else is an Intel Mac.
    ///
    /// `read` is the seam: a test answers it both ways without the hardware.
    static func isPresent(sysctl read: (String) -> Int32? = readSysctl) -> Bool {
        read(sysctlName) == 1
    }

    /// An integer sysctl by name, or nil when this Mac has no such entry.
    static func readSysctl(_ name: String) -> Int32? {
        var value: Int32 = 0
        var size = MemoryLayout<Int32>.size
        guard sysctlbyname(name, &value, &size, nil, 0) == 0,
              size == MemoryLayout<Int32>.size
        else { return nil }
        return value
    }
}

/// Which download of Graft this bundle is: Graft.zip, for Macs with Apple
/// silicon, or Graft-Intel.zip, for Intel Macs.
///
/// The bundle's own word for it, its Info.plist's `GraftBundleArch`, which
/// the release script writes before signing. Not the processor this process
/// runs as: the app's executable is universal in both downloads, so on an
/// Apple silicon Mac it runs as arm64 even inside the Intel download. What
/// differs is everything else inside: the Python and the tools, and only the
/// Apple silicon download has UnfairPlay and the PlayCover handover.
enum BundleFlavor: String, Equatable, Sendable {
    case arm64
    case x86_64

    /// The Info.plist key (`app_update.FLAVOR_KEY` in the CLI).
    static let infoKey = "GraftBundleArch"

    /// Its zip on a release. Graft.zip keeps meaning arm64: older updaters
    /// ask for it by that name.
    var assetName: String {
        switch self {
        case .arm64: return "Graft.zip"
        case .x86_64: return "Graft-Intel.zip"
        }
    }

    /// The Macs it is for, in a sentence.
    var macs: String {
        switch self {
        case .arm64: return "Macs with Apple silicon"
        case .x86_64: return "Intel Macs"
        }
    }
}

/// What this copy of Graft can do on this Mac: the one place the app
/// decides which features need Apple silicon. The CLI keeps its own
/// refusals (`host.apple_silicon`), which say the same thing if a feature
/// is reached anyway.
///
/// Two facts: whether the Mac has Apple silicon (`AppleSilicon`), and which
/// download this is (`BundleFlavor`), cross-checked against the embedded
/// Python's Mach-O header.
///
/// - The Apple silicon download on an Apple silicon Mac: everything.
/// - The Intel download, on either Mac: the app runs, without App Store
///   gets, decryption setup, Build for this Mac or PlayCover, which are
///   disabled with `unavailableLine`. On an Apple silicon Mac a banner says
///   to download Graft.zip instead, or, before macOS 14, that Graft.zip
///   needs it (`intelBuildBannerLine`).
/// - The Apple silicon download on an Intel Mac: nothing runs (its Python
///   can't), and `AppleSiliconGate` says which download this Mac needs.
/// - A bundle whose plist and Python disagree: nothing runs either; the gate
///   says it is mixed up. Never a guess at which of the two is right.
///
/// Injected, never read from a shipped environment variable: `GraftApp`
/// passes `thisMac` down as `\.macSupport`, and a test passes any of the
/// combinations.
struct MacSupport: Equatable, Sendable {
    enum Build: Equatable, Sendable {
        case flavor(BundleFlavor)
        /// What disagrees, as a clause: "its Info.plist says …".
        case mixedUp(String)
    }

    let appleSiliconMac: Bool
    let build: Build
    /// This Mac's macOS, by major version: whether Graft.zip opens here
    /// (`opensAppleSiliconDownload`). `thisMac` reads it; a test passes any.
    var macOSMajorVersion = 14

    enum Verdict: Equatable {
        /// The app runs, with `appleSiliconFeatures` deciding the rest.
        case runs
        /// The Apple silicon download on an Intel Mac.
        case wrongDownload
        /// The bundle disagrees with itself.
        case mixedUp(String)
    }

    var verdict: Verdict {
        switch build {
        case let .mixedUp(why): return .mixedUp(why)
        case .flavor(.arm64): return appleSiliconMac ? .runs : .wrongDownload
        case .flavor(.x86_64): return .runs
        }
    }

    /// Whether the app's windows run at all.
    var runsApp: Bool { verdict == .runs }

    /// App Store gets, Set Up Decryption, Build for this Mac, Install to
    /// Mac and Get PlayCover: an Apple silicon Mac, and the download that
    /// carries UnfairPlay and the PlayCover handover.
    var appleSiliconFeatures: Bool { appleSiliconMac && build == .flavor(.arm64) }

    /// The Intel download on an Apple silicon Mac: it runs, and says to
    /// download Graft.zip for everything.
    var isIntelBuildOnAppleSilicon: Bool { appleSiliconMac && build == .flavor(.x86_64) }

    /// Whether Graft.zip, the Apple silicon download, opens on this Mac's
    /// macOS. It needs macOS 14; the Intel download opens on macOS 13 too,
    /// on an Apple silicon Mac through Rosetta.
    var opensAppleSiliconDownload: Bool { macOSMajorVersion >= 14 }

    /// The one line beside an Apple-silicon-only action that is disabled
    /// here, or nil where it isn't.
    var unavailableLine: String? {
        guard !appleSiliconFeatures else { return nil }
        guard appleSiliconMac else { return Self.needsAppleSilicon }
        return opensAppleSiliconDownload ? Self.needsAppleSiliconDownload : Self.needsAppleSiliconDownloadAndMacOS14
    }

    /// The line over the Intel download on an Apple silicon Mac
    /// (`IntelBuildBanner`), or nil anywhere else. Before macOS 14 it says
    /// Graft.zip needs macOS 14, rather than send the user to a download
    /// that won't open.
    var intelBuildBannerLine: String? {
        guard isIntelBuildOnAppleSilicon else { return nil }
        return opensAppleSiliconDownload ? Self.intelBuildBanner : Self.intelBuildBannerBeforeMacOS14
    }

    /// First run's line on its Welcome screen, or nil where everything
    /// works. Graft's own setup has no decryption step (that is Settings ›
    /// App Store › Set Up Decryption…), so this is where a new user on an
    /// Intel Mac learns, before anything else, what this Mac can't do and
    /// what to use instead.
    var firstRunLine: String? {
        guard !appleSiliconFeatures else { return nil }
        let instead = "Graft works with a decrypted IPA you already have, or one dumped from "
            + "your own jailbroken iPhone or iPad."
        let download = opensAppleSiliconDownload
            ? "the Apple silicon download (Graft.zip)"
            : "the Apple silicon download (Graft.zip) and macOS 14 or later"
        return appleSiliconMac
            ? "This is the Intel build, so setup skips getting apps from the App Store, which "
                + "needs \(download). " + instead
            : "Setup skips getting apps from the App Store: decrypting them needs a Mac with "
                + "Apple silicon. " + instead
    }

    static let needsAppleSilicon = "Needs a Mac with Apple silicon"
    static let needsAppleSiliconDownload = "Needs the Apple silicon download of Graft (Graft.zip)"
    static let needsAppleSiliconDownloadAndMacOS14 =
        "Needs the Apple silicon download of Graft (Graft.zip) and macOS 14 or later"
    static let intelBuildBanner = "This is the Intel build. On an Apple silicon Mac, download Graft.zip for everything."
    static let intelBuildBannerBeforeMacOS14 =
        "This is the Intel build. The Apple silicon download, Graft.zip, does everything, but needs macOS 14 or later."

    /// Everything available: the Apple silicon download on an Apple silicon
    /// Mac. What a view sees when nothing set `\.macSupport`, as in most tests.
    static let appleSilicon = MacSupport(appleSiliconMac: true, build: .flavor(.arm64))
    /// The Intel download on an Intel Mac.
    static let intel = MacSupport(appleSiliconMac: false, build: .flavor(.x86_64))

    /// What a bundle's embedded Python is, as far as the flavor cares.
    enum EmbeddedPython: Equatable {
        /// No Python at all: a Debug or Xcode build, which runs the `graft`
        /// on PATH. (A release build missing it is a damaged app, which its
        /// first CLI launch says: `SetupNeed.damagedApp`.)
        case absent
        /// There, and not a Mach-O this can read; or more than one; or
        /// reached through a link (`embeddedPython(in:)`).
        case unreadable
        /// The processors it has code for (`MachOArchitectures`).
        case architectures([String])
    }

    /// Which download a bundle is, from its Info.plist's `GraftBundleArch`
    /// (`infoValue`, as `Bundle.object(forInfoDictionaryKey:)` gives it) and
    /// its embedded Python.
    ///
    /// **No key is arm64.** Every build before the key existed was the
    /// Apple silicon download (2.0.2 and 2.1 were arm64 only), and a Debug
    /// or Xcode build has none either. So a Debug build on an Intel Mac gets
    /// the gate, as it did before the Intel download existed.
    ///
    /// The Python must have code for the processor the key names (no key:
    /// arm64), or the bundle is mixed up, whichever of the two is wrong.
    static func build(infoValue: Any?, python: EmbeddedPython) -> Build {
        let flavor: BundleFlavor
        switch infoValue {
        case nil:
            flavor = .arm64
        case let text as String:
            guard let named = BundleFlavor(rawValue: text) else {
                return .mixedUp("its Info.plist names no build Graft knows (\(BundleFlavor.infoKey) is \"\(text)\")")
            }
            flavor = named
        default:
            return .mixedUp("its Info.plist's \(BundleFlavor.infoKey) isn't the name of a build")
        }
        switch python {
        case .absent:
            return .flavor(flavor)
        case .unreadable:
            return .mixedUp("its embedded Python isn't a program Graft can read")
        case let .architectures(found) where found.contains(flavor.rawValue):
            return .flavor(flavor)
        case let .architectures(found):
            let has = found.isEmpty ? "no processor" : found.joined(separator: ", ")
            return .mixedUp("its Info.plist says it is the build for \(flavor.macs) (\(flavor.rawValue)), "
                + "and its embedded Python has code only for \(has)")
        }
    }

    /// The folders, under a Graft.app, of the one holding the interpreter its
    /// CLI runs on (`app_update.EMBEDDED_PYTHON` in the CLI).
    static let embeddedPythonFolders = ["Contents", "Resources", "GraftPython", "bin"]

    /// Whether `name` is the interpreter's, in `embeddedPythonFolders`:
    /// python3.<one to three ASCII digits>, the whole name, as the CLI's
    /// `app_update.INTERPRETER_RE` finds it. Not python3.13t, python3.13-config,
    /// python3 or python3.13.1.
    static func isInterpreterName(_ name: String) -> Bool {
        let bytes = Array(name.utf8)
        let prefix = Array("python3.".utf8)
        guard bytes.starts(with: prefix), (1...3).contains(bytes.count - prefix.count) else { return false }
        return bytes.dropFirst(prefix.count).allSatisfy { (UInt8(ascii: "0")...UInt8(ascii: "9")).contains($0) }
    }

    /// The embedded Python of the bundle at `bundleURL`, read off its header.
    ///
    /// Found by its name (`isInterpreterName`), not as this release's
    /// python3.13, so the first Graft on a newer Python still reads as the
    /// build it is (`app_update.embedded_interpreter` in the CLI). No folder,
    /// or no interpreter in it, is `.absent`, as in a Debug build. More than
    /// one is `.unreadable`, a mixed-up bundle, as the CLI holds. So is a
    /// folder there that can't be listed, and one reached through a link, a
    /// folder on the way or the interpreter itself: a bundle's own code is
    /// its own files.
    static func embeddedPython(in bundleURL: URL) -> EmbeddedPython {
        let fileManager = FileManager.default
        func isLink(_ url: URL) -> Bool {
            (try? fileManager.attributesOfItem(atPath: url.path)[.type] as? FileAttributeType) == .typeSymbolicLink
        }
        var folder = bundleURL
        for part in embeddedPythonFolders {
            folder = folder.appending(path: part)
            if isLink(folder) { return .unreadable }
        }
        guard fileManager.fileExists(atPath: folder.path) else { return .absent }
        guard let listed = try? fileManager.contentsOfDirectory(atPath: folder.path) else { return .unreadable }
        let names = listed.filter(isInterpreterName)
        guard let name = names.first else { return .absent }
        guard names.count == 1 else { return .unreadable }
        let url = folder.appending(path: name)
        var isDirectory: ObjCBool = false
        guard !isLink(url), fileManager.fileExists(atPath: url.path, isDirectory: &isDirectory),
              !isDirectory.boolValue, let found = MachOArchitectures.read(url)
        else { return .unreadable }
        return .architectures(found)
    }

    /// This Mac and this copy of Graft, asked once: neither changes under a
    /// running app.
    static let thisMac = MacSupport(
        appleSiliconMac: AppleSilicon.isPresent(),
        build: build(
            infoValue: Bundle.main.object(forInfoDictionaryKey: BundleFlavor.infoKey),
            python: embeddedPython(in: Bundle.main.bundleURL)
        ),
        macOSMajorVersion: ProcessInfo.processInfo.operatingSystemVersion.majorVersion
    )
}

/// Which processors a Mach-O file has code for, read off its headers: the
/// fat table's entries, each checked against its slice's own header, or a
/// thin file's header. Named as `lipo -archs` names them, and as the CLI's
/// `macho.architectures` does. Never by running `lipo` or `file`.
enum MachOArchitectures {
    private static let fatMagic: UInt32 = 0xCAFE_BABE
    private static let fatMagic64: UInt32 = 0xCAFE_BABF
    private static let machMagics: Set<UInt32> = [0xFEED_FACE, 0xFEED_FACF]
    private static let machCigams: Set<UInt32> = [0xCEFA_EDFE, 0xCFFA_EDFE]
    private static let maxSlices: UInt32 = 16

    /// The file's architectures in file order, or nil when it isn't a Mach-O
    /// this can read: too short, not one, a fat table that disagrees with a
    /// slice, or no Mach-O slice at all.
    static func read(_ url: URL) -> [String]? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        func bytes(at offset: UInt64, count: Int) -> Data? {
            guard (try? handle.seek(toOffset: offset)) != nil,
                  let data = try? handle.read(upToCount: count), data.count == count
            else { return nil }
            return data
        }
        guard let head = bytes(at: 0, count: 8) else { return nil }
        let magic = head.bigEndianUInt32(at: 0)
        guard magic == fatMagic || magic == fatMagic64 else {
            return header(bytes(at: 0, count: 12)).map { [name(cputype: $0.0, subtype: $0.1)] }
        }
        let count = head.bigEndianUInt32(at: 4)
        guard count > 0, count <= maxSlices else { return nil }
        let entrySize = magic == fatMagic64 ? 32 : 20
        guard let table = bytes(at: 8, count: entrySize * Int(count)) else { return nil }
        var names: [String] = []
        for index in 0..<Int(count) {
            let at = index * entrySize
            let cputype = Int32(bitPattern: table.bigEndianUInt32(at: at))
            let subtype = table.bigEndianUInt32(at: at + 4)
            let offset = magic == fatMagic64
                ? UInt64(table.bigEndianUInt32(at: at + 8)) << 32 | UInt64(table.bigEndianUInt32(at: at + 12))
                : UInt64(table.bigEndianUInt32(at: at + 8))
            guard let slice = bytes(at: offset, count: 12) else { return nil }
            // A slice that is no Mach-O (a static library's archive) is left out.
            guard let found = header(slice) else { continue }
            let said = name(cputype: cputype, subtype: subtype)
            guard said == name(cputype: found.0, subtype: found.1) else { return nil }
            names.append(said)
        }
        return names.isEmpty ? nil : names
    }

    /// A Mach-O header's (cputype, cpusubtype), either byte order.
    private static func header(_ data: Data?) -> (Int32, UInt32)? {
        guard let data, data.count >= 12 else { return nil }
        let raw = data.bigEndianUInt32(at: 0)
        if machCigams.contains(raw) {
            // Stored little-endian, the usual case: read back to front.
            return (Int32(bitPattern: data.littleEndianUInt32(at: 4)), data.littleEndianUInt32(at: 8))
        }
        if machMagics.contains(raw) {
            return (Int32(bitPattern: data.bigEndianUInt32(at: 4)), data.bigEndianUInt32(at: 8))
        }
        return nil
    }

    /// <mach/machine.h>'s names; a subtype's top byte is capability bits.
    static func name(cputype: Int32, subtype: UInt32) -> String {
        let sub = subtype & 0x00FF_FFFF
        switch cputype {
        case 0x0100_000C: return sub == 2 ? "arm64e" : "arm64"
        case 0x0100_0007: return sub == 8 ? "x86_64h" : "x86_64"
        case 7: return "i386"
        default: return "cputype 0x\(String(UInt32(bitPattern: cputype), radix: 16)) subtype \(sub)"
        }
    }
}

private extension Data {
    func bigEndianUInt32(at offset: Int) -> UInt32 {
        let start = startIndex + offset
        return self[start..<start + 4].reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
    }

    func littleEndianUInt32(at offset: Int) -> UInt32 {
        let start = startIndex + offset
        return self[start..<start + 4].reversed().reduce(UInt32(0)) { $0 << 8 | UInt32($1) }
    }
}

private struct MacSupportKey: EnvironmentKey {
    static let defaultValue = MacSupport.appleSilicon
}

extension EnvironmentValues {
    /// What this copy of Graft can do on this Mac (`MacSupport`). Set once,
    /// at the root, by `AppleSiliconGate`; everything when unset.
    var macSupport: MacSupport {
        get { self[MacSupportKey.self] }
        set { self[MacSupportKey.self] = newValue }
    }
}
