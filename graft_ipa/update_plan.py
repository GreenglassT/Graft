"""What `graft --update` would change, per app, worked out before anything changes.

Three steps per app (spec 3.1), each skipped with a reason when it is:

- **App Store:** "get V" when the App Store's current version is newer (by
  `version_key`) than the IPA a build would use (`find_in_cache`). Skipped
  for a held app, where App Store requests can't run
  (`store_requests_blocker`), before the App Store is set up on this Mac
  (`appstore.setup_missing`), for a version the Apple ID was refused
  (`update_refusals`), and when the lookup failed. Lookups are batched: one
  request per store country (`store_versions`).
- **Tweaks:** each attached tweak with a newer build from its own source
  (`updates.statuses`, 2.3.1 A1), where it comes from, and the apps outside
  the run that share it (`also_affects`). One that couldn't be checked is
  listed and left alone; so is one whose update isn't verified
  (`unverified_reason`) until the person confirms it (`trusted`,
  `--trust-tweak`): it's `confirmed` only then, and only a confirmed one
  counts. An app with one waiting is `needs_confirmation`.
- **Install:** when either step above changes something, or the delivery
  state is `stale` because what a build would make differs from what was
  installed (a change staged since its last install). Only a real change is
  installed on its own (spec §11.1): after a deliberate Re-install of an
  archived build, or when a reconcile didn't find the last install on the
  device, the plan says so (`REINSTALLED`, `NOT_ON_DEVICE`) and offers the
  install for the person to press (`manual_install`). An app never delivered
  (`never`) is left to Build & Install; `unknown` counts as current.

All of it per target (spec §11.3, option B): every device the app was
delivered to (`cli._delivery_targets`), each with its own delivery state, so
the iPhone can be current while the iPad installs. Then readiness
(`update_readiness`), asked only of the targets with something to do: a
device that isn't connected is waiting, and the app's other targets still
run; any other "no" blocks that target. The app is blocked only when no
target with something to do can run, and `actionable` (the sidebar's
indicator, Update All's count) only when one can.

Nothing here reads the keychain or runs ipatool, with or without `quick`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import (
    appstore,
    devices_seen,
    host,
    itunes,
    preflight,
    tweak_sources,
    update_readiness,
    update_refusals,
    updates,
)
from .config import AppConfig, Config
from .errors import ITunesError
from .ipa_cache import find_in_cache
from .repo import version_key

#: The steps, as `update-step` and `update-app-end` spell them
#: (`CLIContract.Update` in the app). `install` is once per target.
STEP_TWEAKS = "tweaks"
STEP_GET = "get"
STEP_BUILD = "build"
STEP_INSTALL = "install"

#: Why the App Store step is skipped (`store.skipped_reason`).
HELD = "held at {version}"
#: Held, and the held version's file isn't staged: nothing to build from
#: until it is got again. `{build}` is " (build 1 of 2)" for a hold by build
#: of a version the App Store has more than one build of, else empty.
HELD_MISSING = (
    "held at {version}{build}, which isn't downloaded: get it again from Get an older version…"
)
NEEDS_APPLE_SILICON = "needs Apple silicon"
#: The get can't run on this Mac yet (`appstore.setup_missing`).
DECRYPTION_NOT_SET_UP = appstore.DECRYPTION_NOT_SET_UP
NOT_SIGNED_IN = appstore.NOT_SIGNED_IN
LOOKUP_FAILED = "couldn't check the App Store"
#: No colon of its own: the app's confirmation shows it as "App Store 1.2.0:
#: <reason>". Capitalized, unlike the others: the pane shows it alone, as a
#: sentence, and finds it by how it begins (`CLIContract.Update.refusedPrefix`).
REFUSED = "Your Apple ID doesn't have {version}; get it in the App Store"
#: Why the install is (`install_skipped_reason`).
NOT_INSTALLED_YET = "not installed yet"
#: Every device the app is on was dropped (`--drop-update-target`).
DROPPED_EVERYWHERE = "Update was stopped on every device it's on"
#: The delivery log wouldn't open, so no device is known.
LOG_UNREADABLE = "the delivery log can't be read"
#: Not installed on its own, and offered (`manual_install`): spec §11.1.
REINSTALLED = "Re-installed an older build"
NOT_ON_DEVICE = "the last install couldn't be found on the device"
#: An app whose deliveries were recorded without a device Graft can name.
BUILD_ONCE = "Build & Install {app} once, so Graft knows which device it's on"
#: A delivery record's `origin` for `--reinstall-build`.
ORIGIN_REINSTALL = "reinstall"
#: And for `graft --update`'s installs after the first, of the same build on
#: the app's other devices: read like a build's, never offered only.
ORIGIN_UPDATE = "update"

NEVER = "never"
STALE = "stale"


@dataclass(frozen=True)
class StoreAnswer:
    """The App Store's current version of one app, or why there is none.
    `unreachable`: the lookup itself failed (offline, or Apple refused), as
    against an answer that the store doesn't sell it."""

    version: str | None
    error: str | None = None
    unreachable: bool = False


Ask = Callable[[list[str], str], dict[str, itunes.ITunesApp]]


def _ask(bundle_ids: list[str], country: str) -> dict[str, itunes.ITunesApp]:
    return itunes.lookup_by_bundle_ids(bundle_ids, country=country)


def store_versions(apps: list[AppConfig], *, ask: Ask | None = None) -> dict[str, StoreAnswer]:
    """Each app's current App Store version, keyed by app name, from one
    lookup per store country.

    Each app's stores are its own chain (`host.lookup_countries`): its
    `store_country` alone, else this Mac's region's store, then the US. Every
    bundle id whose chain names a store goes into that store's one request,
    so a country is asked once however many apps it answers for, and an app
    takes the first store of its chain that sells it. As in `host.look_up`,
    only "not sold there" moves on down the chain: a store whose request
    failed is "couldn't check" for the apps that reached it.
    """
    resolve = ask if ask is not None else _ask
    # One chain per store setting, so this Mac's region is read once however
    # many apps have no store of their own.
    by_store: dict[str | None, tuple[str, ...]] = {}
    for app in apps:
        if app.store_country not in by_store:
            by_store[app.store_country] = host.lookup_countries(app.store_country)
    chains = {app.name: by_store[app.store_country] for app in apps}
    wanted: dict[str, list[str]] = {}
    for app in apps:
        for country in chains[app.name]:
            ids = wanted.setdefault(country, [])
            if app.bundle_id not in ids:
                ids.append(app.bundle_id)
    answers: dict[str, dict[str, itunes.ITunesApp] | ITunesError] = {}
    for country, ids in wanted.items():
        try:
            answers[country] = resolve(ids, country)
        except ITunesError as e:
            answers[country] = e
    out: dict[str, StoreAnswer] = {}
    for app in apps:
        result = StoreAnswer(None, f"no app found for bundle id {app.bundle_id} in the App Store")
        for country in chains[app.name]:
            answer = answers[country]
            if isinstance(answer, ITunesError):
                result = StoreAnswer(None, answer.message, unreachable=True)
                break
            hit = answer.get(app.bundle_id)
            if hit is not None:
                result = StoreAnswer(hit.version)
                break
        out[app.name] = result
    return out


@dataclass(frozen=True)
class StoreStep:
    """`update-plan`'s `store`: `from_version` is the IPA a build would use
    now, `to_version` the App Store's newer one, `skipped_reason` why it
    won't be fetched."""

    from_version: str | None
    to_version: str | None
    skipped_reason: str | None

    @property
    def planned(self) -> bool:
        return self.to_version is not None and self.skipped_reason is None


def store_step(cfg: Config, app: AppConfig, answer: StoreAnswer | None) -> StoreStep:
    """The App Store step for `app`, given the lookup's `answer` (None: never
    asked, for Apple's own apps, which a get refuses).

    "Newer" is against the IPA a build would use (`find_in_cache`, which
    honors a held version), so a version already staged is no step at all.
    The checks after "is it newer" are in the order the spec's table gives:
    held, Apple silicon, a version the Apple ID was refused. A refusal is
    forgotten once the App Store has something newer than what was refused.
    """
    cached = find_in_cache(cfg, app)
    current = cached.version if cached else None
    if answer is None:
        return StoreStep(current, None, None)
    if answer.version is None:
        return StoreStep(current, None, LOOKUP_FAILED)
    if current is not None and version_key(answer.version) <= version_key(current):
        return StoreStep(current, None, None)
    held = appstore.held_version(app)
    if held is not None and current is None:
        build = appstore.held_build(cfg, app)
        which = f" (build {build.number} of {build.count})" if build else ""
        return StoreStep(current, answer.version, HELD_MISSING.format(version=held, build=which))
    if held is not None:
        return StoreStep(current, answer.version, HELD.format(version=held))
    if appstore.store_requests_blocker() is not None:
        return StoreStep(current, answer.version, NEEDS_APPLE_SILICON)
    missing = appstore.setup_missing(cfg)
    if missing is not None:
        return StoreStep(current, answer.version, missing)
    refused = update_refusals.refused_version(cfg.state_dir, app)
    if refused is not None:
        if version_key(answer.version) > version_key(refused):
            update_refusals.forget(cfg.state_dir, app.name)
        else:
            return StoreStep(current, answer.version, REFUSED.format(version=refused))
    return StoreStep(current, answer.version, None)


@dataclass(frozen=True)
class DeliveryFacts:
    """An app's delivery state on one target device (`cli._delivery_targets`):
    `kind` (`never`, `stale`, `current` or `unknown`) and its `reasons`, and
    from the last delivery there its `origin` and whether a reconcile didn't
    find it on the device. `udid` is the target."""

    kind: str
    reasons: list[str] = field(default_factory=list)
    origin: str | None = None
    reconcile_missing: bool = False
    udid: str | None = None

    @property
    def offered_only(self) -> str | None:
        """Why a `stale` state alone isn't installed on its own (spec §11.1),
        or None when it is a real change."""
        if self.kind != STALE:
            return None
        if self.reconcile_missing:
            return NOT_ON_DEVICE
        if self.origin == ORIGIN_REINSTALL:
            return REINSTALLED
        return None


@dataclass(frozen=True)
class AppDeliveries:
    """Where `graft --update` installs an app (`cli._delivery_targets`, spec
    §11.3): one `DeliveryFacts` per device in its delivery history, less the
    ones dropped (`update_drops`). `delivered`: any delivery at all.
    `unplaced`: some were recorded without a device and can't be counted as
    the default's. `dropped`: the devices left out. `unreadable`: the
    delivery log wouldn't open, so no device is known."""

    targets: list[DeliveryFacts] = field(default_factory=list)
    delivered: bool = False
    unplaced: bool = False
    dropped: list[str] = field(default_factory=list)
    unreadable: bool = False


@dataclass
class TweakChange:
    """One attached tweak in `update-plan`'s `tweaks`."""

    filename: str
    name: str | None
    from_version: str | None
    to_version: str | None
    unchecked: bool
    #: What to download (`update_tweaks.update`); not in the event.
    status: updates.TweakStatus
    also_affects: list[str] = field(default_factory=list)
    #: The `confirm_key` the person gave for this tweak (`--trust-tweak`), if
    #: any. Their yes counts only while it is this update's own key.
    given_key: str | None = None

    @property
    def source(self) -> str | None:
        return self.status.available.source if self.status.available else None

    @property
    def verified(self) -> bool:
        return self.status.verified

    @property
    def unverified_reason(self) -> str | None:
        return self.status.available.unverified_reason if self.status.available else None

    @property
    def confirm_key(self) -> str | None:
        return self.status.confirm_key if self.to_version is not None else None

    @property
    def trusted(self) -> bool:
        """The person said yes to exactly this update (re-verify #2)."""
        return self.given_key is not None and self.given_key == self.confirm_key

    @property
    def trust_changed(self) -> bool:
        """The person said yes to this tweak's update, and it has changed
        since: another version, build or source. Not trusted, and the run
        holds its apps back rather than leave it out unsaid."""
        return (self.given_key is not None and not self.trusted and not self.verified
                and not self.unchecked and self.to_version is not None)

    @property
    def confirmed(self) -> bool:
        """Verified, or the person said yes: only then does it run."""
        return self.verified or self.trusted

    @property
    def planned(self) -> bool:
        return not self.unchecked and self.to_version is not None and self.confirmed

    @property
    def awaiting_confirmation(self) -> bool:
        """A newer build, shown, which runs only once the person confirms it."""
        return not self.unchecked and self.to_version is not None and not self.confirmed


@dataclass
class TargetPlan:
    """One device an app is on, in `update-plan`'s `targets`.

    `work`: the store or tweak step changes the build, or this device's own
    delivery state calls for an install. Only a target with work is asked
    about readiness, so only it can be `waiting` or blocked; it installs in
    this run (`install`) when it is neither."""

    udid: str
    device_name: str | None
    delivery: str
    lc_bundle_id: str | None
    kind: str
    work: bool = False
    #: Its delivery state alone calls for an install: what a build would make
    #: differs from what was installed there (not a Re-install, nor a reconcile).
    stale_change: bool = False
    #: Spec §11.1, on this device: offered for the person to press.
    install_skipped_reason: str | None = None
    readiness: str = update_readiness.CHECKED
    waiting: str | None = None
    blocked_reason: str | None = None
    readiness_note: str | None = None
    #: On usbmux as the plan was made, for every target, work or not: with
    #: `kind`, the device's local facts, which `waiting` and `install` aren't
    #: alone, being worked out only for a target with work.
    connected: bool = False

    @property
    def manual_install(self) -> bool:
        return self.install_skipped_reason is not None

    @property
    def ready(self) -> bool:
        return self.waiting is None and self.blocked_reason is None

    @property
    def install(self) -> bool:
        """Installs in this run."""
        return self.work and self.ready

    @property
    def label(self) -> str:
        return self.device_name or self.udid


@dataclass
class AppPlan:
    app: str
    display_name: str | None
    store: StoreStep
    tweaks: list[TweakChange]
    targets: list[TargetPlan]
    readiness: str
    #: Why the app has no target at all: never delivered, or every device
    #: dropped, or recorded only without a device Graft can name.
    no_target_reason: str | None = None
    #: Of the lookups made online for this app (the App Store's, and
    #: canister's for its tweaks), how many answered and how many couldn't be
    #: made (`checks_failed`).
    answered: int = 0
    unanswered: int = 0

    @property
    def tweak_updates(self) -> list[TweakChange]:
        return [t for t in self.tweaks if t.planned]

    @property
    def trust_changed(self) -> list[TweakChange]:
        """Its tweak updates the person confirmed that have changed since."""
        return [t for t in self.tweaks if t.trust_changed]

    @property
    def needs_confirmation(self) -> bool:
        """A tweak update waits for the person's OK (`--trust-tweak`). Alone
        it's nothing to do: an update without that OK never runs it."""
        return any(t.awaiting_confirmation for t in self.tweaks)

    @property
    def install(self) -> bool:
        return any(t.install for t in self.targets)

    @property
    def manual_install(self) -> bool:
        return any(t.manual_install for t in self.targets)

    @property
    def install_skipped_reason(self) -> str | None:
        """Why nothing installs: no target at all, or (with no target that
        has work) the first install only offered."""
        if self.no_target_reason is not None:
            return self.no_target_reason
        if any(t.work for t in self.targets):
            return None
        return next((t.install_skipped_reason for t in self.targets if t.manual_install), None)

    @property
    def blocked_reason(self) -> str | None:
        """Set only when no target with work can run: each one's reason."""
        working = [t for t in self.targets if t.work]
        if not working or any(t.ready for t in working):
            return None
        return "; ".join(t.waiting or t.blocked_reason or "" for t in working)

    @property
    def readiness_note(self) -> str | None:
        notes = [t.readiness_note for t in self.targets if t.readiness_note]
        return "; ".join(notes) if notes else None

    @property
    def nothing_to_do(self) -> bool:
        """Nothing at all, counting waiting and blocked work as work."""
        return (not self.store.planned and not self.tweak_updates
                and not any(t.work or t.manual_install for t in self.targets)
                and self.install_skipped_reason is None)

    @property
    def has_update(self) -> bool:
        """Something to update, ready or not; an app never delivered is
        Build & Install's, and an install only offered is the person's."""
        return not self.nothing_to_do and self.install_skipped_reason is None

    @property
    def waiting_only(self) -> bool:
        """Its only work is on targets waiting for their device."""
        working = [t for t in self.targets if t.work]
        return bool(working) and all(t.waiting is not None for t in working)

    @property
    def actionable(self) -> bool:
        """Something to update now, on a target that can take it. Waiting or
        blocked work alone isn't, nor an install only offered (§11.1)."""
        return self.has_update and self.install

    @property
    def runnable(self) -> bool:
        return self.actionable

    @property
    def steps(self) -> list[str]:
        """The app-wide steps this plan would run, in run order (each
        target's install is its own: `update-app-end`'s `targets`)."""
        out = []
        if self.tweak_updates:
            out.append(STEP_TWEAKS)
        if self.store.planned:
            out.append(STEP_GET)
        if self.install:
            out.append(STEP_BUILD)
        return out


def _changes(app_name: str, records: list[dict], statuses: dict[str, updates.TweakStatus],
             trusted: Mapping[str, str]) -> list[TweakChange]:
    out: list[TweakChange] = []
    for record in records:
        if app_name not in (record.get("used_by") or []):
            continue
        status = statuses.get(record["filename"])
        if status is None:
            continue
        if status.update_available and status.available is not None:
            out.append(TweakChange(record["filename"], record.get("name"), status.installed,
                                   status.available.version, False, status,
                                   given_key=trusted.get(record["filename"])))
        elif status.unchecked:
            out.append(TweakChange(record["filename"], record.get("name"), status.installed,
                                   None, True, status))
    return out


def _asked_online(status: updates.TweakStatus,
                  recorded: tweak_sources.Source | None) -> bool | None:
    """Whether canister answered for this tweak: True, it did (a version from
    any source, or "nothing published"); False, it couldn't be asked; None,
    it wasn't asked online at all.

    The same rule `updates.statuses` asks by: canister is asked for a file
    unless its recorded source is a repo, whose cached index answers alone,
    fresh or not (a stale one leaves it unchecked, which is that index's age,
    not the network's). So it's the record that says whether the network was
    tried, not which source the answer came from: a file with no record can
    be answered by a repo after canister answered too."""
    if not status.package_id or getattr(recorded, "kind", None) == tweak_sources.REPO:
        return None
    return not status.unchecked


def _lookups(cfg: Config, app_name: str, answer: StoreAnswer | None, records: list[dict],
             statuses: dict[str, updates.TweakStatus]) -> tuple[int, int]:
    """How many of `app_name`'s online lookups answered, and how many
    couldn't be made: the App Store's (none for Apple's own apps), and
    canister's for each tweak it uses."""
    asked: list[bool] = []
    if answer is not None:
        asked.append(not answer.unreachable)
    for record in records:
        status = statuses.get(record["filename"])
        if status is not None and app_name in (record.get("used_by") or []):
            filename = record["filename"]
            recorded = tweak_sources.recorded_for(
                cfg.state_dir, filename, tweak_sources.file_sha256(cfg.tweaks_dir / filename))
            online = _asked_online(status, recorded)
            if online is not None:
                asked.append(online)
    return asked.count(True), asked.count(False)


def checks_failed(plans: list[AppPlan]) -> bool:
    """The plan asked online and nothing answered: the App Store lookup and
    canister both failed, which is how an offline Mac looks. Its apps then
    read as up to date when nothing was learned (`update-plan`'s
    `checks_failed`): the app keeps what it showed and asks again later."""
    return any(p.unanswered for p in plans) and not any(p.answered for p in plans)


def _order(cfg: Config, targets: list[TargetPlan]) -> list[TargetPlan]:
    """The default device first, then by name (spec §11.3)."""
    default = cfg.default_device_udid or None
    return sorted(targets, key=lambda t: (t.udid != default, t.label.casefold(), t.udid))


def plan(
    cfg: Config,
    names: list[str],
    *,
    quick: bool,
    with_affected: bool,
    probe,
    tweak_records: Callable[[Config], list[dict]],
    delivery: Callable[[str], AppDeliveries],
    trusted: Mapping[str, str] | None = None,
) -> list[AppPlan]:
    """The plan for `names`, in the order given, then (with `with_affected`)
    the apps that share a tweak being updated, sorted, until none is added.

    `trusted`: the `confirm_key` the person confirmed for each library tweak
    (`--trust-tweak`), by filename. Such an update counts although it isn't
    verified, while it is still the update they were shown (`trust_changed`
    otherwise).

    `tweak_records` is `--tweaks`' records (`cli._collect_tweak_records`);
    only the files the covered apps use are looked up. `delivery` answers
    each app's targets and their delivery states (`cli._delivery_targets`).
    `probe` is `update_readiness`'s quick or full one, and is asked only
    about targets with something to do.
    """
    keys: Mapping[str, str] = trusted or {}
    order = list(dict.fromkeys(names))
    for name in order:
        cfg.get_app(name)  # an unknown app is refused before anything is asked
    records = tweak_records(cfg)
    users = {r["filename"]: list(r.get("used_by") or []) for r in records}
    statuses: dict[str, updates.TweakStatus] = {}
    facts: dict[str, AppDeliveries] = {}

    def facts_for(name: str) -> AppDeliveries:
        if name not in facts:
            facts[name] = delivery(name)
        return facts[name]

    def look_up(app_names: list[str]) -> None:
        covered = set(app_names)
        wanted = [r for r in records if r["filename"] not in statuses and set(users[r["filename"]]) & covered]
        if wanted:
            for status in updates.statuses(cfg, wanted):
                statuses[status.filename] = status

    look_up(order)
    while with_affected:
        updated_files = [f for f, s in statuses.items()
                         if s.update_available and (s.verified or keys.get(f) == s.confirm_key)
                         and set(users.get(f, [])) & set(order)]
        added = sorted({u for f in updated_files for u in users.get(f, [])
                        if u not in order and facts_for(u).targets})
        if not added:
            break
        order += added
        look_up(order)

    apps = [cfg.get_app(name) for name in order]
    answers = store_versions([a for a in apps if not appstore.apples_own(a.bundle_id)])
    plans: list[AppPlan] = []
    for app in apps:
        found = facts_for(app.name)
        store = store_step(cfg, app, answers.get(app.name))
        changes = _changes(app.name, records, statuses, keys)
        changes_something = store.planned or any(c.planned for c in changes)
        targets: list[TargetPlan] = []
        for f in found.targets:
            assert f.udid is not None
            stale_change = f.kind == STALE and f.offered_only is None
            targets.append(TargetPlan(
                udid=f.udid,
                device_name=devices_seen.name_of(cfg.state_dir, f.udid),
                delivery=app.delivery,
                lc_bundle_id=app.lc_bundle_id if app.delivery == update_readiness.LIVECONTAINER else None,
                kind=f.kind,
                work=changes_something or stale_change,
                stale_change=stale_change,
                install_skipped_reason=None if changes_something else f.offered_only,
                readiness=probe.readiness,
            ))
        answered, unanswered = _lookups(cfg, app.name, answers.get(app.name), records, statuses)
        plans.append(AppPlan(
            app=app.name,
            display_name=app.cyan.display_name,
            store=store,
            tweaks=changes,
            targets=targets,
            readiness=probe.readiness,
            no_target_reason=None if targets else _no_target_reason(app, found),
            answered=answered,
            unanswered=unanswered,
        ))

    asked = [(cfg.get_app(p.app), t) for p in plans for t in p.targets if t.work]
    ready = update_readiness.readiness_for(cfg, [(app, t.udid) for app, t in asked], probe)
    for app, t in asked:
        r = ready[(app.name, t.udid)]
        t.device_name = r.target.device_name or t.device_name
        t.lc_bundle_id = r.target.lc_bundle_id
        t.waiting, t.blocked_reason, t.readiness_note = r.waiting, r.blocked_reason, r.note
    # One usbmux listing, and only when there's a target to say it of: no
    # lockdown session, nothing asked of any device.
    if any(p.targets for p in plans):
        on_bus = {udid.lower() for udid, _ in preflight.list_connected_devices_with_transport()}
        for p in plans:
            for t in p.targets:
                t.connected = t.udid.lower() in on_bus
    for p in plans:
        p.targets = _order(cfg, p.targets)

    running = {p.app for p in plans if p.runnable}
    for p in plans:
        if not p.runnable:
            continue
        for change in p.tweak_updates:
            change.also_affects = sorted(
                u for u in users.get(change.filename, [])
                if u != p.app and u not in running and facts_for(u).targets
            )
    return plans


def _no_target_reason(app: AppConfig, found: AppDeliveries) -> str:
    if found.unreadable:
        return LOG_UNREADABLE
    if found.dropped:
        return DROPPED_EVERYWHERE
    if found.unplaced:
        return BUILD_ONCE.format(app=app.cyan.display_name or app.name)
    return NOT_INSTALLED_YET


def payload(plans: list[AppPlan], *, quick: bool) -> dict[str, Any]:
    """`update-plan`'s fields."""
    return {"quick": quick, "checks_failed": checks_failed(plans),
            "apps": [_app_payload(p) for p in plans]}


def _app_payload(p: AppPlan) -> dict[str, Any]:
    return {
        "app": p.app,
        "display_name": p.display_name,
        "store": {"from": p.store.from_version, "to": p.store.to_version,
                  "skipped_reason": p.store.skipped_reason},
        "tweaks": [
            {"filename": c.filename, "name": c.name, "from": c.from_version, "to": c.to_version,
             "unchecked": c.unchecked, "also_affects": list(c.also_affects),
             # Where it comes from, and whether it runs without the person's
             # OK: `verified`, or `confirmed` by `--trust-tweak` (2.3.1 A1).
             "source": c.source, "verified": c.verified,
             "unverified_reason": c.unverified_reason, "confirmed": c.confirmed,
             # What `--trust-tweak` takes to confirm exactly this update, and
             # whether a key given for it is for an update it no longer is.
             "confirm_key": c.confirm_key, "trust_changed": c.trust_changed}
            for c in p.tweaks
        ],
        "needs_confirmation": p.needs_confirmation,
        "install": p.install,
        "install_skipped_reason": p.install_skipped_reason,
        "manual_install": p.manual_install,
        "blocked_reason": p.blocked_reason,
        "readiness": p.readiness,
        "readiness_note": p.readiness_note,
        "targets": [_target_payload(t) for t in p.targets],
        "nothing_to_do": p.nothing_to_do,
        "actionable": p.actionable,
    }


def _target_payload(t: TargetPlan) -> dict[str, Any]:
    return {
        "udid": t.udid,
        "device_name": t.device_name,
        "delivery": t.delivery,
        "lc_bundle_id": t.lc_bundle_id,
        "install": t.install,
        "install_skipped_reason": t.install_skipped_reason,
        "manual_install": t.manual_install,
        # The device's own facts, for every target (`TargetPlan.connected`).
        "kind": t.kind,
        "connected": t.connected,
        "waiting": t.waiting,
        "blocked_reason": t.blocked_reason,
        "readiness": t.readiness,
        "readiness_note": t.readiness_note,
    }


def describe(p: AppPlan) -> str:
    """One line, for a person in Terminal."""
    name = p.display_name or p.app
    parts: list[str] = []
    if p.store.planned:
        parts.append(f"App Store {p.store.to_version}")
    elif p.store.to_version and p.store.skipped_reason:
        parts.append(f"App Store has {p.store.to_version} ({p.store.skipped_reason})")
    elif p.store.skipped_reason:
        parts.append(p.store.skipped_reason)
    for c in p.tweaks:
        label = c.name or c.filename
        if c.unchecked:
            parts.append(f"{label} couldn't be checked")
        elif c.trust_changed:
            parts.append(f"{label} changed since you confirmed it: it's now {c.to_version} from "
                         f"{c.source}: --trust-tweak {c.confirm_key}, if you trust it")
        elif c.awaiting_confirmation:
            parts.append(f"{label} {c.from_version or '?'} → {c.to_version} needs your OK "
                         f"({c.unverified_reason}): --trust-tweak {c.confirm_key}")
        else:
            parts.append(f"{label} {c.from_version or '?'} → {c.to_version}")
        if c.also_affects:
            parts.append(f"also used by {', '.join(c.also_affects)}: it will need an install afterward")
    if p.install:
        parts.append("rebuild & install on " + ", ".join(t.label for t in p.targets if t.install))
    for t in p.targets:
        if t.waiting:
            parts.append(t.waiting)
        elif t.blocked_reason:
            parts.append(f"blocked on {t.label}: {t.blocked_reason}")
        elif t.manual_install and p.install_skipped_reason is None:
            parts.append(f"{t.install_skipped_reason} on {t.label}")
    if p.install_skipped_reason:
        parts.append(p.install_skipped_reason)
    if not parts:
        return f"{name}: up to date"
    line = f"{name}: " + " · ".join(parts)
    if p.readiness_note:
        line += f" ({p.readiness_note})"
    return line
