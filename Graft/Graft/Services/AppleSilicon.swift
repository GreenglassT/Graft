import Darwin
import Foundation

/// Whether this Mac has Apple silicon.
///
/// Graft 2's bundled Python and tools are arm64 only, so on an Intel Mac
/// nothing the app runs could start. The app's own executable stays universal
/// so that an Intel Mac still opens it and `AppleSiliconGate` can say why it
/// can't be used, and where Graft 1.1 is. An arm64-only app would get macOS's
/// "can't be opened on this Mac" instead, which names neither.
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

    /// This Mac, asked once: the hardware does not change under a running app.
    static let thisMac: Bool = isPresent()

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
