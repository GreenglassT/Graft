import SwiftUI

/// A drawn phone silhouette — a rounded body with a border, a subtle
/// top-to-bottom gradient, and a face detail chosen from the CLI's product
/// type string (`iPhone16,1`, `iPad13,1`, …), which is the only hardware
/// signal available. Exists so a bench with two phones is visually
/// distinguishable at a glance, in both the sidebar row (14×21pt) and the
/// detail-pane header (~34×52pt).
///
/// Both call sites render this small, so every dimension below is a fraction
/// of `size` rather than a literal — see `AppIcon`'s `size * 0.235` for the
/// same idiom elsewhere in this file's neighbourhood.
///
/// **Deliberately does not tint from `DeviceColor` or `DeviceEnclosureColor`.**
/// Both come back from lockdown as bare integers — `1` and `9` on the device
/// this was built against — with no Apple-published mapping from integer to
/// color. Guessing one wrong reads as *this app thinks your phone is a
/// color it is not*, which is worse than the neutral gradient below. If a
/// real mapping ever surfaces, wire it in then; don't add a guessed one
/// here believing this was an oversight.
struct DeviceSilhouette: View {
    let productType: String
    let size: CGSize

    private var face: Face { Face(productType: productType) }

    private var cornerRadius: CGFloat { size.width * 0.22 }
    private var borderWidth: CGFloat { max(0.75, size.width * 0.045) }

    var body: some View {
        RoundedRectangle(cornerRadius: cornerRadius)
            .fill(
                // Top-lit, not a real material — `surface` (the lighter of
                // the two) at the top, `surface2` ("deeper recess" per its
                // own doc comment) at the bottom, the same pairing
                // `BuildButton` uses for its own vertical gradient.
                LinearGradient(
                    colors: [Palette.surface, Palette.surface2],
                    startPoint: .top, endPoint: .bottom
                )
            )
            .frame(width: size.width, height: size.height)
            .overlay(faceDetail)
            .overlay(
                RoundedRectangle(cornerRadius: cornerRadius)
                    .strokeBorder(Palette.line, lineWidth: borderWidth)
            )
            // Crops the notch below, which is deliberately drawn poking past
            // the top edge — see the comment on `.notch`.
            .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
    }

    @ViewBuilder
    private var faceDetail: some View {
        switch face {
        case .dynamicIsland:
            Capsule()
                .fill(Palette.surface2)
                .frame(width: size.width * 0.46, height: size.height * 0.07)
                .padding(.top, size.height * 0.09)
                .frame(maxHeight: .infinity, alignment: .top)

        case .notch:
            // Pushed up by half its own height so the body's `.clipShape`
            // above crops the top half away, leaving a notch flush with the
            // top edge rather than a floating pill — that floating look is
            // reserved for `.dynamicIsland`, which is the visual difference
            // between the two faces at a glance.
            Capsule()
                .fill(Palette.surface2)
                .frame(width: size.width * 0.52, height: size.height * 0.14)
                .offset(y: -size.height * 0.07)
                .frame(maxHeight: .infinity, alignment: .top)

        case .homeButton:
            Circle()
                .strokeBorder(Palette.line, lineWidth: borderWidth)
                .frame(width: size.width * 0.28, height: size.width * 0.28)
                .padding(.bottom, size.height * 0.06)
                .frame(maxHeight: .infinity, alignment: .bottom)

        case .generic:
            EmptyView()
        }
    }
}

extension DeviceSilhouette {
    /// The face detail to draw, decided entirely from the product-type
    /// string. Exposed as its own type because asserting on a rendered
    /// SwiftUI view is expensive and brittle — `DeviceSilhouetteTests`
    /// checks this classification instead of the view's body.
    enum Face: Equatable {
        /// `iPhone14,2`'s Face — a notch has more precedent than Island as a
        /// zero case, but neither should read as the default: see `generic`.
        case dynamicIsland
        case notch
        case homeButton
        /// The honest fallback for anything unrecognized — an iPad, a future
        /// identifier this parser can't read, or a malformed string. Chosen
        /// deliberately over guessing: a plain body with no face detail is
        /// truthful about what this component doesn't know, where a
        /// confidently-wrong notch or Island is not.
        case generic

        /// Model-number boundaries, verified against theiphonewiki.com's
        /// model list and the community `adamawolf/3048717` identifier gist
        /// (both cross-checked, not taken from memory) before being encoded
        /// here. Apple's `iPhone<major>,<minor>` numbering climbs with the
        /// face *almost* everywhere:
        ///
        ///  - **Home button:** everything before `iPhone10,3`.
        ///  - **Notch:** `iPhone10,3` (iPhone X) through `iPhone14,8`
        ///    (iPhone 14 Plus).
        ///  - **Dynamic Island:** `iPhone15,2` (iPhone 14 Pro) onward.
        ///
        /// Three real devices break that plain reading, and a rule that
        /// only compares major numbers gets all three wrong:
        ///
        ///  - `iPhone12,8` (iPhone SE 2nd gen, 2020) and `iPhone14,6`
        ///    (iPhone SE 3rd gen, 2022) reuse the iPhone 8 home-button body
        ///    on a chip generation that shipped alongside notch-era phones —
        ///    SE 2020's major number (12) sits inside the notch range
        ///    because it launched alongside the notched iPhone 11 line; SE
        ///    2022's (14) sits there because it launched alongside the
        ///    notched iPhone 13 line. Both are home button, not notch.
        ///  - `iPhone17,5` (iPhone 16e, Feb 2025) shares its major number
        ///    with the Dynamic Island `iPhone17,1`–`17,4` (the iPhone 16
        ///    line) but shipped with a plain notch — Apple's budget line
        ///    kept the older face more than two years after Dynamic Island
        ///    became standard on the mainline phones it launched beside.
        ///    (Rumour has the next budget model, unreleased as of this
        ///    writing, switching to Dynamic Island — if `iPhone18,x` or
        ///    later turns up notched, this exception needs a sibling.)
        ///
        /// One more irregularity, inside a single major number rather than
        /// across one: `iPhone10,1`/`10,2`/`10,4`/`10,5` are the iPhone 8
        /// and 8 Plus (two radio variants each, home button); only `10,3`
        /// and `10,6` are the iPhone X (notch). Minor number, not a `>=`
        /// comparison, decides within major 10 — see the branch below.
        ///
        /// (My starting assumption for this doc comment — Dynamic Island
        /// at `iPhone15,2`, notch at `iPhone10,3`, everything older a home
        /// button — held up as the *general* rule. What it missed was the
        /// SE resets and the 16e; both are folded in as exceptions rather
        /// than changing the general boundaries, which were correct.)
        private static let homeButtonExceptions: Set<String> = ["iPhone12,8", "iPhone14,6"]
        private static let notchExceptions: Set<String> = ["iPhone17,5"]

        init(productType: String) {
            guard let (major, minor) = Face.parse(productType) else {
                self = .generic
                return
            }

            if Face.homeButtonExceptions.contains(productType) {
                self = .homeButton
            } else if Face.notchExceptions.contains(productType) {
                self = .notch
            } else if major >= 15 {
                self = .dynamicIsland
            } else if major == 10 {
                self = (minor == 3 || minor == 6) ? .notch : .homeButton
            } else if major > 10 {
                self = .notch
            } else {
                self = .homeButton
            }
        }

        /// `"iPhone16,1"` → `(16, 1)`. `nil` for anything not shaped like an
        /// iPhone identifier — an iPad's `iPad13,1`, an empty string, or a
        /// malformed `"iPhone,"` with no numbers either side of the comma.
        private static func parse(_ productType: String) -> (major: Int, minor: Int)? {
            guard productType.hasPrefix("iPhone") else { return nil }
            let rest = productType.dropFirst("iPhone".count)
            let parts = rest.split(separator: ",", maxSplits: 1)
            guard parts.count == 2, let major = Int(parts[0]), let minor = Int(parts[1]) else {
                return nil
            }
            return (major, minor)
        }
    }
}
