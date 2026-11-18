import Foundation

/// ISO 8601 parsers + a short display formatter shared across views that
/// render server-side timestamps (BuildsView, AppDetailView, BuildLogView).
enum DateHelpers {
    static let iso8601WithFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()

    static let iso8601: ISO8601DateFormatter = ISO8601DateFormatter()

    static let mediumDateShortTime: DateFormatter = {
        let f = DateFormatter()
        f.dateStyle = .medium
        f.timeStyle = .short
        return f
    }()

    static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .abbreviated
        return f
    }()

    /// Parse a CLI-emitted ISO 8601 timestamp. Tries the fractional-seconds
    /// variant first (what `datetime.now(timezone.utc).isoformat()` produces),
    /// then falls back to the default format for safety.
    static func parseISO8601(_ raw: String) -> Date? {
        iso8601WithFractional.date(from: raw) ?? iso8601.date(from: raw)
    }
}
