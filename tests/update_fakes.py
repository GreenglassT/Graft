"""Stand-ins for `graft --update`'s tests.

A world of two LiveContainer apps, `example` and `bravo`, sharing a tweak
(`a-tweak.deb`; `bravo` also has `b-tweak.deb`), each staged and delivered
once to the one phone, with:
- the App Store lookup answered from `store` (`itunes.lookup_by_bundle_ids`),
  every request recorded in `lookups`;
- published tweak versions in `published` (`updates.canister_offers`, one
  canister repository, `REPO_URL`, which lists every build the library holds),
  each library tweak recorded as fetched from it (`tweak_sources`), so its
  updates are verified (`serve_insecure` makes one plain http), and a
  download that writes the new .deb (`canister.fetch_deb`), recorded;
- phones at usbmux's, lockdown's and installation_proxy's boundaries
  (`devices`, `lc_manage.UsbmuxDevice`, `device_info`);
- the App Store get through the fake ipatool and UnfairPlay (`FakeTools`);
- a build (`FakeBuild`) that records the delivery a push records, so the
  delivery state moves as it would.

Nothing here reaches a real device, tool, keychain or the network.
"""

from __future__ import annotations

import dataclasses
import json
import plistlib
from collections.abc import Callable
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path

import yaml

from graft_ipa import (
    canister,
    device_info,
    devices,
    itunes,
    lc_manage,
    tweak_sources,
    update_plan,
    update_run,
    updates,
)
from graft_ipa.build_log import BuildLog
from graft_ipa.config import Config, load_config
from graft_ipa.delivery import compute_inputs_digest
from graft_ipa.devices import BriefDevice, DeviceInfo, DeviceListing, DeviceLookupFailure
from graft_ipa.errors import DeviceNotConnectedError, ITunesError
from graft_ipa.ipa_cache import find_in_cache
from graft_ipa.json_output import JsonEmitter
from graft_ipa.tweak_sources import CANISTER, Source, file_sha256
from graft_ipa.update_readiness import FullProbe, QuickProbe
from graft_ipa.updates import Download, LookupUnavailable, Offer

from .appstore_fakes import FakeTools, itunes_offline, make_ipa
from .test_deb_control import _deb

PHONE = "00008130-FAKE00000000PHONE1"
OTHER_PHONE = "00008130-FAKE00000000PHONE2"
LIVECONTAINER = "com.kdt.livecontainer"
#: The canister repository every published tweak comes from.
REPO_ID = "examplerepo"
REPO_URL = "https://repo.example.com"
BUNDLES = {"example": "com.example.app", "bravo": "com.bravo.app", "delta": "com.delta.app"}
#: A signing profile's entitlements: a development one, which needs Developer
#: Mode on the phone, and an ad hoc one, which doesn't (`developer_mode.needed`).
DEVELOPMENT = {"get-task-allow": True, "application-identifier": "ABCDE12345.com.example.*"}
AD_HOC = {"get-task-allow": False, "application-identifier": "ABCDE12345.com.example.*"}


def update_deb_package(path: Path) -> str | None:
    from graft_ipa.deb import identity

    found = identity(path)
    return found[0] if found else None


def new_name(package_id: str, version: str) -> str:
    """The library name Graft gives a fetched build (`tweak_sources.library_name`)."""
    return f"{package_id}_{version}.deb"


def deb(path: Path, package_id: str, version: str, name: str | None = None) -> Path:
    """A .deb whose control stanza names `package_id` at `version`."""
    text = f"Package: {package_id}\nVersion: {version}\n" + (f"Name: {name}\n" if name else "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_deb(text=text))
    return path


def events(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.startswith("{")]


@dataclass
class FakePhone:
    name: str = "Example iPhone"
    ios_version: str = "18.1"
    livecontainer: str | None = LIVECONTAINER
    installed: list[str] = field(default_factory=lambda: [LIVECONTAINER, "com.apple.mobilesafari"])
    developer_mode: bool | None = True
    #: Set: usbmux lists it, and a lockdown session can't describe it.
    unreadable: str | None = None


class FakeBuild:
    """`Pipeline.build`: records each call (app, device, its tweaks, its IPA's
    version, as they were then), archives a stand-in IPA with the build
    inputs' digest (`update_run.Built`), then the delivery a push records.
    `fail` fails an app's build before its archive; `fail_install` after it,
    at its own install."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, str | None, list[str], str | None]] = []
        self.fail: dict[str, Exception] = {}
        self.fail_install: dict[str, Exception] = {}
        self.before: Callable[[str], None] | None = None
        #: Called as the build's own install begins, with the app.
        self.installing: Callable[[str], None] | None = None
        #: The other devices each build was told it would be installed on.
        self.also_for: list[list[str]] = []
        #: The build couldn't fingerprint its inputs (it only warns).
        self.no_digest = False

    def __call__(self, *, cfg: Config, app_name: str, device: str | None, emitter: JsonEmitter | None,
                 built: update_run.Built | None = None) -> None:
        app = cfg.get_app(app_name)
        cached = find_in_cache(cfg, app)
        self.calls.append((app_name, device, list(app.tweaks), cached.version if cached else None))
        if self.before is not None:
            self.before(app_name)
        if built is not None:
            self.also_for.append(list(built.also_for))
        if app_name in self.fail:
            raise self.fail.pop(app_name)
        if device is not None and device not in self.world.phones:
            # `_planned_target`, first thing in a real build.
            raise DeviceNotConnectedError(device, label=self.world.phones_name(device))
        assert cached is not None
        if built is not None:
            cfg.built_dir.mkdir(parents=True, exist_ok=True)
            archive = cfg.built_dir / f"{app_name}_{cached.version}_{len(self.calls)}.ipa"
            archive.write_bytes(f"built {app_name} {cached.version}".encode())
            inputs = None if self.no_digest else compute_inputs_digest(
                cfg, app, source_version=cached.version)
            built.archived(archive, inputs, cached.version)
            built.installing()
        if self.installing is not None:
            self.installing(app_name)
        if app_name in self.fail_install:
            raise self.fail_install.pop(app_name)
        self.world.deliver(app_name, udid=device)
        if built is not None:
            built.installed = True


class FakeInstall:
    """`Pipeline.install`: an install after the first, of the build's archive,
    recorded as `cli._do_update_install` records it (origin `update`).
    `fail` fails it on a device; `before` runs first, with (app, device)."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[str, str]] = []
        self.fail: dict[str, Exception] = {}
        self.before: Callable[[str, str], None] | None = None

    def __call__(self, *, cfg: Config, app_name: str, device: str, built: update_run.Built,
                 emitter: JsonEmitter | None) -> None:
        assert built.archive is not None and built.archive.exists(), "the build's own archive"
        self.calls.append((app_name, device))
        if self.before is not None:
            self.before(app_name, device)
        if device in self.fail:
            raise self.fail.pop(device)
        self.world.deliver(app_name, udid=device, origin="update")


class World:
    def __init__(self, cfg_path: Path, tools: FakeTools, tmp: Path, monkeypatch) -> None:
        self.cfg_path = cfg_path
        self.tools = tools
        self.tmp = tmp
        self.store: dict[str, str] = {}
        self.lookups: list[tuple[list[str], str | None]] = []
        self.lookup_error: str | None = None
        self.published: dict[str, str] = {}
        #: Package ids whose repository serves over plain http: unverified.
        self.insecure: set[str] = set()
        self.unreachable: set[str] = set()
        #: Every package id canister was asked about, in order.
        self.latest_asked: list[str] = []
        self.fetches: list[tuple[str, Path]] = []
        self.fetch_error: Exception | None = None
        self.phones: dict[str, FakePhone] = {PHONE: FakePhone()}
        self.sessions: list[str] = []
        self.build = FakeBuild(self)
        self.install = FakeInstall(self)
        monkeypatch.setattr(itunes, "lookup_by_bundle_ids", self._lookup)
        # A refused get looks the price up; never on the network here.
        monkeypatch.setattr(itunes, "lookup_by_bundle_id", itunes_offline)
        monkeypatch.setattr(updates, "repo_offers", lambda _cfg, max_age=updates.CACHE_MAX_AGE: {})
        monkeypatch.setattr(updates, "canister_offers", self._offers)
        monkeypatch.setattr(canister, "fetch_deb", self._fetch)
        # usbmux's list: `preflight.list_connected_devices_with_transport`,
        # and through it `installer.resolve_target`, read it at call time.
        monkeypatch.setattr(devices, "list_devices_brief", self._brief)
        monkeypatch.setattr(devices, "list_devices", self._listing)
        monkeypatch.setattr(lc_manage.UsbmuxDevice, "installed_bundle_ids",
                            lambda _self, udid: self._installed(udid))
        monkeypatch.setattr(device_info, "read_developer_mode", self._developer_mode)

    @property
    def cfg(self) -> Config:
        return load_config(self.cfg_path)

    def phones_name(self, udid: str) -> str:
        """What Graft calls `udid` in a sentence (`devices_seen.label`)."""
        from graft_ipa import devices_seen

        return devices_seen.label(self.cfg.state_dir, udid)

    # --- the cache, the delivery log ---------------------------------------

    def stage(self, app: str, version: str) -> Path:
        cfg = self.cfg
        return make_ipa(cfg.cache_dir / f"{BUNDLES[app]}_{version}.ipa", bundle_id=BUNDLES[app],
                        version=version, encrypted=False)

    def offer_get(self, version: str) -> None:
        """The next App Store get of `example` stages `version`."""
        self.tools.set(decrypted_ipa=str(make_ipa(self.tmp / f"decrypted-{version}.ipa",
                                                  bundle_id=BUNDLES["example"], version=version,
                                                  encrypted=False)))

    def downloads(self) -> int:
        return sum(1 for c in self.tools.calls("ipatool") if c["argv"][:1] == ["download"])

    def deliver(self, app_name: str, *, udid: str | None = PHONE, origin: str = "build") -> int:
        """Record the delivery a push of what a build makes now records, and
        return its id."""
        cfg = self.cfg
        app = cfg.get_app(app_name)
        cached = find_in_cache(cfg, app)
        assert cached is not None, f"nothing staged for {app_name}"
        inputs = compute_inputs_digest(cfg, app, source_version=cached.version)
        return BuildLog(cfg.state_dir / "log.sqlite").record_delivery(
            app=app_name, bundle_id=inputs.bundle_id, inputs_digest=inputs.digest,
            digest_version=inputs.version, cyan=inputs.cyan,
            tweaks=[[t.name, t.sha256] for t in inputs.tweaks], source_version=inputs.source_version,
            ipa_sha256=None, delivery=inputs.delivery, lc_bundle_id=LIVECONTAINER,
            remote_name=f"{app_name}.ipa", udid=udid, origin=origin,
        )

    def reconciled_missing(self, delivery_id: int) -> None:
        """`--reconcile` looked on the device and didn't find that delivery."""
        BuildLog(self.cfg.state_dir / "log.sqlite").mark_reconciled(delivery_id, seen=False)

    def delivered_to(self, app_name: str) -> str | None:
        """The device `app_name` was last delivered to, as its record says."""
        row = BuildLog(self.cfg.state_dir / "log.sqlite").latest_delivery(app_name)
        assert row is not None, f"{app_name} was never delivered"
        return row["udid"]

    def profile(self, entitlements: dict, devices: list[str] | None = None) -> None:
        """The config's signing profile, as `mobileprovision.decode` reads one;
        with `devices`, a development profile's `ProvisionedDevices`."""
        path = self.cfg.signing.profile
        path.parent.mkdir(parents=True, exist_ok=True)
        body: dict = {"Name": "Example", "TeamIdentifier": ["ABCDE12345"], "Entitlements": entitlements}
        if devices is not None:
            body["ProvisionedDevices"] = list(devices)
        path.write_bytes(plistlib.dumps(body, fmt=plistlib.FMT_XML))

    def kind(self, app_name: str, udid: str = PHONE) -> str:
        """`app_name`'s delivery state on `udid`."""
        from graft_ipa import cli as cli_mod

        cfg = self.cfg
        return cli_mod._delivery_state_for(cfg, app_name, cfg.get_app(app_name),
                                           BuildLog(cfg.state_dir / "log.sqlite"), udid=udid)["kind"]

    # --- the stand-ins -------------------------------------------------------

    def _lookup(self, bundle_ids: list[str], *, country: str | None = None) -> dict[str, itunes.ITunesApp]:
        self.lookups.append((list(bundle_ids), country))
        if self.lookup_error is not None:
            raise ITunesError(self.lookup_error)
        return {b: itunes.ITunesApp(track_id=1, bundle_id=b, name=b, version=self.store[b])
                for b in bundle_ids if b in self.store}

    def _offers(self, package_id: str, _repositories=None) -> list[Offer]:
        self.latest_asked.append(package_id)
        if package_id in self.unreachable:
            raise LookupUnavailable(f"canister.me didn't answer for {package_id}")
        version = self.published.get(package_id)
        if not version:
            return []
        url = REPO_URL.replace("https://", "http://") if package_id in self.insecure else REPO_URL
        library = self.cfg.tweaks_dir
        hashes = frozenset(
            file_sha256(f) or "" for f in (library.iterdir() if library.is_dir() else [])
            if f.is_file() and update_deb_package(f) == package_id
        )
        return [Offer(Source(CANISTER, REPO_ID, url), version,
                      (Download(f"{url}/debs/{package_id}_{version}.deb", "0" * 64),), hashes)]

    def _fetch(self, target: str, dest_dir: Path, expected_sha=None, *, version=None,
               on_pick=None) -> Path:
        self.fetches.append((target, dest_dir))
        if self.fetch_error is not None:
            raise self.fetch_error
        package_id, version = target.rsplit("/", 1)[-1].removesuffix(".deb").rsplit("_", 1)
        return deb(dest_dir / "download.deb", package_id, version)

    def record_source(self, filename: str, url: str = REPO_URL) -> None:
        """`filename` was fetched from the canister repository, at `url`."""
        cfg = self.cfg
        tweak_sources.record(cfg.state_dir, cfg.tweaks_dir, filename, Source(CANISTER, REPO_ID, url),
                             file_sha256(cfg.tweaks_dir / filename) or "")

    def serve_insecure(self, package_id: str) -> None:
        """The repository serves `package_id` over plain http, as it did when
        the library's file of it was fetched: its update isn't verified."""
        self.insecure.add(package_id)
        plain = REPO_URL.replace("https://", "http://")
        for f in self.cfg.tweaks_dir.iterdir():
            if f.is_file() and update_deb_package(f) == package_id:
                self.record_source(f.name, plain)

    def _brief(self) -> list[BriefDevice]:
        return [BriefDevice(udid=u, connection_type="USB") for u in self.phones]

    def _listing(self) -> DeviceListing:
        self.sessions.extend(self.phones)
        return DeviceListing(
            devices=[DeviceInfo(udid=u, name=p.name, model="iPhone16,1", ios_version=p.ios_version,
                                connection_type="USB", livecontainer_bundle_id=p.livecontainer)
                     for u, p in self.phones.items() if p.unreadable is None],
            failures=[DeviceLookupFailure(udid=u, error=p.unreadable)
                      for u, p in self.phones.items() if p.unreadable is not None],
        )

    def _installed(self, udid: str) -> list[str]:
        self.sessions.append(udid)
        return list(self.phones[udid].installed)

    def _developer_mode(self, udid: str) -> bool | None:
        self.sessions.append(udid)
        return self.phones[udid].developer_mode


def make_world(temp_config_env: Path, tmp_path: Path, monkeypatch) -> World:
    tools = FakeTools(tmp_path / "tools")
    raw = yaml.safe_load(temp_config_env.read_text())
    raw["appstore"] = tools.appstore_section()
    raw["default_device_udid"] = PHONE
    raw["apps"] = {
        "example": {"bundle_id": BUNDLES["example"], "delivery": "livecontainer",
                    "tweaks": ["a-tweak.deb"], "cyan": {"display_name": "Example+"}},
        "bravo": {"bundle_id": BUNDLES["bravo"], "delivery": "livecontainer",
                  "tweaks": ["a-tweak.deb", "b-tweak.deb"]},
    }
    temp_config_env.write_text(yaml.safe_dump(raw, sort_keys=False))
    world = World(temp_config_env, tools, tmp_path, monkeypatch)
    cfg = world.cfg
    deb(cfg.tweaks_dir / "a-tweak.deb", "com.example.atweak", "1.0", "A Tweak")
    deb(cfg.tweaks_dir / "b-tweak.deb", "com.example.btweak", "2.0", "B Tweak")
    # Fetched from the repository: a source on record, so updates verify.
    world.record_source("a-tweak.deb")
    world.record_source("b-tweak.deb")
    world.stage("example", "1.0")
    world.stage("bravo", "5.0")
    world.store = {BUNDLES["example"]: "1.0", BUNDLES["bravo"]: "5.0"}
    world.published = {"com.example.atweak": "1.0", "com.example.btweak": "2.0"}
    world.deliver("example")
    world.deliver("bravo")
    return world


def confirm_keys(world: World, names: list[str]) -> dict[str, str]:
    """Each tweak's `confirm_key` in the plan as it stands: what the person
    confirms, and `--trust-tweak` takes."""
    return {t["filename"]: t["confirm_key"] for p in plan_of(world, names).values() for t in p["tweaks"]
            if t.get("confirm_key")}


def plan_of(world: World, names: list[str], *, quick: bool = False,
            with_affected: bool = False, trusted: dict[str, str] | None = None) -> dict[str, dict]:
    """The `update-plan` payload's apps, by name."""
    from graft_ipa import cli as cli_mod

    cfg = world.cfg
    probe = QuickProbe(cfg.state_dir) if quick else FullProbe(cfg.state_dir)
    plans = update_plan.plan(cfg, names, quick=quick, with_affected=with_affected, probe=probe,
                             tweak_records=cli_mod._collect_tweak_records,
                             delivery=cli_mod._delivery_targets(cfg), trusted=trusted or {})
    return {a["app"]: a for a in update_plan.payload(plans, quick=quick)["apps"]}


def pipeline(world: World, emitter: JsonEmitter | None, *, real_install: bool = False):
    """The CLI's own pipeline, with the build stood in for, and the installs
    after the first too unless `real_install`."""
    from graft_ipa import cli as cli_mod

    pipe = dataclasses.replace(cli_mod._update_pipeline(emitter), build=world.build)
    return pipe if real_install else dataclasses.replace(pipe, install=world.install)


def emitter() -> tuple[JsonEmitter, StringIO]:
    out = StringIO()
    return JsonEmitter(stream=out), out
