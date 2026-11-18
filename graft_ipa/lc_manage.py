"""Install, update and look after LiveContainer itself — signed correctly.

`lc_runner` delivers *into* a LiveContainer. This module manages the
LiveContainers: which are on a device and at what version (`--lc-status`),
putting upstream's latest on as instance 1, 2 and/or 3 (`--lc-install`), and
the two chores that follow an install: handing LiveContainer the signing
certificate for its JIT-less mode (`--lc-push-cert`) and removing what we left
in its Documents folder (`--lc-clean`).

**Why installing it is this tool's job now.** LiveContainer used to be a
config app with `delivery: sideload`, built through zsign. zsign copies a
wildcard profile's entitlements into the signature literally, which switches
off LiveContainer's keychain redirection — and then no guest app can keep a
login. `lc_sign` signs it the way AltStore does; `refuse_legacy_build` stops
the old path from quietly coming back.

**AltStore-managed devices are left alone.** On a device with AltStore or
SideStore, those LiveContainers are signed with AltStore's own certificate,
every guest app's JIT-less signature hangs off it, and AltStore refreshes its
stored copy back over anything else on its schedule. Replacing them would
orphan the former and lose to the latter, so `install` refuses there without
`force`, and says how to update through AltStore instead.

Every external effect goes through a seam — `DeviceOps` for the phone, a
profile source for App Store Connect, `lc_sign.Runner` for `codesign`,
`security` and `ideviceinstaller`, `fetch_ipa` / `get_json` / `download` for
GitHub — so the tests touch none of them.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import plistlib
import re
import signal
import subprocess
import tempfile
import threading
import uuid
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import USER_AGENT, lc_instance, lc_sign, mobileprovision
from .apple_portal import AppIdNotRegistered
from .errors import ConfigError, InstallError
from .json_output import JsonEmitter
from .lc_runner import LiveContainerError

#: The AltStore sources upstream publishes; also what a user adds to AltStore.
STABLE_SOURCE_URL = "https://github.com/LiveContainer/LiveContainer/releases/download/1.0/apps.json"
NIGHTLY_SOURCE_URL = (
    "https://github.com/LiveContainer/LiveContainer/releases/download/nightly/apps_nightly.json"
)
SOURCES = {"stable": STABLE_SOURCE_URL, "nightly": NIGHTLY_SOURCE_URL}
#: Nightly is what upstream recommends: it marks the latest stable "outdated".
DEFAULT_CHANNEL = "nightly"
RELEASES_API = "https://api.github.com/repos/LiveContainer/LiveContainer/releases/tags/"
ASSET_NAME = "LiveContainer.ipa"
CERT_NAME = "AppleDevelopment.p12"
#: Said wherever the certificate is pushed. LiveContainer's guest apps run in
#: its sandbox and can read its Documents folder, so the .p12 should not stay
#: there a moment longer than the import needs.
CERT_STAYS_NOTE = (
    "The certificate stays in LiveContainer's Documents, readable by its guest "
    "apps, until you remove it: import it, then run --lc-clean --cert-only "
    "(Remove certificate in the app)."
)
#: AltStore's and SideStore's own bundle ids, matched on dot boundaries
#: *anywhere* in an installed id. Not a prefix: on the iPhone AltStore is
#: installed as `com.ABCDE12345.com.rileytestut.AltStore` — team id first —
#: and the team-suffixed `….AltStore.<TEAM>` form exists too.
ALTSTORE_IDS = re.compile(
    r"(^|\.)(com\.rileytestut\.altstore|com\.sidestore\.sidestore)($|\.)", re.IGNORECASE
)
#: How long an install may run before we say why it might be stuck. iOS stages
#: an install over a *running* LiveContainer and applies it only once the app
#: is closed, and `ideviceinstaller` blocks until then.
WAIT_NOTICE_SECONDS = 20.0
WAITING_MESSAGE = "close LiveContainer on the device to finish"

@contextlib.contextmanager
def exit_on_termination():
    """Turn SIGTERM / SIGHUP into `SystemExit` for the duration, so `finally`
    blocks run — above all `lc_sign.temp_keychain`'s, which takes the
    throwaway keychain back off the user's search list.

    Python's default for both signals kills the process outright, skipping
    every `finally`. SIGTERM is exactly what the GUI's Stop and CLIRunner's
    cancellation send, so without this a stopped install left an unlocked
    keychain on the search list. The previous handlers come back afterwards.
    Main thread only, which every CLI verb is.
    """

    # `signal.signal` raises ValueError off the main thread. The verbs always
    # run on it; anything that does not gets its body run unguarded rather
    # than an exception (the same guard the App Store branch's
    # `_stop_cleans_up` uses).
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _exit(signum: int, _frame: object) -> None:
        raise SystemExit(128 + signum)

    previous ={sig: signal.signal(sig, _exit) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class LcEvents:
    """Where this module's output goes: the JSON event stream, or a terminal.

    One method per event, each with its event type written out as a literal
    `emit("…")`. That is not ceremony: `tests/cli_ast.py` reads every
    `.emit()` in the package to check the type is registered and decoded by
    the GUI, and a generic forwarder taking the type as a variable would be
    invisible to it — exactly the hole that let `device-error` ship unread.

    `warn` is the CLI's `_warn`, so a warning reaches both channels the way
    every other warning in this tool does.
    """

    def __init__(
        self,
        emitter: JsonEmitter | None,
        *,
        say: Callable[[str], None] = print,
        warn: Callable[[str], None] = print,
    ) -> None:
        self.emitter, self.say, self.warn = emitter, say, warn

    def status(self, **f: Any) -> None:
        if self.emitter is not None:
            self.emitter.emit("lc-status", **f)
        else:
            self.say(human_line("lc-status", f))

    def progress(self, udid: str, instance: int | None, step: str, detail: str) -> None:
        f = {"udid": udid, "instance": instance, "step": step, "detail": detail}
        if self.emitter is not None:
            self.emitter.emit("lc-install-progress", **f)
        else:
            self.say(human_line("lc-install-progress", f))

    def waiting(self, **f: Any) -> None:
        if self.emitter is not None:
            self.emitter.emit("lc-install-waiting", **f)
        else:
            self.say(human_line("lc-install-waiting", f))

    def installed(self, **f: Any) -> None:
        if self.emitter is not None:
            self.emitter.emit("lc-installed", **f)
        else:
            self.say(human_line("lc-installed", f))

    def cert_pushed(self, **f: Any) -> None:
        if self.emitter is not None:
            self.emitter.emit("lc-cert-pushed", **f)
        else:
            self.say(human_line("lc-cert-pushed", f))

    def cleaned(self, **f: Any) -> None:
        if self.emitter is not None:
            self.emitter.emit("lc-cleaned", **f)
        else:
            self.say(human_line("lc-cleaned", f))

    def note(self, line: str) -> None:
        if self.emitter is not None:
            self.emitter.emit("log", source="graft", line=line)
        else:
            self.say(line)

    def warning(self, line: str) -> None:
        self.warn(line)


# ---------- arguments ----------


def check_channel(channel: str | None) -> str:
    channel = channel or DEFAULT_CHANNEL
    if channel not in SOURCES:
        raise ConfigError(f"--channel must be nightly or stable, not {channel!r}")
    return channel


def parse_instances(text: str | None) -> tuple[int, ...]:
    """`--instances 1,3` → (1, 3). Default: all three."""
    if text is None:
        return lc_instance.INSTANCES
    try:
        picked = {int(part) for part in text.split(",")}
    except ValueError:
        picked = set()
    if not picked or not picked <= set(lc_instance.INSTANCES):
        raise ConfigError(f"--instances takes a comma list of 1, 2 and 3, not {text!r}")
    return tuple(sorted(picked))


# ---------- releases ----------


@dataclass(frozen=True)
class Release:
    channel: str
    version: str
    url: str


def _get_json(url: str) -> Any:
    import requests

    try:
        resp = requests.get(
            url, timeout=30,
            headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        raise LiveContainerError(
            f"could not read {url}: {e}", hint="check the network connection"
        ) from e


def _download(url: str, dest: Path) -> None:
    import requests

    try:
        with requests.get(url, timeout=60, stream=True,
                          headers={"User-Agent": USER_AGENT}) as resp:
            resp.raise_for_status()
            with dest.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=1 << 16):
                    f.write(chunk)
    except requests.RequestException as e:
        raise LiveContainerError(f"could not download {url}: {e}") from e


def latest_release(channel: str, *, get_json: Callable[[str], Any] = _get_json) -> Release:
    """The newest LiveContainer on `channel`, as upstream's AltStore source says.

    The source rather than the GitHub release API because the nightly's tag is
    literally `nightly` — only the source names its version. It is also exactly
    what an AltStore user subscribed to that source would be offered.
    """
    channel = check_channel(channel)
    data = get_json(SOURCES[channel])
    apps = data.get("apps") if isinstance(data, dict) else None
    app = next(
        (a for a in (apps if isinstance(apps, list) else [])
         if isinstance(a, dict) and a.get("bundleIdentifier") == lc_instance.LC_ORIGINAL_ID),
        None,
    )
    if app is None or not app.get("version") or not app.get("downloadURL"):
        raise LiveContainerError(f"the {channel} source lists no LiveContainer release")
    return Release(channel, check_version(str(app["version"])), str(app["downloadURL"]))


def release_api_url(channel: str, version: str) -> str:
    return RELEASES_API + ("nightly" if channel == "nightly" else version)


def asset_digest(release: Release, *, get_json: Callable[[str], Any] = _get_json) -> str | None:
    """The sha256 GitHub publishes for `LiveContainer.ipa`, or None."""
    data = get_json(release_api_url(release.channel, release.version))
    assets = data.get("assets") if isinstance(data, dict) else None
    if not isinstance(assets, list) or not all(isinstance(a, dict) for a in assets):
        # Raised, not None: "GitHub publishes no digest" and "GitHub answered
        # something we cannot read" are different, and `ensure_ipa` says which.
        raise LiveContainerError("the GitHub release API answered in an unexpected shape")
    for asset in assets:
        if asset.get("name") == ASSET_NAME:
            digest = asset.get("digest") or ""
            return digest.removeprefix("sha256:") if digest.startswith("sha256:") else None
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _ipa_version(ipa: Path) -> str:
    with zipfile.ZipFile(ipa) as z:
        name = next(
            (n for n in z.namelist()
             if n.startswith("Payload/") and n.endswith(".app/Info.plist") and n.count("/") == 2),
            None,
        )
        if name is None:
            raise LiveContainerError(f"{ipa.name} has no Payload/*.app/Info.plist")
        return str(plistlib.loads(z.read(name)).get("CFBundleShortVersionString", ""))


#: What a version may look like before it becomes part of a file name. Both
#: sources of one — the upstream source JSON and the downloaded IPA's
#: CFBundleShortVersionString — come off the network.
_VERSION = re.compile(r"[0-9A-Za-z.+-]+")


def check_version(version: str) -> str:
    if not _VERSION.fullmatch(version):
        raise LiveContainerError(
            f"refusing LiveContainer version {version!r}: not a plain version string"
        )
    return version


def cached_ipa_path(cache_dir: Path, version: str) -> Path:
    """The same naming the build cache uses for any other app."""
    return cache_dir / f"{lc_instance.LC_ORIGINAL_ID}_{check_version(version)}.ipa"


def ensure_ipa(
    release: Release,
    cache_dir: Path,
    *,
    get_json: Callable[[str], Any] = _get_json,
    download: Callable[[str, Path], None] = _download,
    note: Callable[[str], None] = lambda message: None,
    warn: Callable[[str], None] = lambda message: None,
) -> Path:
    """The release's IPA in the cache, verified against GitHub's digest.

    A cached file is reused only if it matches the digest (a nightly can be
    rebuilt under the same version number). A download that does not match is
    deleted, never cached. A download is cached under the version its own
    Info.plist states, since nightly can move between the lookup and the fetch.

    The digest lookup failing (rate limit, outage) is treated like a release
    that publishes none — warned about, not fatal — and a cached copy of that
    version is used as it is. What is never accepted is a digest that *is*
    known and does not match.
    """
    cached = cached_ipa_path(cache_dir, release.version)
    try:
        digest = asset_digest(release, get_json=get_json)
    except LiveContainerError as e:
        digest = None
        then = ("using the cached copy unverified" if cached.exists()
                else "downloading it unverified")
        warn(f"couldn't fetch GitHub's sha256 for {ASSET_NAME} ({e.message}); {then}")
    else:
        if digest is None:
            note("GitHub publishes no sha256 digest for this LiveContainer.ipa; not verified")
    if cached.exists() and (digest is None or _sha256(cached) == digest):
        return cached

    cache_dir.mkdir(parents=True, exist_ok=True)
    part = cache_dir / f".{lc_instance.LC_ORIGINAL_ID}-{uuid.uuid4().hex[:8]}.part"
    try:
        download(release.url, part)
        if digest is not None and _sha256(part) != digest:
            raise LiveContainerError(
                f"downloaded {ASSET_NAME} does not match the sha256 GitHub publishes",
                hint="try again; if it persists, the release may have been replaced",
            )
        final = cached_ipa_path(cache_dir, _ipa_version(part) or release.version)
        part.replace(final)
        return final
    finally:
        part.unlink(missing_ok=True)


def fetch_ipa(
    channel: str, cache_dir: Path, *, note: Callable[[str], None], warn: Callable[[str], None]
) -> tuple[Release, Path]:
    release = latest_release(channel)
    return release, ensure_ipa(release, cache_dir, note=note, warn=warn)


# ---------- status ----------


def version_key(version: str) -> tuple:
    """Numeric where it can be: '3.8.14' sorts after '3.8.0'."""
    # (0, n) / (1, text) so a non-numeric part never meets an int in a
    # comparison, which would raise rather than answer.
    return tuple(
        (0, int(p)) if p.isdigit() else (1, p) for p in version.replace("-", ".").split(".")
    )


def role_of(bundle_id: str, team: str) -> str | None:
    """`primary`, `2`, `3`, `legacy` (any other LiveContainer), or None."""
    for n in lc_instance.INSTANCES:
        if bundle_id == lc_instance.instance_bundle_id(n, team):
            return "primary" if n == 1 else str(n)
    if bundle_id.lower().startswith(lc_instance.LC_ORIGINAL_ID):
        return "legacy"
    return None


def altstore_present(apps: dict[str, dict]) -> bool:
    """AltStore or SideStore manages LiveContainer on this device.

    Two signals, either one enough.

    The store app itself being installed, matched by `ALTSTORE_IDS`. This
    used to be a `startswith` check, and the real `--lc-status` against the
    AltStore-managed iPhone on 2026-11-17 answered `unknown` because of it:
    AltStore *was* among its User apps, as `com.ABCDE12345.com.rileytestut.AltStore`
    — team id first — which no prefix test can match. Answering `unknown`
    there would have let `--lc-install` replace AltStore's copies without
    `--force`.

    Second line of defence: a LiveContainer carrying the `ALT…` Info.plist keys
    AltStore is understood to write into apps it installs (`ALTCertificateID`
    is how LiveContainer looks for AltStore's certificate; upstream's own IPA
    has none). **Unconfirmed on a real AltStore install** — the iPhone was not
    reachable again to read its LiveContainers' keys — so it is kept only as a
    backstop for the check above, never relied on alone.
    """
    if any(ALTSTORE_IDS.search(b) for b in apps):
        return True
    return any(
        b.lower().startswith(lc_instance.LC_ORIGINAL_ID)
        and any(str(k).startswith("ALT") for k in (meta or {}))
        for b, meta in apps.items()
    )


def _instances(apps: dict[str, dict], team: str) -> list[dict]:
    order = {"primary": 0, "2": 1, "3": 2, "legacy": 3}
    found = []
    for bundle_id, meta in apps.items():
        role = role_of(bundle_id, team)
        if role is None:
            continue
        app_id = str((meta.get("Entitlements") or {}).get("application-identifier", ""))
        found.append({
            "bundle_id": bundle_id,
            "version": str(meta.get("CFBundleShortVersionString") or meta.get("CFBundleVersion") or ""),
            "role": role,
            # The zsign defect, visible from outside: such a copy's keychain
            # hook is off, and iOS refuses to upgrade it in place.
            "wildcard_signed": app_id.endswith("*"),
        })
    return sorted(found, key=lambda i: (order[i["role"]], i["bundle_id"]))


def status_payload(
    *, udid: str, apps: dict[str, dict], team: str, ours: set[str], latest: Release | None
) -> dict[str, Any]:
    """One `lc-status` event.

    `managed_by`: `altstore` whenever AltStore/SideStore is on the device —
    even over our own record, since AltStore will refresh its copy back over
    ours; else `graft` if we recorded installing one of the instances
    present; else `unknown` (installed by hand, or nothing installed).
    """
    instances = _instances(apps, team)
    if altstore_present(apps):
        managed_by = "altstore"
    elif ours & {i["bundle_id"] for i in instances}:
        managed_by = "graft"
    else:
        managed_by = "unknown"
    update = latest is not None and any(
        version_key(i["version"]) < version_key(latest.version)
        for i in instances if i["role"] != "legacy"
    )
    return {
        "udid": udid,
        "reachable": True,
        "instances": instances,
        "managed_by": managed_by,
        "latest": {"version": latest.version, "channel": latest.channel} if latest else None,
        "update_available": update,
        "sources": dict(SOURCES),
    }


def unreachable_payload(udid: str, error: str, latest: Release | None = None) -> dict[str, Any]:
    """`lc-status` for a device that did not answer — said, not skipped."""
    return {
        "udid": udid,
        "reachable": False,
        "error": error,
        "instances": [],
        "managed_by": "unknown",
        "latest": {"version": latest.version, "channel": latest.channel} if latest else None,
        "update_available": False,
        "sources": dict(SOURCES),
    }


# ---------- the device seam ----------


class DeviceOps(Protocol):
    def installed_apps(self, udid: str) -> dict[str, dict]: ...
    def list_documents(self, udid: str, bundle_id: str) -> list[str]: ...
    def push_document(self, udid: str, bundle_id: str, name: str, data: bytes) -> None: ...
    def remove_document(self, udid: str, bundle_id: str, name: str) -> None: ...


class UsbmuxDevice:
    """`DeviceOps` over pymobiledevice3 — usbmux, so Wi-Fi-paired devices too."""

    def installed_apps(self, udid: str) -> dict[str, dict]:
        return self._run(udid, self._apps(udid))

    def list_documents(self, udid: str, bundle_id: str) -> list[str]:
        async def go(ha):
            return [Path(p).name for p in await ha.listdir("Documents")]
        return self._run(udid, self._sandbox(udid, bundle_id, go))

    def push_document(self, udid: str, bundle_id: str, name: str, data: bytes) -> None:
        async def go(ha):
            await ha.set_file_contents(f"Documents/{_plain(name)}", data)
        self._run(udid, self._sandbox(udid, bundle_id, go))

    def remove_document(self, udid: str, bundle_id: str, name: str) -> None:
        async def go(ha):
            await ha.rm(f"Documents/{_plain(name)}")
        self._run(udid, self._sandbox(udid, bundle_id, go))

    @staticmethod
    def _run(udid: str, coro):
        """pymobiledevice3's own exceptions, as one this CLI reports cleanly."""
        try:
            return asyncio.run(coro)
        except LiveContainerError:
            raise
        except Exception as e:
            raise LiveContainerError(
                f"could not talk to {udid}: {e}",
                hint="is the device connected (USB or Wi-Fi), unlocked and trusted?",
            ) from e

    @staticmethod
    async def _apps(udid: str) -> dict[str, dict]:
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.installation_proxy import InstallationProxyService

        from .device_session import closing_best_effort

        lock = await create_using_usbmux(serial=udid)
        async with closing_best_effort(lock):
            proxy = InstallationProxyService(lockdown=lock)
            async with closing_best_effort(proxy):
                return dict(await proxy.get_apps(application_type="User"))

    @staticmethod
    async def _sandbox(udid: str, bundle_id: str, body):
        from pymobiledevice3.lockdown import create_using_usbmux
        from pymobiledevice3.services.house_arrest import HouseArrestService

        from .device_session import closing_best_effort

        lock = await create_using_usbmux(serial=udid)
        async with closing_best_effort(lock):
            ha = await HouseArrestService.create(lockdown=lock, bundle_id=bundle_id)
            async with closing_best_effort(ha):
                return await body(ha)


def _plain(name: str) -> str:
    if Path(name).name != name or name in ("", ".", ".."):
        raise LiveContainerError(f"refusing a Documents path that is not a plain file name: {name!r}")
    return name


def usbmux_transport(udid: str) -> str:
    """`USB` or `Network`, as usbmux sees the device right now."""
    from .devices import list_devices_brief

    for dev in list_devices_brief():
        if dev.udid == udid:
            return dev.connection_type
    raise InstallError(f"device {udid} is not connected", hint="connect it, or pair it over Wi-Fi")


# ---------- profiles ----------


class ProfileSource(Protocol):
    def exact(self, bundle_id: str) -> Path: ...
    def wildcard(self, bundle_id: str) -> Path: ...


class AscProfiles:
    """Profiles from App Store Connect for the configured devices.

    `exact` never registers an App ID: LiveContainer's need an App Group, which
    the API cannot assign (`AppIdNotRegistered`). `wildcard` is the ordinary
    auto-profile path, for the one extension that has no App ID of its own.
    """

    def __init__(self, cfg: Any, password: str) -> None:
        from . import apple_portal, signer

        asc = cfg.app_store_connect
        if asc is None:
            raise ConfigError(
                "--lc-install needs an app_store_connect section",
                hint="run `graft --setup-portal`",
            )
        self._portal = apple_portal
        self._client = apple_portal.ASCClient(apple_portal.PortalCredentials(
            key_id=asc.key_id, issuer_id=asc.issuer_id, key_path=asc.key_file,
        ))
        self._serial = apple_portal.read_p12_cert_serial(cfg.signing.cert, password)
        self._udids = signer.auto_profile_device_udids(cfg)
        self._cache = asc.profile_cache_dir

    def _ensure(self, bundle_id: str, *, exact: bool) -> Path:
        return self._portal.ensure_profile_for_bundle(
            self._client, bundle_id=bundle_id, cert_serial=self._serial,
            device_udids=self._udids, cache_dir=self._cache, exact=exact,
            register_missing=not exact,
        ).profile_path

    def exact(self, bundle_id: str) -> Path:
        return self._ensure(bundle_id, exact=True)

    def wildcard(self, bundle_id: str) -> Path:
        return self._ensure(bundle_id, exact=False)


#: Extensions that may be signed on the `com.kdt.*` wildcard when they have no
#: App ID of their own. Only ShareExtension: AltStore never registered one for
#: it, and it works without the app group. Every other extension stops the
#: install instead — see `_resolve_profiles`.
WILDCARD_OK_EXTENSIONS = frozenset({"ShareExtension"})

_APP_ID_HINT = (
    "register it at developer.apple.com -> Identifiers with the App "
    "Groups capability and assign group.com.SideStore.SideStore.<TEAM>; "
    "App Group assignment cannot be done through the App Store Connect API"
)


def _resolve_profiles(
    prepared: lc_instance.PreparedInstance, profiles: ProfileSource, events: LcEvents
) -> dict[str, Path]:
    try:
        by_id = {prepared.bundle_id: profiles.exact(prepared.bundle_id)}
    except AppIdNotRegistered as e:
        # Raised as ours so the GUI shows it like every other refusal; the
        # hint is the part worth reading.
        raise LiveContainerError(e.message, hint=_APP_ID_HINT) from e
    for appex_id in prepared.appex_ids.values():
        try:
            by_id[appex_id] = profiles.exact(appex_id)
        except AppIdNotRegistered as e:
            if appex_id.rsplit(".", 1)[-1] not in WILDCARD_OK_EXTENSIONS:
                # LiveProcess and LaunchAppExtension need the shared app
                # group, which the wildcard profile cannot carry. Signing them
                # on it anyway would install a LiveContainer that looks fine
                # and cannot reach its own group: stop and say so instead.
                raise LiveContainerError(e.message, hint=_APP_ID_HINT) from e
            events.note(f"{appex_id} has no App ID of its own; signing it on the "
                        "wildcard profile, without app groups")
            by_id[appex_id] = profiles.wildcard(appex_id)
    return by_id


# ---------- install ----------


def altstore_refusal(udid: str) -> LiveContainerError:
    return LiveContainerError(
        f"LiveContainer on {udid} is managed by AltStore — update it there",
        hint=(
            "In AltStore: Browse -> Sources -> + and add the LiveContainer source "
            f"(stable: {STABLE_SOURCE_URL} ; nightly: {NIGHTLY_SOURCE_URL}), then "
            "update LiveContainer from it. Replacing AltStore's copies would orphan "
            "every guest app's JIT-less signature, and AltStore would refresh its "
            "own copy back over ours. Pass --force to replace them anyway."
        ),
    )


def _install_hint(output: str) -> str:
    if "MismatchedApplicationIdentifierEntitlement" in output:
        return (
            "the LiveContainer on the device was signed with a wildcard application "
            "identifier (an old zsign-signed copy), and iOS refuses to upgrade it in "
            "place. Delete it on the device, then install again."
        )
    from .installer import _install_hint as generic

    return generic(output)


def _install_one(
    runner: lc_sign.Runner, *, udid: str, transport: str, ipa: Path, instance: int,
    bundle_id: str, events: LcEvents, wait_notice_seconds: float,
) -> None:
    cmd = ["ideviceinstaller", "-u", udid]
    if transport.lower().startswith("network"):
        cmd.append("--network")
    cmd += ["install", str(ipa)]
    proc = runner.spawn(cmd)
    try:
        output, _ = proc.communicate(timeout=wait_notice_seconds)
    except subprocess.TimeoutExpired:
        events.waiting(udid=udid, instance=instance, bundle_id=bundle_id,
                       message=WAITING_MESSAGE)
        output, _ = proc.communicate()
    output = output or ""
    if proc.returncode != 0 or "ERROR" in output or "Error occurred" in output:
        raise InstallError(
            f"installing {bundle_id} failed (ideviceinstaller exit {proc.returncode})",
            hint=_install_hint(output),
        )


def install(
    *,
    udid: str,
    team: str,
    instances: Iterable[int],
    channel: str,
    force: bool,
    p12: Path,
    p12_password: str,
    profiles: ProfileSource,
    device_ops: DeviceOps,
    runner: lc_sign.Runner,
    transport: Callable[[str], str],
    log: Any,
    events: LcEvents,
    fetch_ipa: Callable[[str], tuple[Release, Path]],
    load_profile: Callable[[Path], dict] = mobileprovision.decode,
    workdir: Path | None = None,
    wait_notice_seconds: float = WAIT_NOTICE_SECONDS,
) -> list[dict[str, Any]]:
    """Put upstream's latest LiveContainer on `udid` as each of `instances`.

    Three phases, in this order on purpose:

    1. prepare every instance and resolve every profile — the cheap failures
       (a missing App ID) stop the run before anything else happens;
    2. sign them all, inside one throwaway keychain;
    3. install them one by one, after the keychain is gone — an install over a
       running LiveContainer can block until the user closes it, and the
       keychain must not sit on the user's search list for that long.
    """
    instances = tuple(instances)
    apps = device_ops.installed_apps(udid)
    # A locked device answers with no apps at all, and "no apps" contains no
    # AltStore — so without this the AltStore check would pass by default on
    # exactly the device it cannot see into.
    if not any(isinstance(b, str) and b for b in apps):
        raise LiveContainerError(
            "couldn't list the device's apps — unlock it and try again"
        )
    if altstore_present(apps) and not force:
        raise altstore_refusal(udid)

    events.progress(udid, None, "fetch", f"LiveContainer ({channel})")
    release, ipa = fetch_ipa(channel)
    sha = _sha256(ipa)

    with contextlib.ExitStack() as stack:
        if workdir is None:
            workdir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="lcinstall-")))
        workdir.mkdir(parents=True, exist_ok=True)

        staged: list[tuple[int, lc_instance.PreparedInstance, Path, dict[str, Path]]] = []
        for n in instances:
            events.progress(udid, n, "prepare", lc_instance.instance_name(n))
            app = lc_instance.extract_app(ipa, workdir / f"i{n}")
            prepared = lc_instance.prepare_app(app, team=team, instance=n)
            events.progress(udid, n, "profile", prepared.bundle_id)
            staged.append((n, prepared, app, _resolve_profiles(prepared, profiles, events)))

        signed: list[tuple[int, lc_instance.PreparedInstance, Path]] = []
        identity = lc_sign.p12_identity(p12, p12_password)
        with lc_sign.temp_keychain(runner, p12, p12_password, workdir) as keychain:
            for n, prepared, app, by_id in staged:
                events.progress(udid, n, "sign", prepared.bundle_id)
                for report in lc_sign.sign_app(
                    app, profile_for=by_id.__getitem__, identity=identity,
                    keychain=keychain, runner=runner, workdir=workdir / f"i{n}",
                    load_profile=load_profile,
                ):
                    events.note(report.summary())
                out = workdir / f"{lc_instance.instance_name(n)}.ipa"
                lc_instance.archive_payload(workdir / f"i{n}", out)
                signed.append((n, prepared, out))

        results = []
        how = transport(udid)
        for n, prepared, out in signed:
            events.progress(udid, n, "install", prepared.bundle_id)
            _install_one(
                runner, udid=udid, transport=how, ipa=out, instance=n,
                bundle_id=prepared.bundle_id, events=events,
                wait_notice_seconds=wait_notice_seconds,
            )
            log.record_lc_install(udid=udid, bundle_id=prepared.bundle_id,
                                  version=release.version, channel=release.channel,
                                  ipa_sha256=sha)
            result = {"udid": udid, "bundle_id": prepared.bundle_id, "instance": n,
                      "version": release.version, "channel": release.channel}
            events.installed(**result)
            results.append(result)
    return results


# ---------- certificate and clean-up ----------


def _primary_installed(udid: str, team: str, device_ops: DeviceOps) -> str:
    primary = lc_instance.instance_bundle_id(1, team)
    if primary not in device_ops.installed_apps(udid):
        raise LiveContainerError(
            f"{primary} is not installed on {udid}", hint="run --lc-install first"
        )
    return primary


def push_cert(*, udid: str, team: str, p12: Path, device_ops: DeviceOps, events: LcEvents) -> dict:
    """Put the `.p12` where LiveContainer's Settings -> Import Certificate can
    pick it (On My iPhone -> LiveContainer). A graft-signed LiveContainer
    has no AltStore to borrow a certificate from, and JIT-less mode needs one.
    The password is typed on the device; it never leaves this Mac."""
    primary = _primary_installed(udid, team, device_ops)
    device_ops.push_document(udid, primary, CERT_NAME, p12.read_bytes())
    payload = {"udid": udid, "bundle_id": primary, "path": f"Documents/{CERT_NAME}",
               "note": CERT_STAYS_NOTE}
    events.cert_pushed(**payload)
    return payload


def clean(
    *, udid: str, team: str, device_ops: DeviceOps, pushed_names: set[str], events: LcEvents,
    cert_only: bool = False,
) -> dict:
    """Remove the certificate and the IPAs we pushed — nothing else.

    Only `.ipa` names this tool is known to have pushed are candidates: a
    pairing file, a profile or an IPA the user put there by hand are not ours
    to delete, and a Documents folder is the user's.

    `cert_only` removes the certificate and nothing else — the step right
    after Import Certificate, when the IPAs are still waiting to be imported.
    """
    primary = _primary_installed(udid, team, device_ops)
    docs = device_ops.list_documents(udid, primary)
    removed = sorted(
        n for n in docs
        if n == CERT_NAME or (not cert_only and n.endswith(".ipa") and n in pushed_names)
    )
    for name in removed:
        device_ops.remove_document(udid, primary, name)
    payload = {"udid": udid, "bundle_id": primary, "removed": removed,
               "kept": len(docs) - len(removed)}
    events.cleaned(**payload)
    return payload


# ---------- the legacy build path ----------


def refuse_legacy_build(app: Any) -> None:
    """Stop a config app from building LiveContainer the zsign way.

    That path signs a wildcard profile's entitlements in literally, which
    disables LiveContainer's keychain redirection: guest apps sign in and are
    not remembered. It was how LiveContainer got onto the devices until
    2026-11-17, so a config.yaml from before then can still carry the entry.
    """
    if app.delivery == "sideload" and app.bundle_id.lower().startswith(lc_instance.LC_ORIGINAL_ID):
        raise ConfigError(
            f"{app.name}: building LiveContainer through the sideload path signs it "
            "with zsign, which breaks its keychain (guest apps cannot stay logged in)",
            hint=(
                "remove this app from config.yaml and use `graft --lc-install "
                "--device <udid>`, which signs LiveContainer with the entitlements "
                "it needs"
            ),
        )


# ---------- the CLI verbs, against a loaded config ----------
#
# `cli.py` keeps one thin handler per verb and calls these, so the shared file
# carries four short functions rather than this module's logic.


def _team(cfg: Any) -> str:
    if not cfg.signing.team_id:
        raise ConfigError("signing.team_id is not set", hint="set it in Settings or config.yaml")
    return cfg.signing.team_id


def _target(cfg: Any, udid: str | None) -> str:
    target = udid or cfg.default_device_udid
    if not target:
        raise ConfigError("no device given and no default device configured",
                          hint="pass --device <udid>")
    return target


def run_status(
    cfg: Any, *, udid: str | None, channel: str | None, events: LcEvents,
    device_ops: DeviceOps | None = None, get_json: Callable[[str], Any] = _get_json,
    connected: Callable[[], list[str]] | None = None,
) -> list[dict]:
    """One `lc-status` per device: `udid`, or every device usbmux can see."""
    from .build_log import BuildLog

    team = _team(cfg)
    channel = check_channel(channel)
    device_ops = device_ops or UsbmuxDevice()
    if udid:
        udids = [udid]
    else:
        from .devices import list_devices_brief

        udids = connected() if connected else [d.udid for d in list_devices_brief()]
    try:
        latest: Release | None = latest_release(channel, get_json=get_json)
    except LiveContainerError as e:
        # Status without "latest" is still worth having; say why it is missing.
        latest = None
        events.warning(f"couldn't look up the latest LiveContainer: {e.message}")
    log = BuildLog(cfg.state_dir / "log.sqlite")
    out = []
    for u in udids:
        try:
            apps = device_ops.installed_apps(u)
        except LiveContainerError as e:
            payload = unreachable_payload(u, e.message, latest)
        else:
            payload = status_payload(udid=u, apps=apps, team=team,
                                     ours=log.lc_installed_bundle_ids(u), latest=latest)
        events.status(**payload)
        out.append(payload)
    return out


def run_install(
    cfg: Any, *, udid: str | None, instances: str | None, channel: str | None,
    force: bool, events: LcEvents,
) -> list[dict]:
    from . import signer
    from .build_log import BuildLog
    from .errors import SigningError

    team, target = _team(cfg), _target(cfg, udid)
    picked, channel = parse_instances(instances), check_channel(channel)
    password = signer.resolve_p12_password(cfg)
    if not password:
        raise SigningError(
            f".p12 password not available (env {cfg.signing.cert_password_env} unset "
            "and not in Keychain)",
            hint="graft --set-p12-password",
        )
    return install(
        udid=target, team=team, instances=picked, channel=channel, force=force,
        p12=cfg.signing.cert, p12_password=password, profiles=AscProfiles(cfg, password),
        device_ops=UsbmuxDevice(), runner=lc_sign.Runner(), transport=usbmux_transport,
        log=BuildLog(cfg.state_dir / "log.sqlite"), events=events,
        fetch_ipa=lambda ch: fetch_ipa(ch, cfg.cache_dir, note=events.note,
                                         warn=events.warning),
    )


def run_push_cert(cfg: Any, *, udid: str | None, events: LcEvents) -> dict:
    return push_cert(udid=_target(cfg, udid), team=_team(cfg), p12=cfg.signing.cert,
                     device_ops=UsbmuxDevice(), events=events)


def pushed_ipa_names(cfg: Any, log: Any, lc_bundle_id: str) -> set[str]:
    """Every `.ipa` name this tool has pushed into `lc_bundle_id`, or would.

    The recorded deliveries, plus `<app>.ipa` for each configured app — the
    name a push uses — so an IPA pushed before deliveries were recorded is
    still recognised as ours.
    """
    return {f"{name}.ipa" for name in cfg.apps} | log.pushed_remote_names(lc_bundle_id)


def run_clean(cfg: Any, *, udid: str | None, events: LcEvents, cert_only: bool = False) -> dict:
    from .build_log import BuildLog

    team = _team(cfg)
    names = pushed_ipa_names(cfg, BuildLog(cfg.state_dir / "log.sqlite"),
                             lc_instance.instance_bundle_id(1, team))
    return clean(udid=_target(cfg, udid), team=team, device_ops=UsbmuxDevice(),
                 pushed_names=names, events=events, cert_only=cert_only)


def human_line(event_type: str, fields: dict[str, Any]) -> str:
    """The terminal's version of one event, for a run without --json."""
    f = fields
    if event_type == "lc-status":
        if not f["reachable"]:
            return f"{f['udid']}: unreachable ({f.get('error')})"
        found = ", ".join(f"{i['bundle_id']} {i['version']}" for i in f["instances"]) or "none"
        latest = f["latest"]
        tail = f"; latest {latest['channel']} {latest['version']}" if latest else ""
        update = " — update available" if f["update_available"] else ""
        return f"{f['udid']}: {found} (managed by {f['managed_by']}){tail}{update}"
    if event_type == "lc-install-progress":
        who = f"LiveContainer {f['instance']}" if f["instance"] else "LiveContainer"
        return f"→ {who}: {f['step']} — {f['detail']}"
    if event_type == "lc-install-waiting":
        return f"… {f['bundle_id']}: {f['message']}"
    if event_type == "lc-installed":
        return f"✓ installed {f['bundle_id']} {f['version']} ({f['channel']})"
    if event_type == "lc-cert-pushed":
        line = f"✓ certificate copied to {f['bundle_id']} {f['path']}"
        return line + (f"\n  {f['note']}" if f.get("note") else "")
    if event_type == "lc-cleaned":
        removed = ", ".join(f["removed"]) or "nothing"
        return f"✓ removed {removed} from {f['bundle_id']} Documents ({f['kept']} kept)"
    return str(f.get("line", ""))
