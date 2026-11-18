"""`--lc-install`, `--lc-push-cert` and `--lc-clean`, against fakes.

Nothing here reaches a phone, App Store Connect or a keychain. The device is a
`FakeDevice`; profiles come from `FakeProfiles`; `codesign`, `security` and
`ideviceinstaller` go to a `FakeRunner` that records argv. The signing code
under them is the real `lc_sign` / `lc_instance`, on a synthetic IPA.
"""

from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from graft_ipa import lc_manage
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
        self.events = lc_manage.LcEvents(JsonEmitter(stream=self.buf))

    def all(self) -> list[tuple[str, dict]]:
        out = []
        for line in self.buf.getvalue().splitlines():
            event = json.loads(line)
            out.append((event.pop("type"), event))
        return out

    def of(self, event_type: str) -> list[dict]:
        return [f for t, f in self.all() if t == event_type]


class FakeProc:
    """`ideviceinstaller` as `Popen` would hand it back."""

    def __init__(self, output: str = "Install: Complete\n", returncode: int = 0,
                 timeouts: int = 0) -> None:
        self.output, self.returncode, self.timeouts = output, returncode, timeouts

    def communicate(self, timeout: float | None = None):
        if self.timeouts and timeout is not None:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("ideviceinstaller", timeout)
        return self.output, None


class InstallRunner(FakeRunner):
    def __init__(self, procs: list[FakeProc] | None = None, **kw) -> None:
        super().__init__(**kw)
        self.procs = procs or []
        self.spawned: list[list[str]] = []

    def spawn(self, cmd):
        self.calls.append(list(cmd))
        self.spawned.append(list(cmd))
        return self.procs.pop(0) if self.procs else FakeProc()


class FakeDevice:
    def __init__(self, apps: dict[str, dict] | None = None,
                 docs: dict[str, list[str]] | None = None) -> None:
        self.apps = apps if apps is not None else {}
        self.docs = docs or {}
        self.pushed: list[tuple[str, str, bytes]] = []
        self.removed: list[tuple[str, str]] = []

    def installed_apps(self, udid: str) -> dict[str, dict]:
        return self.apps

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
                 runner: InstallRunner | None = None, profiles: FakeProfiles | None = None,
                 transport: str = "Network") -> None:
        (tmp_path / "profiles").mkdir(exist_ok=True)
        self.p12, _ = _make_p12(tmp_path)
        # A phone with one ordinary app: an empty listing is a locked phone,
        # which `install` refuses (see the test for that).
        self.device = device or FakeDevice({"com.example.app": {}})
        self.runner = runner or InstallRunner()
        self.profiles = profiles or FakeProfiles(tmp_path / "profiles")
        self.log = BuildLog(tmp_path / "log.sqlite")
        self.recorder = Recorder()
        self.fetched = 0
        self.ipa = ipa
        self.transport = transport
        self.tmp_path = tmp_path

    def fetch(self, channel: str):
        self.fetched += 1
        return RELEASE, self.ipa

    def install(self, *, instances=(1, 2, 3), force=False):
        return lc_manage.install(
            udid="u1", team=TEAM, instances=instances, channel="nightly", force=force,
            p12=self.p12, p12_password=REAL_PASSWORD, profiles=self.profiles,
            device_ops=self.device, runner=self.runner,
            transport=lambda udid: self.transport, log=self.log, events=self.recorder.events,
            fetch_ipa=self.fetch, load_profile=self.profiles.decoded.__getitem__,
            workdir=self.tmp_path / "work",
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


@pytest.mark.parametrize("apps", [{}, {"": {}}])
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
    assert [s[:5] for s in h.runner.spawned] == [
        ["ideviceinstaller", "-u", "u1", "--network", "install"]
    ] * 3
    assert h.log.lc_installed_bundle_ids("u1") == {PRIMARY, LC2, LC3}
    assert h.of("lc-installed")[0] == {
        "udid": "u1", "bundle_id": PRIMARY, "instance": 1, "version": "3.8.14",
        "channel": "nightly",
    }


def test_a_usb_device_is_installed_without_network(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa, transport="USB")

    h.install(instances=(3,))

    assert h.runner.spawned[0][:4] == ["ideviceinstaller", "-u", "u1", "install"]


def test_every_signature_is_made_before_the_first_install(tmp_path: Path, ipa: Path) -> None:
    """An install can block for minutes on a running LiveContainer; the temp
    keychain must not sit on the user's search list for that long."""
    h = Harness(tmp_path, ipa)

    h.install()

    first_spawn = h.runner.calls.index(h.runner.spawned[0])
    restore = max(
        i for i, c in enumerate(h.runner.calls)
        if c == ["security", "list-keychains", "-d", "user", "-s", LOGIN, SYSTEM]
    )
    assert restore < first_spawn


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
    h = Harness(tmp_path, ipa, runner=InstallRunner([FakeProc(timeouts=1)]))

    h.install(instances=(1,))

    assert h.of("lc-install-waiting") == [{
        "udid": "u1", "instance": 1, "bundle_id": PRIMARY,
        "message": "close LiveContainer on the device to finish",
    }]
    assert [e["bundle_id"] for e in h.of("lc-installed")] == [PRIMARY]


def test_an_install_that_finishes_promptly_says_nothing_about_waiting(
    tmp_path: Path, ipa: Path
) -> None:
    h = Harness(tmp_path, ipa)

    h.install(instances=(1,))

    assert h.of("lc-install-waiting") == []


# ---- failures ----


def test_a_mismatched_application_identifier_says_to_delete_the_old_copy(
    tmp_path: Path, ipa: Path
) -> None:
    out = "Install: Error occurred: MismatchedApplicationIdentifierEntitlement\n"
    h = Harness(tmp_path, ipa, runner=InstallRunner([FakeProc(out, returncode=1)]))

    with pytest.raises(InstallError) as exc:
        h.install(instances=(1,))

    assert exc.value.hint == (
        "the LiveContainer on the device was signed with a wildcard application "
        "identifier (an old zsign-signed copy), and iOS refuses to upgrade it in "
        "place. Delete it on the device, then install again."
    )
    assert h.log.lc_installed_bundle_ids("u1") == set()


def test_an_output_saying_complete_with_a_failing_exit_is_a_failure(
    tmp_path: Path, ipa: Path
) -> None:
    h = Harness(tmp_path, ipa, runner=InstallRunner([FakeProc("Install: Complete", 1)]))

    with pytest.raises(InstallError):
        h.install(instances=(1,))


def test_a_main_app_without_its_app_id_stops_and_explains(tmp_path: Path, ipa: Path) -> None:
    profiles = FakeProfiles(tmp_path / "profiles", missing={LC2})
    (tmp_path / "profiles").mkdir()
    h = Harness(tmp_path, ipa, profiles=profiles)

    with pytest.raises(LiveContainerError) as exc:
        h.install(instances=(2,))

    assert exc.value.message == f"App ID {LC2} is not registered in this account"
    assert "App Group" in exc.value.hint
    assert h.runner.spawned == []
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
    assert h.runner.spawned == []


def test_an_extension_without_its_app_id_rides_the_wildcard(tmp_path: Path, ipa: Path) -> None:
    h = Harness(tmp_path, ipa)

    h.install(instances=(1,))

    notes = [e["line"] for e in h.of("log")]
    assert (
        f"{PRIMARY}.ShareExtension has no App ID of its own; signing it on the "
        "wildcard profile, without app groups"
    ) in notes


# ---- push-cert / clean ----


def test_the_certificate_goes_into_the_primary_documents(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)
    device = FakeDevice({PRIMARY: {}})
    rec = Recorder()

    lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=device, events=rec.events)

    assert device.pushed == [(PRIMARY, "AppleDevelopment.p12", p12.read_bytes())]
    assert rec.all() == [("lc-cert-pushed", {
        "udid": "u1", "bundle_id": PRIMARY, "path": "Documents/AppleDevelopment.p12",
        "note": lc_manage.CERT_STAYS_NOTE,
    })]


def test_the_certificate_needs_the_primary_installed(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)

    with pytest.raises(LiveContainerError) as exc:
        lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=FakeDevice({LC2: {}}),
                            events=Recorder().events)

    assert exc.value.message == f"{PRIMARY} is not installed on u1"


def test_clean_removes_only_what_we_put_there() -> None:
    docs = [
        "AppleDevelopment.p12", "example.ipa", "bravo.ipa", "someone-elses.ipa",
        "iPad.mobiledevicepairing", "notes.txt", "profile.mobileprovision",
    ]
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: docs})
    rec = Recorder()

    lc_manage.clean(udid="u1", team=TEAM, device_ops=device,
                    pushed_names={"example.ipa", "bravo.ipa", "delta.ipa"}, events=rec.events)

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

    lc_manage.clean(udid="u1", team=TEAM, device_ops=device, pushed_names={"example.ipa"},
                    events=rec.events, cert_only=True)

    assert device.removed == [(PRIMARY, "AppleDevelopment.p12")]
    assert rec.of("lc-cleaned")[0]["removed"] == ["AppleDevelopment.p12"]
    assert rec.of("lc-cleaned")[0]["kept"] == 2


def test_the_pushed_certificate_says_it_stays_until_removed(tmp_path: Path) -> None:
    p12, _ = _make_p12(tmp_path)
    rec = Recorder()

    lc_manage.push_cert(udid="u1", team=TEAM, p12=p12, device_ops=FakeDevice({PRIMARY: {}}),
                        events=rec.events)

    note = rec.of("lc-cert-pushed")[0]["note"]
    assert "stays" in note and "until" in note and "--lc-clean --cert-only" in note
    line = lc_manage.human_line("lc-cert-pushed", rec.of("lc-cert-pushed")[0])
    assert note in line


def test_clean_with_nothing_to_remove_says_so() -> None:
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["iPad.mobiledevicepairing"]})
    rec = Recorder()

    lc_manage.clean(udid="u1", team=TEAM, device_ops=device, pushed_names=set(),
                    events=rec.events)

    assert device.removed == []
    assert rec.of("lc-cleaned")[0]["removed"] == []


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
