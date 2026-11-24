import Darwin
import Foundation

/// Bytes that held a secret, overwritten where they lie.
///
/// A Swift `String` can't be wiped: it is immutable, may share storage, and
/// a short one lives inline in whatever holds it. So the UI turns a secret
/// into `[UInt8]` the moment it is submitted, clears the field, and hands the
/// array on. Everything that touches it after that wipes it.
enum Secret {
    /// `memset_s`, which the compiler may not optimize away as a dead store.
    static func zero(_ bytes: inout [UInt8]) {
        bytes.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress, raw.count > 0 else { return }
            _ = memset_s(base, raw.count, 0, raw.count)
        }
    }

    static func wipe(_ bytes: inout [UInt8]) {
        zero(&bytes)
        bytes.removeAll()
    }
}

/// A child's stdin, kept open for a two-way exchange.
///
/// `--appstore-login` reads the Apple ID and the password as stdin's first
/// two lines and, minutes later and only if Apple asks, the 2FA code as its
/// third. `runOnce(arguments:stdin:)` writes once and closes, which can't do
/// that. This stays open until `close()` or the child exits.
///
/// - A line sent before the launch is held in `pending`, which is reserved up
///   front so appending never reallocates and strands a copy. It is written
///   the moment `CLIRunner` attaches the pipe.
/// - Every caller's array is wiped, whether or not the write happened.
/// - Writes are POSIX `write(2)` on a descriptor marked `F_SETNOSIGPIPE`. A
///   child that has already exited must cost a `false`, not a SIGPIPE that
///   kills the app, nor the Objective-C exception `FileHandle.write` raises,
///   which Swift can't catch.
final class ChildInput: @unchecked Sendable {
    private static let reserve = 2048
    private let lock = NSLock()
    private var handle: FileHandle?
    private var pending: [UInt8] = []
    private var closed = false

    init() {
        pending.reserveCapacity(Self.reserve)
    }

    /// Whether `bytes` can go as one stdin line: no LF and no CR in them.
    static func isOneLine(_ bytes: [UInt8]) -> Bool {
        !bytes.contains(0x0A) && !bytes.contains(0x0D)
    }

    /// Send `bytes` as one line, then wipe them. False when the input is
    /// closed, the child has gone, or an early line wouldn't fit, and when
    /// `bytes` hold a line break themselves (review part 4, #20): the CLI
    /// reads the Apple ID, the password and the code as one line each, so an
    /// LF or CR inside one would move every later value to the wrong line.
    /// That is the caller's mistake, refused before anything is written, never
    /// a crash.
    @discardableResult
    func send(_ bytes: inout [UInt8]) -> Bool {
        defer { Secret.wipe(&bytes) }
        guard Self.isOneLine(bytes) else { return false }
        lock.lock()
        defer { lock.unlock() }
        guard !closed else { return false }
        guard let handle else {
            guard pending.count + bytes.count + 1 <= pending.capacity else { return false }
            pending.append(contentsOf: bytes)
            pending.append(0x0A)
            return true
        }
        return Self.write(handle.fileDescriptor, bytes) && Self.write(handle.fileDescriptor, [0x0A])
    }

    /// Called by `CLIRunner` once the child is running.
    func attach(_ writer: FileHandle) {
        lock.lock()
        defer { lock.unlock() }
        guard !closed else {
            try? writer.close()
            return
        }
        _ = fcntl(writer.fileDescriptor, F_SETNOSIGPIPE, 1)
        handle = writer
        if !pending.isEmpty {
            _ = Self.write(writer.fileDescriptor, pending)
            Secret.zero(&pending)
            pending.removeAll(keepingCapacity: true)
        }
    }

    /// Close our end, so the child reads end of input. Safe to call twice.
    func close() {
        lock.lock()
        defer { lock.unlock() }
        closed = true
        Secret.zero(&pending)
        pending.removeAll(keepingCapacity: true)
        try? handle?.close()
        handle = nil
    }

    private static func write(_ fd: Int32, _ bytes: [UInt8]) -> Bool {
        bytes.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return true }
            var offset = 0
            while offset < raw.count {
                let written = Darwin.write(fd, base + offset, raw.count - offset)
                if written < 0 {
                    if errno == EINTR { continue }
                    return false
                }
                offset += written
            }
            return true
        }
    }
}
