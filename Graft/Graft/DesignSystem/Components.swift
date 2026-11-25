import SwiftUI

struct CardStyle: ViewModifier {
    var surfaceOpacity: Double = 0.4
    var borderOpacity: Double = 0.4
    /// `Metrics.radiusInner` for a card nested inside another card — see the
    /// note on that constant. Every card sitting directly on the pane keeps
    /// the default.
    var cornerRadius: CGFloat = Metrics.radius

    func body(content: Content) -> some View {
        content
            .background(Palette.surface.opacity(surfaceOpacity))
            .overlay(RoundedRectangle(cornerRadius: cornerRadius).strokeBorder(Palette.line.opacity(borderOpacity), lineWidth: 0.5))
            .clipShape(RoundedRectangle(cornerRadius: cornerRadius))
    }
}

extension View {
    func cardStyle(
        surfaceOpacity: Double = 0.4,
        borderOpacity: Double = 0.4,
        cornerRadius: CGFloat = Metrics.radius
    ) -> some View {
        modifier(CardStyle(
            surfaceOpacity: surfaceOpacity,
            borderOpacity: borderOpacity,
            cornerRadius: cornerRadius
        ))
    }
}

extension View {
    /// A paid-only control on a free Apple ID: disabled, with the reason as its
    /// tooltip (`PaidOnly` holds the sentences). Nil — a paid account, or one
    /// not known yet — leaves the control exactly as it was, with no tooltip
    /// added: an empty `.help("")` still draws an empty bubble on hover.
    ///
    /// Composes with the control's own `.disabled(_:)`, which SwiftUI ORs, so
    /// a control busy for its own reasons stays disabled either way.
    @ViewBuilder
    func paidOnly(_ reason: String?) -> some View {
        if let reason {
            self.disabled(true).help(reason)
        } else {
            self
        }
    }
}

extension Binding {
    /// Drive an `isPresented:` from an optional payload: true while it's set,
    /// and clearing it on dismiss. `.alert(_:isPresented:presenting:)` wants
    /// both, and hand-rolling the pair at each call site is how they drift.
    func isPresent<Wrapped>() -> Binding<Bool> where Value == Wrapped? {
        Binding<Bool>(
            get: { wrappedValue != nil },
            set: { if !$0 { wrappedValue = nil } }
        )
    }
}

extension View {
    /// The confirmation shown before stopping a build, in one place.
    ///
    /// Two surfaces offer the stop — the detail pane's Stop button and the
    /// sidebar's context menu — and written out twice they had already drifted
    /// apart in title ("Stop building example?" against "Stop this build?").
    /// One body, so the destructive action in this app cannot describe itself
    /// differently depending on which control the user reached for.
    ///
    /// The action takes the value rather than re-reading that optional.
    /// Dismissing an alert writes the `isPresented` binding, which for
    /// `$pendingCancel.isPresent()` clears `pendingCancel` — so a closure that
    /// read it back at invocation time could find nil and silently do nothing.
    /// That is the sidebar's Stop, which is the only cancel that survives
    /// navigating away from the detail pane.
    ///
    /// `consequence` is a closure rather than a `String`, and that is not
    /// stylistic. `ConfigStore.cancelConsequence` derives from `buildStage`,
    /// which is rewritten on every stage start and end — a dozen-plus times per
    /// build. Passed by value it would be worked out in every `body` of
    /// whatever view attached this modifier — on `SidebarView`, the app's
    /// permanently-mounted navigation column, at every stage transition — for
    /// a sentence that is discarded unless the confirmation is actually up.
    /// Read inside the message builder, it is worked out only while the alert
    /// is up.
    func stopBuildConfirmation<T>(
        presenting item: Binding<T?>,
        name: @escaping (T) -> String,
        consequence: @escaping () -> String,
        onStop: @escaping (T) -> Void
    ) -> some View {
        alert(
            "Stop building \(item.wrappedValue.map(name) ?? "")?",
            isPresented: item.isPresent(),
            presenting: item.wrappedValue
        ) { value in
            Button("Keep building", role: .cancel) {}
            Button("Stop", role: .destructive) { onStop(value) }
        } message: { _ in
            Text(consequence())
        }
    }
}

/// `1 package` / `2 packages` — English, not `\(n) \(noun)s`.
///
/// Three surfaces count things (repo rows, the package table, Canister
/// results) and all three read "1 packages". Small, but it is the kind of
/// thing that makes a tool feel unfinished, and the fix is one place rather
/// than three ternaries.
func pluralized(_ count: Int, _ singular: String, _ plural: String? = nil) -> String {
    "\(count) " + (count == 1 ? singular : (plural ?? singular + "s"))
}

/// `128.3 MB` — one definition of what a byte count looks like in this app.
///
/// `.file`, which is 1000-based, because that is the unit Apple quotes storage
/// in: a 128 GB phone should read "128 GB", not "119.2 GB". Every size shown
/// anywhere in this app went through that count style already; what it did not
/// go through was one function. Seven call sites spelled the same expression
/// out inline, and two of them are load bearing together — `CachePaneView`'s
/// row and `AddAppSheet.Prefill`'s sentence print the size of **the same file
/// to the same user**, and until this existed their agreement was held by a
/// test asserting one literal in both places rather than by a shared
/// definition.
///
/// The formatter is built once. `ByteCountFormatter` is not free to construct,
/// and a `List` row that formats on every draw builds one per row per frame —
/// which is why `DeviceStatStrip` and `TweaksView` were already caching theirs
/// privately.
///
/// **`TweaksView` deliberately does not use this** and keeps its own: it sets
/// `allowedUnits = [.useKB, .useMB]` so a tweak `.deb` never reads in GB. That
/// is a different function, not a duplicate of this one.
func fileSize(_ bytes: Int) -> String {
    fileSizeFormatter.string(fromByteCount: Int64(bytes))
}

private let fileSizeFormatter: ByteCountFormatter = {
    let fmt = ByteCountFormatter()
    fmt.countStyle = .file
    return fmt
}()

enum Pasteboard {
    static func copy(_ string: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(string, forType: .string)
    }
}

struct MetaCell: View {
    let label: String
    let value: String
    /// `Metrics.radiusInner` when this cell sits inside another card, which is
    /// the caller's business and not this component's: `DeviceCard` nests a row
    /// of these, but `AppDetailView`'s meta strip puts them straight onto the
    /// pane background alongside cards drawn at the full radius. Baked in here,
    /// the strip was the one element in that column with tighter corners than
    /// everything stacked beneath it.
    var cornerRadius: CGFloat = Metrics.radius

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            // No `lineLimit` here, unlike the value below, and that is a
            // decision rather than an omission. `labelCaps()` sets font,
            // tracking and case only, so a label too wide for its cell wraps to
            // a second line — which makes the cell taller than its siblings but
            // leaves every word readable. `lineLimit(1)` would trade that for
            // "DELIVERY ST…", and a label is the half of a cell a reader cannot
            // reconstruct from context.
            //
            // It is close. "DELIVERY STATE", the longest label on the app
            // pane's 4-up strip, wants 91pt at 11pt semibold smallCaps with 0.6
            // tracking; a cell wraps below 110pt; and the tightest cell the app
            // can produce — 900pt window, sidebar dragged to its 360pt maximum
            // — is 117pt. Roughly one character of slack, and the strip sits
            // inside a `ScrollView` that would absorb the extra line silently,
            // so `AppDetailLayoutTests.test_the_longest_meta_label_does_not_
            // wrap_at_the_narrowest_cell` measures it directly.
            Text(label).labelCaps()
            Text(value)
                .font(AppFont.mono)
                .foregroundStyle(Palette.text)
                .lineLimit(1)
                .truncationMode(.middle)
        }
        .padding(Metrics.innerPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        .cardStyle(surfaceOpacity: 0.7, borderOpacity: 0.5, cornerRadius: cornerRadius)
        // After the padding and the frame, not before them: `.help` covers the
        // frame of the view it modifies, so applied to the bare text block it
        // left the card's padding and all the slack in an equal-width cell
        // outside the tooltip's reach — hovering the right half of the "Tweaks"
        // cell, or anywhere in its margin, produced nothing.
        //
        // These cells share the row's width equally, so a long value truncates
        // while a short one ("5") leaves space unused — deliberate, because a
        // 4-up strip that re-proportioned itself per app would jitter as you
        // move down the sidebar. This is what keeps the truncation from losing
        // anything: a UDID and "35.73 …f 128 GB" are both readable on hover.
        .help(value)
    }
}

struct SectionTitle: View {
    let text: String
    var body: some View {
        Text(text).labelCaps().padding(.bottom, 4)
    }
}

/// The app's one way of saying "that didn't work": white on
/// `Palette.dangerSurface`, with an optional heading and an optional dismiss
/// control.
///
/// The fill is `dangerSurface` and not `danger` — white on the latter is
/// 2.77:1. See `Palette.dangerSurface`.
///
/// Two details here are load-bearing, and each was a bug before it was code:
///
///  - `.fixedSize(horizontal: false, vertical: true)` — inside an HStack with a
///    trailing `Spacer`, a long message otherwise truncates to a single line.
///    "Failed to attach x.deb: attached to a, b, c" is exactly the message that
///    gets cut, and it's the half that says what to do about it.
///  - `.textSelection(.enabled)` — these get pasted into bug reports.
struct ErrorBanner: View {
    /// How the banner meets its container, and the only axis this component
    /// has. A strip that spans a pane edge-to-edge squares its corners and
    /// adopts that pane's own gutter, so the message lines up with the content
    /// above it; a banner inside an already-padded column is a card sized like
    /// its neighbours.
    enum Fit {
        case fullBleed(gutter: CGFloat)
        case inset
    }

    let message: String
    /// A heading above the message, for a banner whose message alone doesn't
    /// say which operation failed.
    var title: String? = nil
    var fit: Fit = .inset
    /// nil hides the dismiss control — for an error with nowhere to go, like a
    /// failed save that the next attempt clears anyway.
    var onDismiss: (() -> Void)? = nil

    /// The longest *unbreakable run* a message may contain.
    ///
    /// This is the real hazard, and it is not length. A `Text` asks for the
    /// width its content would take on one line; ordinary prose wraps, so it
    /// asks for very little. A token with nowhere to wrap — a URL, a
    /// filesystem path — cannot, so its demand travels up through the
    /// enclosing `NavigationSplitView` until the whole window stops drawing.
    /// `lineLimit` does not help: it bounds how many lines are *shown*, not
    /// how much width the content asks for.
    ///
    /// 60 characters at `AppFont.monoSmall` is comfortably inside the pane at
    /// the app's minimum window width.
    static let tokenLimit = 60

    /// A ceiling on the whole message, so a wall of text cannot bury the pane.
    ///
    /// Deliberately generous. It was 150, which is fine for "couldn't reach the
    /// device" and destroys anything that explains itself: a decrypt failure
    /// arrives as a short summary *plus* the sentence saying what to do about
    /// it, and shortening the pair from the middle ate the end of one and the
    /// start of the other, leaving a sentence that began mid-word. The width
    /// problem was never the character count — see `tokenLimit`.
    static let messageLimit = 420

    /// `message` with any unwrappable run shortened, then bounded overall.
    ///
    /// Tokens first, because that is what actually threatens the layout, and
    /// doing it per-token keeps the prose around them intact. Each is cut from
    /// the middle: both ends of a path or URL carry the information.
    static func displayed(_ message: String) -> String {
        let shortened = message
            .split(separator: " ", omittingEmptySubsequences: false)
            .map { token -> Substring in
                guard token.count > tokenLimit else { return token }
                let half = (tokenLimit - 1) / 2
                return Substring(token.prefix(half) + "…" + token.suffix(half))
            }
            .joined(separator: " ")
        guard shortened.count > messageLimit else { return shortened }
        let half = (messageLimit - 1) / 2
        return shortened.prefix(half) + "…" + shortened.suffix(half)
    }

    var body: some View {
        HStack(alignment: .top, spacing: fit.spacing) {
            Symbols.failure.image(size: fit.iconSize)
                .foregroundStyle(.white)
                .padding(.top, fit.iconTopPadding)
            VStack(alignment: .leading, spacing: 4) {
                if let title {
                    Text(title)
                        .font(AppFont.bodyMedium)
                        .foregroundStyle(.white)
                }
                Text(Self.displayed(message))
                    .font(AppFont.monoSmall)
                    .foregroundStyle(.white)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    // Bounded, because an unbounded one takes the window with
                    // it. A message containing a long unbreakable token — a
                    // URL or a filesystem path, which is most of what this
                    // app's errors are made of — has no wrap opportunity, so
                    // its ideal width exceeds the window and *nothing* draws:
                    // no sidebar, no list, just chrome over an empty pane,
                    // until the app is relaunched.
                    //
                    // Reproduced against a 404 whose URL is 95 characters —
                    // clicking Update on a tweak whose .deb is missing — and
                    // narrowed by substituting a short message, which rendered
                    // normally. `frame(maxWidth: .infinity)` alone did not fix
                    // it; the text has to be unable to demand the width in the
                    // first place. This is the app's one general-purpose
                    // failure surface and takes whatever string an error
                    // arrived with, so it has to survive all of them.
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .lineLimit(4)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 0)
            if let onDismiss {
                Button(action: onDismiss) {
                    Symbols.dismiss.image(size: fit.dismissSize)
                        .foregroundStyle(.white.opacity(0.8))
                }
                .buttonStyle(.plain)
            }
        }
        .padding(.horizontal, fit.horizontalPadding)
        .padding(.vertical, fit.verticalPadding)
        .frame(maxWidth: .infinity, alignment: .leading)
        // `dangerSurface`, not `danger`: white on the latter is 2.77:1, which
        // fails AA and AA-large, on the one surface in the app whose whole job
        // is to be read. See `Palette.dangerSurface`.
        .background(Palette.dangerSurface)
        .clipShape(RoundedRectangle(cornerRadius: fit.cornerRadius))
        // The untruncated text, for the messages long enough to be shortened.
        // Losing the middle of a path is survivable; losing it with no way to
        // see it is not.
        .help(message)
    }
}

private extension ErrorBanner.Fit {
    var horizontalPadding: CGFloat {
        switch self {
        case .fullBleed(let gutter): return gutter
        case .inset: return 12
        }
    }

    var verticalPadding: CGFloat {
        switch self {
        case .fullBleed: return 12
        case .inset: return 8
        }
    }

    var cornerRadius: CGFloat {
        switch self {
        // Rounding a strip that runs edge-to-edge just lets the window
        // background show through four corners.
        case .fullBleed: return 0
        case .inset: return Metrics.radiusInner
        }
    }

    var spacing: CGFloat {
        switch self {
        case .fullBleed: return 10
        case .inset: return 8
        }
    }

    var iconSize: CGFloat {
        switch self {
        case .fullBleed: return 14
        case .inset: return 12
        }
    }

    /// Optical alignment: the smaller icon needs nudging down to sit on the
    /// first line of text rather than above it.
    var iconTopPadding: CGFloat {
        switch self {
        case .fullBleed: return 0
        case .inset: return 2
        }
    }

    var dismissSize: CGFloat {
        switch self {
        case .fullBleed: return 11
        case .inset: return 10
        }
    }
}

struct SearchField: View {
    let placeholder: String
    @Binding var text: String
    var onSubmit: () -> Void = {}
    /// Up (-1) and down (+1) arrow in the field, for a field with a list
    /// under it to move through. Returns whether it used the key; nil, and a
    /// key it didn't use, leave the field its usual arrow keys.
    var onMove: ((Int) -> Bool)? = nil

    var body: some View {
        HStack(spacing: 6) {
            Symbols.search.image
                .foregroundStyle(Palette.textDim)
            TextField(placeholder, text: $text)
                .textFieldStyle(.plain)
                .font(AppFont.body)
                .onSubmit(onSubmit)
                .modifier(ArrowKeysMove(onMove: onMove))
        }
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(Palette.surface)
        .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
    }
}

/// `SearchField`'s up and down arrows, through `onMove`. `onKeyPress` is
/// macOS 14's, and macOS 13 has nothing like it for a text field: there the
/// arrows keep their usual job in the field, and a result is picked by
/// clicking it, or Return for the first.
private struct ArrowKeysMove: ViewModifier {
    let onMove: ((Int) -> Bool)?

    func body(content: Content) -> some View {
        if #available(macOS 14, *) {
            content.onKeyPress(keys: [.upArrow, .downArrow]) { press in
                guard let onMove else { return .ignored }
                return onMove(press.key == .downArrow ? 1 : -1) ? .handled : .ignored
            }
        } else {
            content
        }
    }
}

struct TagChip: View {
    let label: String

    /// Drawn *instead of* `label`, for a chip whose noun has a mark of its own.
    ///
    /// The label is still required and still lays the chip out — see `content`.
    /// It is not dead: it is what the chip says to VoiceOver, so a marked chip
    /// announces the same word the text chip did.
    var mark: Mark? = nil

    var body: some View {
        content
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(Palette.surface)
            .foregroundStyle(Palette.textMuted)
            .clipShape(Capsule())
    }

    /// A `ZStack` with the label hidden underneath, rather than the mark alone,
    /// and the stack is the point of it.
    ///
    /// A chip is a *label*: `AppsListView`'s doc records what happened when this
    /// one was allowed to change size under pressure ("LC" compressed to "L",
    /// which reads as a different word rather than a shortened one). A column of
    /// these has one chip per row, and the delivery mode changes from row to
    /// row — so a marked chip that were merely as big as its mark would make the
    /// capsules step in and out down the list. A `ZStack` takes the larger of
    /// its children in each axis, so a marked chip is never smaller than the
    /// text chip it replaces and the mark can never overflow the capsule.
    ///
    /// 11pt because `AppFont.label` is 11pt: the mark is sized to the type it
    /// stands in for, which is what `Mark.frame(forPointSize:)` is for.
    @ViewBuilder
    private var content: some View {
        if let mark {
            ZStack {
                Text(label).font(AppFont.label).hidden()
                mark.image(size: 11)
            }
            .accessibilityLabel(label)
        } else {
            Text(label).font(AppFont.label)
        }
    }
}

// `DeliveryBadge` used to live here — a warn-colored dot plus "not delivered"
// / "out of date", for the 320pt apps column. When that column merged into the
// sidebar the label no longer fit, and `AppSidebarRow` carries the dot alone
// with the CLI's sentence on hover. Deleted rather than left standing: it had
// no remaining callers, and a second renderer of delivery state is exactly how
// the sidebar and the detail pane drift apart on what counts as needing
// attention. `DeliveryKind.needsAttention` is still the single decision.

/// An app's icon — real artwork when `ArtworkStore` has resolved it, and the
/// lettered tile when it hasn't.
///
/// **The tile is not a placeholder awaiting deletion.** It is the last rung
/// of the artwork cascade (see `ArtworkStore`), and it is reached for real:
/// an app that isn't on the US App Store, ships no flat icon in its IPA, and
/// isn't installed top-level on a connected phone has no artwork anywhere in
/// the chain to find. It is also what every icon draws before the first
/// `ArtworkStore.load` for it returns — this view doesn't track
/// "loading," only "has an image or doesn't." Don't delete this branch as
/// dead code; it's a load-bearing fallback, not a bootstrap stub.
///
/// The corner radius — `size * 0.235`, iOS's own squircle proportion — is
/// applied to *both* branches, and that matters for artwork specifically:
/// every source in the cascade hands back an unmasked square (a SpringBoard
/// PNG, an App Store JPEG), so this clip is the only thing that makes a
/// device-sourced icon and a store-sourced one read as the same kind of
/// object next to each other in a list. Drop it from the image branch and
/// real artwork renders as squares among rounded tiles.
///
/// One more thing the cache does that this view has to not care about: it
/// stores `.png` filenames that frequently hold JPEG bytes, because real App
/// Store artwork is JPEG and the cascade doesn't transcode it. `NSImage`
/// sniffs content rather than trusting the extension, so this is transparent
/// here — but it's why nothing in this view (or anything upstream of it)
/// should infer format from a filename.
struct AppIcon: View {
    let name: String
    let size: CGFloat
    /// nil is not an error state — see the type doc.
    var image: NSImage? = nil

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fill)
            } else {
                ZStack {
                    RoundedRectangle(cornerRadius: size * 0.235)
                        .fill(LinearGradient(
                            colors: [Palette.accent.opacity(0.85), Palette.accent.opacity(0.5)],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        ))
                    Text(initials)
                        .font(.system(size: size * 0.42, weight: .bold))
                        .foregroundStyle(.white)
                }
            }
        }
        .frame(width: size, height: size)
        .clipShape(RoundedRectangle(cornerRadius: size * 0.235))
    }

    /// The tile's text: the name's first two characters, uppercased. "?" for
    /// a name that's empty or all whitespace, rather than an empty tile.
    var initials: String {
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "?" }
        return String(trimmed.prefix(2)).uppercased()
    }

    /// Exposed for tests, which can assert on a bool far more cheaply than on
    /// a rendered view.
    var showsArtwork: Bool { image != nil }
}

enum BuildState: Equatable {
    case idle
    case running(stage: String)
    case ok
    case failed(message: String)
    /// The user stopped it. Distinct from `.failed` because the difference is
    /// the whole point: a failure is news, and this is the thing the user just
    /// asked for. Rendering a cancel as a red "Retry" reports a defect for an
    /// intentional act, and puts a "Last build failed" card under it saying
    /// nothing about the truncated file the kill may have left on the phone.
    case cancelled
}

struct BuildButton: View {
    let title: String
    let state: BuildState
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 7) {
                Circle().fill(dotColor).frame(width: 7, height: 7)
                    .shadow(color: dotColor.opacity(0.7), radius: 3)
                // The primary action in the app, and the only button here that
                // was free to reflow: with nothing pinning it, the header
                // HStack compressed this before anything else and "Build &
                // Install" rendered as three stacked lines at a 1280pt window
                // — a control three times the height of the two beside it.
                // Pinned, the pressure goes to the header's bundle-id line
                // instead, which truncates cleanly.
                Text(label)
                    .font(AppFont.bodyMedium)
                    .foregroundStyle(labelColor)
                    .lineLimit(1)
            }
            .padding(.horizontal, 14).padding(.vertical, 7)
            .background(
                LinearGradient(
                    colors: [Palette.surface.opacity(1.0), Palette.bg],
                    startPoint: .top, endPoint: .bottom
                )
            )
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Color.white.opacity(0.08), lineWidth: 0.5))
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
        }
        .buttonStyle(.plain)
        .disabled(isDisabled)
    }

    private var dotColor: Color {
        switch state {
        case .idle: return Palette.success
        case .running: return Palette.warn
        case .ok: return Palette.success
        case .failed: return Palette.danger
        case .cancelled: return Palette.warn
        }
    }

    private var labelColor: Color {
        switch state {
        case .failed: return Palette.danger
        default: return Palette.success
        }
    }

    private var label: String {
        switch state {
        case .idle: return title
        case .running(let stage): return "Building — \(stage)"
        case .ok: return "Done"
        case .failed: return "Retry"
        // Not "Canceled": the button's job is to name what pressing it does,
        // and the state is already told by the amber dot beside it plus the
        // log's own last line.
        case .cancelled: return title
        }
    }

    private var isDisabled: Bool {
        if case .running = state { return true }
        return false
    }
}
