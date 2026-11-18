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

    /// How long ago `date` was, for a timestamp the CLI wrote.
    ///
    /// Within a minute either side of `now` it is "just now". The CLI stamps a
    /// build with its own clock, a moment after the app last read `now` — and
    /// `RelativeDateTimeFormatter` rounds a zero-or-future interval into the
    /// future tense, so a build that had just finished read "LAST BUILD in 0s".
    /// No reader wants the seconds of a minute-old event anyway.
    static let justNowWindow: TimeInterval = 60

    static func relativeToNow(_ date: Date, now: Date = .now) -> String {
        guard abs(date.timeIntervalSince(now)) >= justNowWindow else { return "just now" }
        return relative.localizedString(for: date, relativeTo: now)
    }

    /// Parse a CLI-emitted ISO 8601 timestamp. Tries the fractional-seconds
    /// variant first (what `datetime.now(timezone.utc).isoformat()` produces),
    /// then falls back to the default format for safety.
    static func parseISO8601(_ raw: String) -> Date? {
        iso8601WithFractional.date(from: raw) ?? iso8601.date(from: raw)
    }
}
