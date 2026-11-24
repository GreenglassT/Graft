import Foundation

/// Apple's App Store storefronts, and the store a region with none shops in.
///
/// The Mac app's copy of `graft_ipa/storefronts.py`, which names the source
/// and date; tests/test_storefronts.py pins the two lists, the territory map
/// and `noStoreNote` together. The store pickers (Add App's Search, Edit App)
/// draw from `all`, and Search's starts at `forRegion`, so the app never sends
/// a `--country` Apple has no store for: it answers those with HTTP 400.
enum Storefronts {
    /// Apple's 175 storefronts, as ISO 3166-1 alpha-2 codes (Kosovo is XK).
    static let all: [String] = [
        "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AR", "AT", "AU", "AZ", "BA", "BB", "BE",
        "BF", "BG", "BH", "BJ", "BM", "BN", "BO", "BR", "BS", "BT", "BW", "BY", "BZ", "CA",
        "CD", "CG", "CH", "CI", "CL", "CM", "CN", "CO", "CR", "CV", "CY", "CZ", "DE", "DK",
        "DM", "DO", "DZ", "EC", "EE", "EG", "ES", "FI", "FJ", "FM", "FR", "GA", "GB", "GD",
        "GE", "GH", "GM", "GR", "GT", "GW", "GY", "HK", "HN", "HR", "HU", "ID", "IE", "IL",
        "IN", "IQ", "IS", "IT", "JM", "JO", "JP", "KE", "KG", "KH", "KN", "KR", "KW", "KY",
        "KZ", "LA", "LB", "LC", "LK", "LR", "LT", "LU", "LV", "LY", "MA", "MD", "ME", "MG",
        "MK", "ML", "MM", "MN", "MO", "MR", "MS", "MT", "MU", "MV", "MW", "MX", "MY", "MZ",
        "NA", "NE", "NG", "NI", "NL", "NO", "NP", "NR", "NZ", "OM", "PA", "PE", "PG", "PH",
        "PK", "PL", "PT", "PW", "PY", "QA", "RO", "RS", "RU", "RW", "SA", "SB", "SC", "SE",
        "SG", "SI", "SK", "SL", "SN", "SR", "ST", "SV", "SZ", "TC", "TD", "TH", "TJ", "TM",
        "TN", "TO", "TR", "TT", "TW", "TZ", "UA", "UG", "US", "UY", "UZ", "VC", "VE", "VG",
        "VN", "VU", "XK", "YE", "ZA", "ZM", "ZW",
    ]

    /// Regions with no store of their own, and the store their people use;
    /// plus the alias UK, which is GB.
    static let territories: [String: String] = [
        "PR": "US", "GU": "US", "VI": "US", "AS": "US", "MP": "US",
        "RE": "FR", "GP": "FR", "MQ": "FR", "GF": "FR", "YT": "FR",
        "PM": "FR", "BL": "FR", "MF": "FR",
        "GL": "DK", "FO": "DK",
        "AX": "FI",
        "MC": "FR", "LI": "CH", "SM": "IT", "VA": "IT",
        "GI": "GB", "IM": "GB", "JE": "GB", "GG": "GB",
        "SJ": "NO",
        "UK": "GB",
    ]

    static let defaultStore = "US"
    /// What Search says when this Mac's region has no store.
    static let noStoreNote = "Searching the US store: your region has no App Store."

    private static let known = Set(all)

    /// The store to ask, and what to say about how it was chosen.
    struct Store: Equatable {
        let code: String
        let note: String?
    }

    /// The storefront `code` names or shops in, upper-case, or nil.
    static func store(for code: String) -> String? {
        let upper = code.uppercased()
        if known.contains(upper) { return upper }
        return territories[upper]
    }

    /// The store this Mac's region shops in: its own, the one a territory
    /// uses, or the US store with `noStoreNote`. A region that can't be read
    /// (nil, or a numeric UN M.49 code such as 419) is the US store with
    /// nothing to say, as the CLI reads it.
    static func forRegion(_ region: String?) -> Store {
        guard let region, region.count == 2,
              region.allSatisfy({ $0.isASCII && $0.isLetter }) else {
            return Store(code: defaultStore, note: nil)
        }
        if let code = store(for: region) { return Store(code: code, note: nil) }
        return Store(code: defaultStore, note: noStoreNote)
    }

    /// A store's name as a picker shows it: "United Kingdom (GB)".
    static func label(_ code: String) -> String {
        let name = Locale.current.localizedString(forRegionCode: code) ?? code
        return "\(name) (\(code))"
    }

    /// `all`, in the order a picker lists them: by name.
    static let byName: [String] = all.sorted {
        label($0).localizedStandardCompare(label($1)) == .orderedAscending
    }
}
