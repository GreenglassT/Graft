"""`--lc-install`, `--lc-push-cert` and `--lc-clean`, against fakes.

Nothing here reaches a phone, App Store Connect or a keychain. The device is a
`FakeDevice`, and installs go to its `install_ipa`; profiles come from
`FakeProfiles`; `codesign` and `security` go to a `FakeRunner` that records
argv. The signing code under them is the real `lc_sign` / `lc_instance`, on a
synthetic IPA.
"""

from __future__ import annotations

import io
import json
import threading
import time
import zipfile
from pathlib import Path

import pytest

from graft_ipa import device_install, installer, lc_instance, lc_manage
from graft_ipa.apple_portal import AppIdNotRegistered
from graft_ipa.build_log import BuildLog
from graft_ipa.errors import InstallError
from graft_ipa.json_output import JsonEmitter
from graft_ipa.lc_runner import LiveContainerError

from .test_lc_entitlements import EXACT_PROFILE, WILDCARD_PROFILE
from .test_lc_instance import _make_app
from .test_lc_sign import LOGIN, REAL_PASSWORD, SYSTEM, FakeRunner, _make_p12

TEAM = "ABCDE12345"
PRIMARY = f"com.kdt.livecontainer.{TEAM}"
LC2 = f"com.kdt.LiveContainer2.{TEAM}"
LC3 = f"com.kdt.LiveContainer3.{TEAM}"
ALTSTORE = f"com.rileytestut.AltStore.{TEAM}"
RELEASE = lc_manage.Release("nightly", "3.8.14", "https://example/LiveContainer.ipa")


class Recorder:
    """A real `JsonEmitter` into a buffer: what the GUI would read, parsed back.

    Real rather than a fake so every event these tests see has also passed
    the emitter's registry check."""

    def __init__(self) -> None:
        self.buf = io.StringIO()
        #: What went through `LcEvents.warning` (the CLI's `_warn`).
        self.warnings: list[str] = []
        self.events = lc_manage.LcEvents(JsonEmitter(stream=self.buf), warn=self.warnings.append)

    def all(self) -> list[tuple[str, dict]]:
        out = []
        for line in self.buf.getvalue().splitlines():
            event = json.loads(line)
            out.append((event.pop("type"), event))
        return out

    def of(self, event_type: str) -> list[dict]:
        return [f for t, f in self.all() if t == event_type]


class InstallRunner(FakeRunner):
    """`FakeRunner`, minus the real `spawn` it inherits from `lc_sign.Runner`.

    Installs go to `FakeDevice.install_ipa` now, so nothing should spawn; a
    spawn that got through would start a real process, so it fails instead."""

    def spawn(self, cmd):
        self.calls.append(list(cmd))
        raise AssertionError(f"lc_manage spawned a process: {cmd}")


class FakeDevice:
    def __init__(self, apps: dict[str, dict] | None = None,
                 docs: dict[str, list[str]] | None = None) -> None:
        self.apps = apps if apps is not None else {}
        self.docs = docs or {}
        self.pushed: list[tuple[str, str, bytes]] = []
        self.removed: list[tuple[str, str]] = []
        #: The bytes of a file in `docs`, by (bundle id, name); others read
        #: as somebody else's.
        self.contents: dict[tuple[str, str], bytes] = {}
        self.installs: list[tuple[str, str]] = []
        self.install_marks: list[int] = []      # len(calls_source) at each install
        self.calls_source: list = []            # Harness points this at runner.calls
        self.install_gate = None                # a threading.Event to block on
        self.waiting_on_user: list = []         # the switch each install was handed
        self.install_error: Exception | None = None

    def installed_apps(self, udid: str) -> dict[str, dict]:
        return self.apps

    def install_ipa(self, udid: str, ipa: Path, on_progress=None, waiting_on_user=None) -> None:
        self.install_marks.append(len(self.calls_source))
        self.waiting_on_user.append(waiting_on_user)
        if self.install_gate is not None:
            self.install_gate.wait(5)
        if self.install_error is not None:
            raise self.install_error
        self.installs.append((udid, ipa.name))

    def read_document(self, udid: str, bundle_id: str, name: str) -> bytes:
        return self.contents.get((bundle_id, name), b"somebody else's file")

    def list_documents(self, udid: str, bundle_id: str) -> list[str]:
        return list(self.docs.get(bundle_id, []))

    def push_document(self, udid: str, bundle_id: str, name: str, data: bytes) -> None:
        self.pushed.append((bundle_id, name, data))

    def remove_document(self, udid: str, bundle_id: str, name: str) -> None:
        self.removed.append((bundle_id, name))


class FakeProfiles:
    """App IDs registered: everything but ShareExtension, unless told otherwise."""

    def __init__(self, root: Path, missing: set[str] | None = None) -> None:
        self.root = root
        self.missing = missing if missing is not None else {f"{PRIMARY}.ShareExtension"}
        self.decoded: dict[Path, dict] = {}
        self.asked: list[str] = []

    def _file(self, bundle_id: str, ents: dict) -> Path:
        path = self.root / f"{bundle_id}.mobileprovision"
        path.write_bytes(bundle_id.encode())
        self.decoded[path] = {"Name": bundle_id, "TeamIdentifier": [TEAM], "Entitlements": ents}
        return path

    def exact(self, bundle_id: str) -> Path:
        self.asked.append(bundle_id)
        if bundle_id in self.missing:
            raise AppIdNotRegistered(f"App ID {bundle_id} is not registered in this account")
        return self._file(bundle_id, {**EXACT_PROFILE,
                                      "application-identifier": f"{TEAM}.{bundle_id}"})

    def wildcard(self, bundle_id: str) -> Path:
        return self._file("com.kdt.x", WILDCARD_PROFILE)


@pytest.fixture
def ipa(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    _make_app(src)
    out = tmp_path / "LiveContainer.ipa"
    with zipfile.ZipFile(out, "w") as z:
        for f in sorted((src / "Payload").rglob("*")):
            z.write(f, f.relative_to(src))
    return out


class Harness:
    def __init__(self, tmp_path: Path, ipa: Path, *, device: FakeDevice | None = None,
                 runner: InstallRunner | None = None,
                 profiles: FakeProfiles | None = None) -> None:
        (tmp_path / "profiles").mkdir(exist_ok=True)
        self.p12, _ = _make_p12(tmp_path)
        # A phone with one ordinary app: an empty listing is a locked phone,
        # which `install` refuses (see the test for that).
        self.device = device or FakeDevice({"com.example.app": {}})
        self.runner = runner or InstallRunner()
        self.device.calls_source = self.runner.calls
        self.profiles = profiles or FakeProfiles(tmp_path / "profiles")
        self.log = BuildLog(tmp_path / "log.sqlite")
        self.recorder = Recorder()
        self.fetched = 0
        self.ipa = ipa
        self.tmp_path = tmp_path

    def fetch(self, channel: str):
        self.fetched += 1
        return RELEASE, self.ipa

    def install(self, *, instances=(1, 2, 3), force=False, wait_notice_seconds: float = 1.0):
        return lc_manage.install(
            udid="u1", team=TEAM, instances=instances, channel="nightly", force=force,
            p12=self.p12, p12_password=REAL_PASSWORD, profiles=self.profiles,
            device_ops=self.device, runner=self.runner,
            log=self.log, events=self.recorder.events,
            fetch_ipa=self.fetch, load_profile=self.profiles.decoded.__getitem__,
            workdir=self.tmp_path / "work", wait_notice_seconds=wait_notice_seconds,
        )

    def of(self, event_type: str) -> list[dict]:
        return self.recorder.of(event_type)


# ---- AltStore ----


def test_an_altstore_managed_device_is_refused_before_anything_happens(
    tmp_path: Path, ipa: Path
) -> None:
    h = Harness(tmp_path, ipa, device=FakeDevice({PRIMARY: {}, ALTSTORE: {}}))

    with pytest.raises(LiveContainerError) as exc:
        h.install()

    assert exc.value.message == (
        "LiveContainer on u1 is managed by AltStore — update it there"
    )
    assert lc_manage.STABLE_SOURCE_URL in exc.value.hint
    assert lc_manage.NIGHTLY_SOURCE_URL in exc.value.hint
    assert h.fetched == 0
    assert h.runner.calls == []


def test_an_altstore_installed_copy_is_refused_even_with_altstore_not_listed(
    tmp_path: Path, ipa: Path
) -> None:
    h = Harness(tmp_path, ipa, device=FakeDevice({PRIMARY: {"ALTCertificateID": "c"}}))

    with pytest.raises(LiveContainerError):
        h.install()

    assert h.fetched == 0


@pytest.mark.parametrize("apps", [
    {}, {"": {}},
    # The listing takes System apps too now; a device that showed only those
    # would be as blind to AltStore, a User app, as one that showed nothing.
    {"com.apple.example": {"ApplicationType": "System"}},
])
def test_a_device_that_lists_no_apps_is_refused(
    tmp_path: Path, ipa: Path, apps: dict
) -> None:
    """A locked device answers InstallationProxy with nothing — and nothing
    contains no AltStore, so the AltStore check alone would wave it through."""
    h = Harness(tmp_path, ipa, device=FakeDevice(apps))

    with pytest.raises(LiveContainerError) as exc:
        h.install()

    assert exc.value.message == "couldn't list the device's apps — unlock it and try again"
    assert h.fetched == 0
    assert h.runner.calls == []


def test_force_installs_over_altstore(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa, device=FakeDevice({ALTSTORE: {}}))

    h.install(instances=(2,), force=True)

    assert [e["bundle_id"] for e in h.of("lc-installed")] == [LC2]


# ---- the happy path ----


def test_three_instances_are_signed_then_installed_and_recorded(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa)

    results = h.install()

    assert [r["bundle_id"] for r in results] == [PRIMARY, LC2, LC3]
    assert h.device.installs == [("u1", f"{lc_instance.instance_name(n)}.ipa") for n in (1, 2, 3)]
    assert not [c for c in h.runner.calls if c and c[0] == "ideviceinstaller"]
    assert h.log.lc_installed_bundle_ids("u1") == {PRIMARY, LC2, LC3}
    assert h.of("lc-installed")[0] == {
        "udid": "u1", "bundle_id": PRIMARY, "instance": 1, "version": "3.8.14",
        "channel": "nightly",
    }


def test_every_signature_is_made_before_the_first_install(tmp_path: Path, ipa: Path) -> None:
    """An install can block for minutes on a running LiveContainer; the temp
    keychain must not sit on the user's search list for that long."""
    h = Harness(tmp_path, ipa)

    h.install()

    restore = max(
        i for i, c in enumerate(h.runner.calls)
        if c == ["security", "list-keychains", "-d", "user", "-s", LOGIN, SYSTEM]
    )
    assert restore < h.device.install_marks[0]


def test_progress_names_each_step_of_each_instance(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa)

    h.install(instances=(2,))

    assert [(e["instance"], e["step"]) for e in h.of("lc-install-progress")] == [
        (None, "fetch"), (2, "prepare"), (2, "profile"), (2, "sign"), (2, "install"),
    ]


def test_the_signing_summary_is_reported(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa)

    h.install(instances=(2,))

    lines = [e["line"] for e in h.of("log")]
    assert any(line.startswith(f"LiveContainer.app: {TEAM}.{LC2} ") for line in lines)


# ---- waiting on a running LiveContainer ----


def test_an_install_still_blocked_says_to_close_livecontainer_once(
    tmp_path: Path, ipa: Path
) -> None:
    # An update: only a LiveContainer already on the device can hold it.
    h = Harness(tmp_path, ipa, device=FakeDevice({"com.example.app": {}, PRIMARY: {}}))
    h.device.install_gate = threading.Event()
    threading.Timer(0.3, h.device.install_gate.set).start()

    h.install(instances=(1,), wait_notice_seconds=0.05)

    assert h.of("lc-install-waiting") == [{
        "udid": "u1", "instance": 1, "bundle_id": PRIMARY,
        "message": "close LiveContainer on the device to finish",
    }]
    assert [e["bundle_id"] for e in h.of("lc-installed")] == [PRIMARY]
    # cli-1: from the notice on, installd's silence is the user's time.
    (switch,) = h.device.waiting_on_user
    assert switch is not None and switch.is_set()


def test_an_install_that_finishes_promptly_says_nothing_about_waiting(
    tmp_path: Path, ipa: Path
) -> None:
    h = Harness(tmp_path, ipa)

    h.install(instances=(1,))

    assert h.of("lc-install-waiting") == []
    (switch,) = h.device.waiting_on_user
    assert switch is not None and not switch.is_set(), "60 s of silence is still a phone gone"


class _SlowDevice(FakeDevice):
    """Every install runs past the notice, then finishes."""

    def install_ipa(self, udid: str, ipa: Path, on_progress=None, waiting_on_user=None) -> None:
        time.sleep(0.3)
        super().install_ipa(udid, ipa, on_progress, waiting_on_user)


@pytest.mark.parametrize("listed", [LC2, LC2.lower()], ids=["as-installed", "other-case"])
def test_only_an_instance_already_on_the_device_waits_on_the_user(
    tmp_path: Path, ipa: Path, listed: str
) -> None:
    """d14-cli-1: iOS can hold an install only over an app that is there.
    Instance 2 is an update and gets the notice; instance 1 is new, and its
    slow install is just slow. The listing is matched without regard to
    case, as discovery matches it."""
    h = Harness(tmp_path, ipa, device=_SlowDevice({"com.example.app": {}, listed: {}}))

    h.install(instances=(1, 2), wait_notice_seconds=0.05)

    assert [e["bundle_id"] for e in h.of("lc-install-waiting")] == [LC2]
    assert [s.is_set() for s in h.device.waiting_on_user] == [False, True]
    assert [e["bundle_id"] for e in h.of("lc-installed")] == [PRIMARY, LC2]


# ---- d14-cli-1: a silent phone, through device_install's real limits ----
#
# `install_ipa` is the real one, down to pymobiledevice3's
# InstallationProxyService; only the lockdown, AFC and upload are left out.
# installd reports one stage, then says nothing with the connection open: a
# phone gone to sleep, or iOS holding the install. Timers scaled.

SILENCE = 0.5
PATIENT = 2.0


class _InstalldDevice(FakeDevice):
    """A `FakeDevice` whose installs go through `UsbmuxDevice.install_ipa`."""

    took: float | None = None

    def install_ipa(self, udid: str, ipa: Path, on_progress=None, waiting_on_user=None) -> None:
        self.waiting_on_user.append(waiting_on_user)
        started = time.monotonic()
        try:
            lc_manage.UsbmuxDevice().install_ipa(udid, ipa, on_progress, waiting_on_user=waiting_on_user)
        finally:
            self.took = time.monotonic() - started


@pytest.fixture
def goes_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    from .test_device_install_liveness import Installd, _proxy

    monkeypatch.setattr(device_install, "SILENCE_SECONDS", SILENCE)
    monkeypatch.setattr(device_install, "PATIENT_SECONDS", PATIENT)

    async def install(ipa, udid, on_progress, on_trust_prompt, waiting_on_user=None):
        installd = Installd([(0.05, {"Status": "InstallingApplication", "PercentComplete": 90})])
        with device_install._quiet_progress_bars():
            await device_install._install_until_silent(
                _proxy(installd), "/PublicStaging/x.ipa", lambda *_: None,
                waiting_on_user=waiting_on_user)

    monkeypatch.setattr(device_install, "_install", install)


def test_a_fresh_install_that_goes_silent_gets_the_silence_limit_and_the_connection_hint(
    tmp_path: Path, ipa: Path, goes_silent
) -> None:
    """The finding's iPad: no LiveContainer on it, so nothing for iOS to
    hold, and a notice timer that has long run out. Silence is a phone gone."""
    h = Harness(tmp_path, ipa, device=_InstalldDevice({"com.example.app": {}}))

    with pytest.raises(InstallError) as e:
        h.install(instances=(1,), wait_notice_seconds=0.05)

    assert h.of("lc-install-waiting") == []
    assert SILENCE <= h.device.took < PATIENT, h.device.took
    assert e.value.hint == installer.connection_hint("PhoneWentSilentError")
    assert "LiveContainer" not in e.value.hint


def test_an_update_that_goes_silent_waits_patiently_and_says_to_close_livecontainer(
    tmp_path: Path, ipa: Path, goes_silent
) -> None:
    h = Harness(tmp_path, ipa, device=_InstalldDevice({"com.example.app": {}, PRIMARY: {}}))

    with pytest.raises(InstallError) as e:
        h.install(instances=(1,), wait_notice_seconds=0.05)

    assert [w["message"] for w in h.of("lc-install-waiting")] == [lc_manage.WAITING_MESSAGE]
    assert h.device.took >= PATIENT, h.device.took
    assert e.value.hint == lc_manage._install_hint("StillWaitingError")
    assert "close LiveContainer" in e.value.hint


# ---- failures ----


def test_a_mismatched_application_identifier_says_to_delete_the_old_copy(
    tmp_path: Path, ipa: Path
) -> None:
    h = Harness(tmp_path, ipa)
    h.device.install_error = device_install.DeviceInstallError(
        "MismatchedApplicationIdentifierEntitlement: upgrade refused")

    with pytest.raises(InstallError) as exc:
        h.install(instances=(1,))

    assert exc.value.hint == (
        "the LiveContainer on the device was signed with a wildcard application "
        "identifier (an old zsign-signed copy), and iOS refuses to upgrade it in "
        "place. Delete it on the device, then install again."
    )
    assert h.log.lc_installed_bundle_ids("u1") == set()


def test_a_main_app_without_its_app_id_stops_and_explains(tmp_path: Path, ipa: Path) -> None:
    profiles = FakeProfiles(tmp_path / "profiles", missing={LC2})
    (tmp_path / "profiles").mkdir()
    h = Harness(tmp_path, ipa, profiles=profiles)

    with pytest.raises(LiveContainerError) as exc:
        h.install(instances=(2,))

    assert exc.value.message == f"App ID {LC2} is not registered in this account"
    assert "App Group" in exc.value.hint
    assert h.device.installs == []
    # Stopped before the keychain was ever made.
    assert not any(c[:2] == ["security", "create-keychain"] for c in h.runner.calls)


@pytest.mark.parametrize("ext", ["LiveProcess", "LaunchAppExtension"])
def test_an_extension_that_needs_its_app_group_stops_without_its_app_id(
    tmp_path: Path, ipa: Path, ext: str
) -> None:
    """LiveProcess and LaunchAppExtension need the shared app group; the
    wildcard profile has none. Falling back would install a LiveContainer whose
    extensions cannot reach the group — the spec says stop and explain."""
    (tmp_path / "profiles").mkdir()
    missing = {f"{PRIMARY}.ShareExtension", f"{PRIMARY}.{ext}"}
    h = Harness(tmp_path, ipa, profiles=FakeProfiles(tmp_path / "profiles", missing=missing))

    with pytest.raises(LiveContainerError) as exc:
        h.install(instances=(1,))

    assert exc.value.message == f"App ID {PRIMARY}.{ext} is not registered in this account"
    assert "App Group" in exc.value.hint
    assert not any(c[:2] == ["security", "create-keychain"] for c in h.runner.calls)
    assert h.device.installs == []


def test_an_extension_without_its_app_id_rides_the_wildcard(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa)

    h.install(instances=(1,))

    notes = [e["line"] for e in h.of("log")]
    assert (
        f"{PRIMARY}.ShareExtension has no App ID of its own; signing it on the "
        "wildcard profile, without app groups"
    ) in notes


# ---- push-cert / clean ----


def test_the_certificate_goes_into_the_primary_documents_and_is_recorded(
    tmp_path: Path,
) -> None:
    p12, _ = _make_p12(tmp_path)
    device = FakeDevice({PRIMARY: {}})
    rec = Recorder()
    log = BuildLog(tmp_path / "log.sqlite")

    lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=device, events=rec.events,
                        log=log)

    assert device.pushed == [(PRIMARY, "AppleDevelopment.p12", p12.read_bytes())]
    assert rec.all() == [("lc-cert-pushed", {
        "udid": "u1", "bundle_id": PRIMARY, "path": "Documents/AppleDevelopment.p12",
        "note": lc_manage.CERT_STAYS_NOTE,
    })]
    # What `--lc-clean` later deletes on, and nothing else does.
    assert log.cert_pushes("u1") == {PRIMARY: {"AppleDevelopment.p12"}}
    assert log.cert_pushes("another-phone") == {}


def test_the_certificate_needs_the_primary_installed(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)

    with pytest.raises(LiveContainerError) as exc:
        lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=FakeDevice({LC2: {}}),
                            events=Recorder().events, log=BuildLog(tmp_path / "log.sqlite"))

    assert exc.value.message == f"{PRIMARY} is not installed on u1"


def test_a_certificate_graft_did_not_push_is_not_overwritten(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["AppleDevelopment.p12"]})
    log = BuildLog(tmp_path / "log.sqlite")

    with pytest.raises(LiveContainerError) as exc:
        lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=device,
                            events=Recorder().events, log=log)

    assert "Graft did not push" in exc.value.message
    assert "rename" in exc.value.hint
    assert device.pushed == []
    assert log.cert_pushes("u1") == {}


def test_a_certificate_graft_pushed_is_pushed_over_again(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["AppleDevelopment.p12"]})
    log = BuildLog(tmp_path / "log.sqlite")
    log.record_cert_push(udid="u1", bundle_id=PRIMARY, name="AppleDevelopment.p12")

    lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=device,
                        events=Recorder().events, log=log)

    assert device.pushed == [(PRIMARY, "AppleDevelopment.p12", p12.read_bytes())]


def test_clean_removes_only_what_we_put_there() -> None:
    docs = [
        "AppleDevelopment.p12", "example.ipa", "bravo.ipa", "someone-elses.ipa",
        "iPad.mobiledevicepairing", "notes.txt", "profile.mobileprovision",
    ]
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: docs})
    rec = Recorder()

    lc_manage.clean(udid="u1", device_ops=device, events=rec.events,
                    pushed={PRIMARY: {"example.ipa", "bravo.ipa", "delta.ipa"}},
                    certs={PRIMARY: {"AppleDevelopment.p12"}})

    assert device.removed == [
        (PRIMARY, "AppleDevelopment.p12"), (PRIMARY, "bravo.ipa"), (PRIMARY, "example.ipa"),
    ]
    assert rec.all() == [("lc-cleaned", {
        "udid": "u1", "bundle_id": PRIMARY,
        "removed": ["AppleDevelopment.p12", "bravo.ipa", "example.ipa"], "kept": 4,
    })]


def test_cert_only_removes_the_certificate_and_keeps_the_ipas() -> None:
    """Guest apps in LiveContainer's sandbox can read Documents/, so the .p12
    should go as soon as it is imported — while the IPAs the next step
    imports must stay."""
    docs = ["AppleDevelopment.p12", "example.ipa", "notes.txt"]
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: docs})
    rec = Recorder()

    lc_manage.clean(udid="u1", device_ops=device, events=rec.events,
                    pushed={PRIMARY: {"example.ipa"}},
                    certs={PRIMARY: {"AppleDevelopment.p12"}}, cert_only=True)

    assert device.removed == [(PRIMARY, "AppleDevelopment.p12")]
    assert rec.of("lc-cleaned")[0]["removed"] == ["AppleDevelopment.p12"]
    assert rec.of("lc-cleaned")[0]["kept"] == 2


def test_the_pushed_certificate_says_it_stays_until_removed(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)
    rec = Recorder()

    lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=FakeDevice({PRIMARY: {}}),
                        events=rec.events, log=BuildLog(tmp_path / "log.sqlite"))

    note = rec.of("lc-cert-pushed")[0]["note"]
    assert "stays" in note and "until" in note and "--lc-clean --cert-only" in note
    line = lc_manage.human_line("lc-cert-pushed", rec.of("lc-cert-pushed")[0])
    assert note in line


def test_clean_with_nothing_to_remove_says_so() -> None:
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["iPad.mobiledevicepairing"]})
    rec = Recorder()

    lc_manage.clean(udid="u1", device_ops=device, events=rec.events,
                    pushed={PRIMARY: {"example.ipa"}})

    assert device.removed == []
    assert rec.of("lc-cleaned")[0]["removed"] == []


class _UnlistableDevice(FakeDevice):
    """Another LiveContainer whose sandbox HouseArrest won't vend (a
    TrollStore install, or one signed for distribution)."""

    def __init__(self, *a, unlistable: set[str], **k) -> None:
        super().__init__(*a, **k)
        self.unlistable = unlistable

    def list_documents(self, udid: str, bundle_id: str) -> list[str]:
        if bundle_id in self.unlistable:
            raise lc_manage.LiveContainerError(f"could not talk to {udid}: InstallationLookupFailed")
        return super().list_documents(udid, bundle_id)


@pytest.mark.parametrize("other", ["com.kdt.livecontainer.AAAAAAAAAA", "com.kdt.livecontainer",
                                   "com.kdt.LiveContainer2.ZZZZZZZZZZ", "com.kdt.livecontainer.ZZZZZZZZZZ"])
def test_an_unrecorded_livecontainer_that_cant_be_listed_is_skipped_with_a_note(other: str) -> None:
    """Combined fix check #4: the full clean visits every LiveContainer for
    partials, and one it can't open ended the clean, before (or after)
    the recorded container was cleaned, and its records were never forgotten."""
    device = _UnlistableDevice({PRIMARY: {}, other: {}}, docs={PRIMARY: ["example.ipa"]},
                               unlistable={other})
    rec = Recorder()

    payloads = lc_manage.clean(udid="u1", device_ops=device, events=rec.events,
                               pushed={PRIMARY: {"example.ipa"}})

    assert device.removed == [(PRIMARY, "example.ipa")]
    assert [p["bundle_id"] for p in payloads] == [PRIMARY]
    notes = [f["line"] for f in rec.of("log") if f["source"] == "graft"]
    assert notes == [f"{other}: skipped, its Documents couldn't be listed "
                     "(could not talk to u1: InstallationLookupFailed)"], rec.all()


def test_a_recorded_livecontainer_that_cant_be_listed_still_fails_the_clean() -> None:
    """Only one Graft never used is skipped: a failure on the container the
    records are about is the clean's failure, as it always was."""
    device = _UnlistableDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa"]}, unlistable={PRIMARY})
    with pytest.raises(lc_manage.LiveContainerError):
        lc_manage.clean(udid="u1", device_ops=device, events=Recorder().events,
                        pushed={PRIMARY: {"example.ipa"}})


# ---- the legacy build path ----


@pytest.mark.parametrize("bundle_id", ["com.kdt.livecontainer", "com.kdt.LiveContainer2.T"])
def test_a_sideloaded_livecontainer_build_is_refused(bundle_id: str) -> None:
    from types import SimpleNamespace

    from graft_ipa.errors import ConfigError

    app = SimpleNamespace(name="livecontainer", bundle_id=bundle_id, delivery="sideload")
    with pytest.raises(ConfigError) as exc:
        lc_manage.refuse_legacy_build(app)

    assert exc.value.message == (
        "livecontainer: building LiveContainer through the sideload path signs it "
        "with zsign, which breaks its keychain (guest apps cannot stay logged in)"
    )
    assert "--lc-install" in exc.value.hint


@pytest.mark.parametrize(
    ("bundle_id", "delivery"),
    [
        ("com.example.app", "livecontainer"),
        ("com.example.app", "sideload"),
        # Only the sideload path signs with zsign; the guard is about that.
        ("com.kdt.livecontainer", "livecontainer"),
    ],
)
def test_other_builds_are_not_refused(bundle_id: str, delivery: str) -> None:
    from types import SimpleNamespace

    lc_manage.refuse_legacy_build(SimpleNamespace(name="x", bundle_id=bundle_id, delivery=delivery))


def test_instances_parse() -> None:
    from graft_ipa.errors import ConfigError

    assert lc_manage.parse_instances(None) == (1, 2, 3)
    assert lc_manage.parse_instances("3,1") == (1, 3)
    for bad in ("4", "1,x", "", "0"):
        with pytest.raises(ConfigError):
            lc_manage.parse_instances(bad)



# ---- cli-1: the longer wait ends in words about LiveContainer, and a Stop ends it ----


def test_a_wait_past_the_longer_bound_says_to_close_livecontainer(tmp_path: Path, ipa: Path) -> None:
    from graft_ipa import device_install

    h = Harness(tmp_path, ipa)
    h.device.install_error = device_install.DeviceInstallError(
        "StillWaitingError: the install was still waiting for the app it replaces to be closed on the "
        "device, 15 minutes on, so Graft stopped waiting"
    )
    with pytest.raises(InstallError) as e:
        h.install(instances=(1,))
    assert "close LiveContainer" in (e.value.hint or "")
    assert "unlock" not in (e.value.hint or ""), "not the locked-phone hint"


@pytest.mark.parametrize("updating", [False, True], ids=["fresh", "update"])
def test_a_stop_ends_an_install_waiting_on_the_user(tmp_path: Path, ipa: Path, updating: bool) -> None:
    """The main thread waits in `done.wait()`, which a Stop interrupts,
    whether or not the install could be waiting on the user."""
    import os
    import signal

    from graft_ipa.errors import Stopped

    device = FakeDevice()
    gate = threading.Event()
    device.install_gate = gate
    events = lc_manage.LcEvents(emitter=None, say=lambda _: None)
    with lc_manage.exit_on_termination():
        threading.Timer(0.3, os.kill, (os.getpid(), signal.SIGTERM)).start()
        started = time.monotonic()
        with pytest.raises(Stopped):
            lc_manage._install_one(device, udid="u1", ipa=ipa, instance=1, bundle_id=PRIMARY,
                                   updating=updating, events=events, wait_notice_seconds=0.05)
        assert time.monotonic() - started < 2
    gate.set()


def test_the_real_device_hands_the_switch_to_the_install(tmp_path: Path, monkeypatch) -> None:
    seen: list = []
    monkeypatch.setattr(device_install, "install_ipa", lambda ipa, **kw: seen.append(kw))
    switch = threading.Event()
    lc_manage.UsbmuxDevice().install_ipa("u1", tmp_path / "x.ipa", waiting_on_user=switch)
    assert seen == [{"udid": "u1", "on_progress": None, "waiting_on_user": switch}]


class _UnremovableDevice(FakeDevice):
    """A LiveContainer whose Documents list, but won't let a file go."""

    def __init__(self, *a, unremovable: set[str], **k) -> None:
        super().__init__(*a, **k)
        self.unremovable = unremovable

    def remove_document(self, udid: str, bundle_id: str, name: str) -> None:
        if bundle_id in self.unremovable:
            raise lc_manage.LiveContainerError(f"could not talk to {udid}: AfcException")
        super().remove_document(udid, bundle_id, name)


def _abandoned_partial() -> str:
    """A partial a push from this Mac left, by a process that is gone."""
    import subprocess
    import sys

    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    return f".example.ipa.{device_install.this_mac()}-{gone.pid}.graft-partial"


def test_a_partial_that_cant_be_removed_from_an_unrecorded_livecontainer_is_skipped_with_a_note() -> None:
    """cli-8: the sweep of a container Graft never pushed to raised out of
    `_remove_partials` and ended the clean, as an unlistable one once did."""
    other = "com.kdt.livecontainer.AAAAAAAAAA"
    partial = _abandoned_partial()
    assert lc_manage.is_abandoned_partial(partial), "premise"
    device = _UnremovableDevice({PRIMARY: {}, other: {}}, docs={PRIMARY: ["example.ipa"], other: [partial]},
                                unremovable={other})
    rec = Recorder()

    payloads = lc_manage.clean(udid="u1", device_ops=device, events=rec.events,
                               pushed={PRIMARY: {"example.ipa"}})

    assert device.removed == [(PRIMARY, "example.ipa")]
    assert [p["bundle_id"] for p in payloads] == [PRIMARY]
    notes = [f["line"] for f in rec.of("log") if f["source"] == "graft"]
    assert notes == [f"{other}: skipped, what an unfinished push left in its Documents couldn't be "
                     "removed (could not talk to u1: AfcException)"], rec.all()


def test_a_partial_that_cant_be_removed_from_a_recorded_livecontainer_still_fails_the_clean() -> None:
    device = _UnremovableDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa", _abandoned_partial()]},
                                unremovable={PRIMARY})
    with pytest.raises(lc_manage.LiveContainerError):
        lc_manage.clean(udid="u1", device_ops=device, events=Recorder().events,
                        pushed={PRIMARY: {"example.ipa"}})
