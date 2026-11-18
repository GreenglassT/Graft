"""`--lc-clean` removes what Graft recorded pushing, and nothing else.

A push goes into the instance an app's `lc_bundle_id` pins (or the default
one) and records which, per device, in the deliveries table; `--lc-push-cert`
records where it put the certificate. Those records are the only evidence
`--lc-clean` deletes on. It used to add `<app>.ipa` for every configured app,
`<app>.ipa` in every pinned container and `AppleDevelopment.p12` in the
primary, whether or not Graft ever put them there. On a free Apple ID the
primary is SideStore's LiveContainer, where people keep their own IPAs, and a
config with no deliveries at all lost the user's files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from graft_ipa import lc_manage
from graft_ipa.build_log import BuildLog
from graft_ipa.config import load_config

from .test_lc_install import LC2, LC3, PRIMARY, TEAM, FakeDevice, Recorder

CERT = "AppleDevelopment.p12"
OTHER_PRIMARY = "com.kdt.livecontainer.FGHIJ67890"


# ---- `clean`, given the records ----


def test_only_recorded_names_go_from_each_instance() -> None:
    device = FakeDevice(
        {PRIMARY: {}, LC2: {}},
        docs={
            PRIMARY: [CERT, "example.ipa", "bravo.ipa"],
            LC2: [CERT, "bravo.ipa", "someone-elses.ipa", "notes.txt"],
        },
    )
    rec = Recorder()

    lc_manage.clean(
        udid="u1", device_ops=device, events=rec.events,
        pushed={PRIMARY: {"example.ipa"}, LC2: {"bravo.ipa"}},
    )

    assert device.removed == [(LC2, "bravo.ipa"), (PRIMARY, "example.ipa")]
    events = rec.of("lc-cleaned")
    assert [e["bundle_id"] for e in events] == [LC2, PRIMARY]
    assert events[0] == {"udid": "u1", "bundle_id": LC2, "removed": ["bravo.ipa"], "kept": 3}


def test_a_recorded_certificate_goes_and_an_unrecorded_one_is_reported() -> None:
    device = FakeDevice(
        {PRIMARY: {}, LC2: {}},
        docs={PRIMARY: [CERT, "example.ipa"], LC2: [CERT, "bravo.ipa"]},
    )
    rec = Recorder()

    lc_manage.clean(
        udid="u1", device_ops=device, events=rec.events,
        pushed={LC2: {"bravo.ipa"}}, certs={PRIMARY: {CERT}},
    )

    assert device.removed == [(LC2, "bravo.ipa"), (PRIMARY, CERT)]
    [warning] = rec.warnings
    assert warning.startswith(f"{LC2}: left {CERT} in Documents")
    assert "no record" in warning


def test_cert_only_removes_recorded_certificates_and_no_ipa() -> None:
    """The step right after Import Certificate: the IPAs still wait to be imported."""
    device = FakeDevice(
        {PRIMARY: {}, LC2: {}}, docs={PRIMARY: [CERT, "example.ipa"], LC2: ["bravo.ipa"]}
    )
    rec = Recorder()

    lc_manage.clean(
        udid="u1", device_ops=device, events=rec.events, cert_only=True,
        pushed={PRIMARY: {"example.ipa"}, LC2: {"bravo.ipa"}}, certs={PRIMARY: {CERT}},
    )

    assert device.removed == [(PRIMARY, CERT)]
    assert [e["bundle_id"] for e in rec.of("lc-cleaned")] == [PRIMARY]


def test_cert_only_with_no_recorded_certificate_removes_nothing_and_says_why() -> None:
    """A certificate `--lc-push-cert` put there before pushes were recorded: it
    is reported where it was put (the team's primary), not deleted."""
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: [CERT, "example.ipa"]})
    rec = Recorder()

    lc_manage.clean(
        udid="u1", device_ops=device, events=rec.events, cert_only=True,
        pushed={PRIMARY: {"example.ipa"}}, look_for_cert=[PRIMARY],
    )

    assert device.removed == []
    assert rec.of("lc-cleaned") == []
    assert len(rec.warnings) == 1 and CERT in rec.warnings[0]


def test_an_instance_that_is_no_longer_installed_is_skipped() -> None:
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa"], LC2: ["bravo.ipa"]})
    rec = Recorder()

    lc_manage.clean(
        udid="u1", device_ops=device, events=rec.events,
        pushed={PRIMARY: {"example.ipa"}, LC2: {"bravo.ipa"}},
    )

    assert device.removed == [(PRIMARY, "example.ipa")]
    assert [e["bundle_id"] for e in rec.of("lc-cleaned")] == [PRIMARY]


def test_nothing_recorded_removes_nothing_and_says_so() -> None:
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa", CERT]})
    rec = Recorder()

    assert lc_manage.clean(udid="u1", device_ops=device, events=rec.events, pushed={}) == []

    assert device.removed == []
    assert rec.of("lc-cleaned") == []
    assert any("nothing removed" in e["line"] for e in rec.of("log"))


# ---- `run_clean`, against the build log ----


def _cfg(tmp_path: Path, apps: dict, *, team: str | None = None):
    raw: dict = {"state_dir": str(tmp_path / "state"), "default_device_udid": "u1",
                 "apps": apps}
    if team:
        raw["signing"] = {"cert": str(tmp_path / "c.p12"), "cert_password_env": "X",
                          "profile": str(tmp_path / "p.mobileprovision"), "team_id": team}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return load_config(path)


def _log(cfg) -> BuildLog:
    return BuildLog(cfg.state_dir / "log.sqlite")


def _record(cfg, app: str, lc: str, remote: str, *, udid: str = "u1") -> None:
    _log(cfg).record_delivery(
        app=app, bundle_id="b", inputs_digest=None, digest_version=None, cyan=None,
        tweaks=[], source_version="1", ipa_sha256=None, delivery="livecontainer",
        lc_bundle_id=lc, remote_name=remote, udid=udid,
    )


APPS = {
    "example": {"bundle_id": "com.example.app", "delivery": "livecontainer"},
    "bravo": {"bundle_id": "com.example.bravo", "delivery": "livecontainer",
              "lc_bundle_id": LC2},
}


def test_empty_deliveries_on_a_free_account_delete_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported case: SideStore's LiveContainer holds the user's own IPAs,
    one called like a configured app, and a certificate Graft cannot have
    pushed (free accounts cannot `--lc-push-cert`). Graft has pushed nothing."""
    cfg = _cfg(tmp_path, APPS)
    device = FakeDevice(
        {PRIMARY: {}, LC2: {}},
        docs={PRIMARY: ["example.ipa", CERT, "my-other.ipa"], LC2: ["bravo.ipa", "notes.txt"]},
    )
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events)

    assert device.removed == []


def test_recorded_deliveries_go_from_the_instance_they_went_into(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, APPS)
    _record(cfg, "example", PRIMARY, "example.ipa")
    _record(cfg, "bravo", LC2, "bravo.ipa")
    device = FakeDevice(
        {PRIMARY: {}, LC2: {}},
        # `bravo.ipa` in the primary and `example.ipa` in LC2 were not pushed
        # there; neither goes.
        docs={PRIMARY: ["example.ipa", "bravo.ipa"], LC2: ["bravo.ipa", "example.ipa"]},
    )
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events)

    assert device.removed == [(LC2, "bravo.ipa"), (PRIMARY, "example.ipa")]


def test_a_delivery_to_another_device_is_not_evidence_here(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _cfg(tmp_path, APPS)
    _record(cfg, "example", PRIMARY, "example.ipa", udid="another-phone")
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa"]})
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events)

    assert device.removed == []


def test_two_primaries_do_not_stop_a_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Where an unpinned push would go is ambiguous here, and a push asks for
    a pin. Clean needs no such answer: the records say where each file went."""
    cfg = _cfg(tmp_path, {"example": {"bundle_id": "com.example.app"}})
    _record(cfg, "example", OTHER_PRIMARY, "example.ipa")
    device = FakeDevice({PRIMARY: {}, OTHER_PRIMARY: {}},
                        docs={PRIMARY: ["example.ipa"], OTHER_PRIMARY: ["example.ipa"]})
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events)

    assert device.removed == [(OTHER_PRIMARY, "example.ipa")]


def test_a_recorded_certificate_is_removed_once_and_then_forgotten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a clean, a certificate the user puts there themselves is theirs."""
    cfg = _cfg(tmp_path, {}, team=TEAM)
    _log(cfg).record_cert_push(udid="u1", bundle_id=PRIMARY, name=CERT)
    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: [CERT]})
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)

    lc_manage.run_clean(cfg, udid=None, events=Recorder().events, cert_only=True)
    assert device.removed == [(PRIMARY, CERT)]

    device.removed.clear()
    rec = Recorder()
    lc_manage.run_clean(cfg, udid=None, events=rec.events, cert_only=True)
    assert device.removed == []
    assert len(rec.warnings) == 1, "the one still there is reported, not deleted"


def test_a_certificate_from_before_records_is_reported_where_it_was_put(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v1.0's `--lc-push-cert` wrote to the team's primary and recorded nothing."""
    cfg = _cfg(tmp_path, {}, team=TEAM)
    device = FakeDevice({PRIMARY: {}, LC3: {}}, docs={PRIMARY: [CERT], LC3: [CERT]})
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)
    rec = Recorder()

    lc_manage.run_clean(cfg, udid=None, events=rec.events, cert_only=True)

    assert device.removed == []
    [warning] = rec.warnings
    assert warning.startswith(f"{PRIMARY}: left {CERT}")
