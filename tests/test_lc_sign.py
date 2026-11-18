"""Signing LiveContainer inside-out through a throwaway keychain — with fakes.

Nothing here runs `codesign` or `security`: `FakeRunner` records argv and
answers from fixtures. The real thing is the opt-in golden check in
`test_lc_golden.py`.
"""

from __future__ import annotations

import plistlib
import struct
from pathlib import Path

import pytest

from graft_ipa import lc_sign
from graft_ipa.errors import SigningError
from graft_ipa.lc_runner import LiveContainerError

from .test_lc_entitlements import EXACT_PROFILE, FIXTURES, SIDESTORE_GROUP, TEAM, WILDCARD_PROFILE

MAIN_ID = f"com.kdt.livecontainer.{TEAM}"
LOGIN = "/Users/someone/Library/Keychains/login.keychain-db"
SYSTEM = "/Library/Keychains/System.keychain"
MACHO = struct.pack("<I", 0xFEEDFACF) + b"\0" * 60


class FakeRunner(lc_sign.Runner):
    """Records every command. `fail_when(cmd)` true → raise like a failed run."""

    def __init__(self, fail_when=lambda cmd: False, search: list[str] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail_when = fail_when
        #: The user's keychain search list, as `security` would keep it.
        self.search: list[str] = list(search) if search is not None else [LOGIN, SYSTEM]

    def run(self, cmd, *, cwd=None, check=True):
        self.calls.append(list(cmd))
        if check and self.fail_when(cmd):
            raise SigningError(f"{cmd[0]} failed")
        stdout = ""
        if cmd[:2] == ["codesign", "-d"]:
            fixture = FIXTURES / f"{Path(cmd[-1]).name}.entitlements.xml"
            stdout = fixture.read_text() if fixture.exists() else ""
        elif cmd == ["security", "list-keychains", "-d", "user"]:
            stdout = "".join(f'    "{p}"\n' for p in self.search)
        elif cmd[:5] == ["security", "list-keychains", "-d", "user", "-s"]:
            self.search = list(cmd[5:])
        elif cmd[:2] == ["security", "delete-keychain"]:
            # `security delete-keychain` also takes it off the search list.
            self.search = [p for p in self.search if p != cmd[2]]
        import subprocess

        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    def signed(self) -> list[str]:
        """Paths passed to `codesign -f`, in order."""
        return [c[-1] for c in self.calls if c[:2] == ["codesign", "-f"]]


def _bundle(path: Path, bundle_id: str, exe: str) -> Path:
    path.mkdir(parents=True)
    (path / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": bundle_id, "CFBundleExecutable": exe})
    )
    (path / exe).write_bytes(MACHO)
    return path


def _make_app(root: Path) -> Path:
    app = _bundle(root / "LiveContainer.app", MAIN_ID, "LiveContainer")
    _bundle(app / "Frameworks" / "OpenSSL.framework", "org.openssl", "OpenSSL")
    (app / "Frameworks" / "TweakLoader.dylib").write_bytes(MACHO)
    _bundle(app / "PlugIns" / "LiveProcess.appex", f"{MAIN_ID}.LiveProcess", "LiveProcess")
    _bundle(app / "PlugIns" / "ShareExtension.appex", f"{MAIN_ID}.ShareExtension", "ShareExtension")
    return app


def _profiles(root: Path) -> tuple[dict[str, Path], dict[Path, dict]]:
    """bundle id → profile file, and profile file → decoded plist."""
    exact = root / "main.mobileprovision"
    live = root / "liveprocess.mobileprovision"
    wild = root / "wildcard.mobileprovision"
    for p, body in ((exact, b"MAIN"), (live, b"LIVE"), (wild, b"WILD")):
        p.write_bytes(body)
    live_ents = {**EXACT_PROFILE, "application-identifier": f"{TEAM}.{MAIN_ID}.LiveProcess"}
    decoded = {
        exact: {"Name": "main", "TeamIdentifier": [TEAM], "Entitlements": EXACT_PROFILE},
        live: {"Name": "live", "TeamIdentifier": [TEAM], "Entitlements": live_ents},
        wild: {"Name": "wild", "TeamIdentifier": [TEAM], "Entitlements": WILDCARD_PROFILE},
    }
    by_id = {
        MAIN_ID: exact,
        f"{MAIN_ID}.LiveProcess": live,
        f"{MAIN_ID}.ShareExtension": wild,
    }
    return by_id, decoded


def _sign(tmp_path: Path, runner: FakeRunner) -> tuple[Path, list[lc_sign.BundleReport]]:
    app = _make_app(tmp_path / "x")
    by_id, decoded = _profiles(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    reports = lc_sign.sign_app(
        app, profile_for=by_id.__getitem__, identity="ABCDEF", keychain=tmp_path / "k.db",
        runner=runner, workdir=work, load_profile=decoded.__getitem__,
    )
    return app, reports


def _entitlements_passed_for(runner: FakeRunner, path: Path) -> dict:
    cmd = next(c for c in runner.calls if c[:2] == ["codesign", "-f"] and c[-1] == str(path))
    return plistlib.loads(Path(cmd[cmd.index("--entitlements") + 1]).read_bytes())


# ---- order and profiles ----


def test_bundles_are_signed_inside_out(tmp_path: Path) -> None:
    runner = FakeRunner()
    app, _ = _sign(tmp_path, runner)

    order = [Path(p).name for p in runner.signed()]
    assert order == [
        "OpenSSL.framework",
        "TweakLoader.dylib",
        "LiveProcess.appex",
        "ShareExtension.appex",
        "LiveContainer.app",
    ]
    assert runner.calls[-1] == ["codesign", "--verify", "--deep", "--strict", str(app)]


def test_each_bundle_embeds_the_profile_for_its_own_bundle_id(tmp_path: Path) -> None:
    app, _ = _sign(tmp_path, FakeRunner())

    assert (app / "embedded.mobileprovision").read_bytes() == b"MAIN"
    live = app / "PlugIns" / "LiveProcess.appex" / "embedded.mobileprovision"
    share = app / "PlugIns" / "ShareExtension.appex" / "embedded.mobileprovision"
    assert live.read_bytes() == b"LIVE"
    assert share.read_bytes() == b"WILD"


def test_the_app_is_signed_with_the_explicit_keychain_groups(tmp_path: Path) -> None:
    runner = FakeRunner()
    app, reports = _sign(tmp_path, runner)

    ents = _entitlements_passed_for(runner, app)
    assert ents["application-identifier"] == f"{TEAM}.{MAIN_ID}"
    assert len(ents["keychain-access-groups"]) == 128
    assert ents["com.apple.security.application-groups"] == [SIDESTORE_GROUP]
    main = reports[-1]
    assert (main.bundle_id, main.keychain_groups, main.app_groups) == (MAIN_ID, 128, [SIDESTORE_GROUP])


def test_each_extension_gets_its_own_entitlements(tmp_path: Path) -> None:
    runner = FakeRunner()
    app, _ = _sign(tmp_path, runner)

    share = _entitlements_passed_for(runner, app / "PlugIns" / "ShareExtension.appex")
    assert share["application-identifier"] == f"{TEAM}.{MAIN_ID}.ShareExtension"
    assert "com.apple.security.application-groups" not in share


def test_frameworks_carry_no_entitlements(tmp_path: Path) -> None:
    runner = FakeRunner()
    app, _ = _sign(tmp_path, runner)

    fw = str(app / "Frameworks" / "OpenSSL.framework")
    cmd = next(c for c in runner.calls if c[:2] == ["codesign", "-f"] and c[-1] == fw)
    assert "--entitlements" not in cmd


def test_every_signature_is_der_untimestamped_and_from_the_temp_keychain(tmp_path: Path) -> None:
    runner = FakeRunner()
    _sign(tmp_path, runner)

    for cmd in (c for c in runner.calls if c[:2] == ["codesign", "-f"]):
        assert "--generate-entitlement-der" in cmd
        assert "--timestamp=none" in cmd
        assert cmd[cmd.index("--keychain") + 1] == str(tmp_path / "k.db")
        assert cmd[cmd.index("-s") + 1] == "ABCDEF"


# ---- untrusted bundle contents ----


def _sign_prepared(tmp_path: Path, app: Path, runner: FakeRunner) -> None:
    by_id, decoded = _profiles(tmp_path)
    lc_sign.sign_app(
        app, profile_for=by_id.__getitem__, identity="ABCDEF", keychain=tmp_path / "k.db",
        runner=runner, workdir=tmp_path, load_profile=decoded.__getitem__,
    )


@pytest.mark.parametrize("bad", ["../evil", "/abs/evil"])
@pytest.mark.parametrize("where", [".", "PlugIns/LiveProcess.appex"])
def test_an_executable_named_outside_its_bundle_is_never_signed(
    tmp_path: Path, bad: str, where: str
) -> None:
    app = _make_app(tmp_path / "x")
    plist = app / where / "Info.plist"
    info = plistlib.loads(plist.read_bytes())
    info["CFBundleExecutable"] = bad
    plist.write_bytes(plistlib.dumps(info))
    runner = FakeRunner()

    with pytest.raises(LiveContainerError) as exc:
        _sign_prepared(tmp_path, app, runner)

    assert "CFBundleExecutable" in exc.value.message
    assert runner.signed() == []
    assert not any(c[:2] == ["codesign", "-d"] for c in runner.calls)


def test_a_symlink_anywhere_in_the_app_stops_signing_before_anything_is_signed(
    tmp_path: Path,
) -> None:
    """`codesign -f` follows a symlink and rewrites its target in place."""
    app = _make_app(tmp_path / "x")
    outside = tmp_path / "outside.dylib"
    outside.write_bytes(MACHO)
    (app / "Frameworks" / "Sneaky.dylib").symlink_to(outside)
    runner = FakeRunner()

    with pytest.raises(LiveContainerError) as exc:
        _sign_prepared(tmp_path, app, runner)

    assert exc.value.message == "LiveContainer.app contains a symlink: Frameworks/Sneaky.dylib"
    assert runner.signed() == []


# ---- the keychain ----

REAL_PASSWORD = "the-users-real-p12-password"


def _make_p12(tmp_path: Path, password: str = REAL_PASSWORD) -> tuple[Path, str]:
    """A real .p12 (EC key, self-signed cert) and its certificate's SHA-1."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Apple Development: Test")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(1)
        .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    p12 = tmp_path / "id.p12"
    p12.write_bytes(pkcs12.serialize_key_and_certificates(
        b"id", key, cert, None, serialization.BestAvailableEncryption(password.encode())
    ))
    return p12, cert.fingerprint(hashes.SHA1()).hex().upper()


def _list_sets(runner: FakeRunner) -> list[list[str]]:
    return [c[5:] for c in runner.calls if c[:5] == ["security", "list-keychains", "-d", "user", "-s"]]


def _import_call(runner: FakeRunner) -> list[str]:
    return next(c for c in runner.calls if c[:2] == ["security", "import"])


def test_the_search_list_is_extended_then_restored_exactly(tmp_path: Path) -> None:
    runner = FakeRunner()
    p12, _ = _make_p12(tmp_path)
    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
        assert _list_sets(runner) == [[LOGIN, SYSTEM, str(kc)]]

    assert _list_sets(runner)[-1] == [LOGIN, SYSTEM]
    assert runner.calls[-1] == ["security", "delete-keychain", str(kc)]


def test_the_search_list_is_restored_when_signing_fails(tmp_path: Path) -> None:
    runner = FakeRunner(fail_when=lambda cmd: cmd[:2] == ["codesign", "-f"])
    p12, _ = _make_p12(tmp_path)
    with pytest.raises(SigningError):
        with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
            _sign_into(tmp_path, runner, kc)

    assert _list_sets(runner)[-1] == [LOGIN, SYSTEM]
    assert ["security", "delete-keychain", str(kc)] in runner.calls


def test_a_failed_import_still_removes_the_keychain_and_leaves_the_list(tmp_path: Path) -> None:
    """The import fails before the search list was touched."""
    runner = FakeRunner(fail_when=lambda cmd: cmd[:2] == ["security", "import"])
    p12, _ = _make_p12(tmp_path)
    with pytest.raises(SigningError):
        with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path):
            pytest.fail("body must not run")

    assert _list_sets(runner) == [], "the list was never touched, so nothing is written back"
    assert runner.search == [LOGIN, SYSTEM]
    assert any(c[:2] == ["security", "delete-keychain"] for c in runner.calls)


# ---- the search list survives what a `finally` cannot see ----

STALE = "/private/var/folders/xx/T/lcsign-old/lcsign-dead1234.keychain-db"


def test_a_keychain_a_killed_run_left_on_the_list_is_removed(tmp_path: Path) -> None:
    """SIGKILL skips every `finally`. The next run must not snapshot the
    leftover and carry it forward — it drops it, and deletes the file if it
    is still there."""
    leftover = tmp_path / "lcsign-alive999.keychain-db"
    leftover.write_bytes(b"x")
    runner = FakeRunner(search=[LOGIN, STALE, str(leftover), SYSTEM])
    p12, _ = _make_p12(tmp_path)

    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
        assert runner.search == [LOGIN, SYSTEM, str(kc)]

    assert runner.search == [LOGIN, SYSTEM]
    deleted = [c[2] for c in runner.calls if c[:2] == ["security", "delete-keychain"]]
    assert str(leftover) in deleted, "a stale keychain file that still exists is deleted"
    assert STALE not in deleted, "one already gone is only taken off the list"


def _dead_pid() -> int:
    """A PID that existed a moment ago and does not now."""
    import subprocess

    proc = subprocess.Popen(["/usr/bin/true"])
    proc.wait()
    return proc.pid


def test_a_concurrent_runs_live_keychain_is_left_alone(tmp_path: Path) -> None:
    """A terminal run (or scripts/lc_resign.py) signing while the GUI starts
    one: its keychain is on the list, its process alive, its file present.
    Sweeping it would pull the identity out from under codesign mid-run."""
    import os

    live = tmp_path / f"lcsign-{os.getpid()}-abcdef12.keychain-db"
    live.write_bytes(b"x")
    runner = FakeRunner(search=[LOGIN, SYSTEM, str(live)])
    p12, _ = _make_p12(tmp_path)

    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
        assert runner.search == [LOGIN, SYSTEM, str(live), str(kc)]

    assert runner.search == [LOGIN, SYSTEM, str(live)]
    assert ["security", "delete-keychain", str(live)] not in runner.calls


def test_a_dead_runs_keychain_is_swept(tmp_path: Path) -> None:
    dead = tmp_path / f"lcsign-{_dead_pid()}-abcdef12.keychain-db"
    dead.write_bytes(b"x")
    runner = FakeRunner(search=[LOGIN, str(dead), SYSTEM])
    p12, _ = _make_p12(tmp_path)

    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path):
        pass

    assert runner.search == [LOGIN, SYSTEM]
    assert ["security", "delete-keychain", str(dead)] in runner.calls


def test_a_live_pid_whose_keychain_file_is_gone_is_swept(tmp_path: Path) -> None:
    import os

    gone = str(tmp_path / f"lcsign-{os.getpid()}-abcdef12.keychain-db")
    runner = FakeRunner(search=[LOGIN, gone, SYSTEM])
    p12, _ = _make_p12(tmp_path)

    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path):
        pass

    assert runner.search == [LOGIN, SYSTEM]


def test_our_keychain_name_carries_our_pid(tmp_path: Path) -> None:
    import os

    runner = FakeRunner()
    p12, _ = _make_p12(tmp_path)
    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
        assert kc.name.startswith(f"lcsign-{os.getpid()}-")


def test_a_list_changed_during_the_run_is_kept_except_for_ours(tmp_path: Path) -> None:
    """Restoring a snapshot would undo this — and undo a concurrent run's own
    clean-up, putting its keychain back."""
    runner = FakeRunner()
    p12, _ = _make_p12(tmp_path)
    other = "/Users/someone/Library/Keychains/added-meanwhile.keychain-db"

    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path):
        runner.search.append(other)

    assert runner.search == [LOGIN, SYSTEM, other]


def test_systemexit_in_the_body_still_restores_and_deletes(tmp_path: Path) -> None:
    """What a SIGTERM becomes under `exit_on_termination`."""
    runner = FakeRunner()
    p12, _ = _make_p12(tmp_path)

    with pytest.raises(SystemExit):
        with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
            raise SystemExit(143)

    assert runner.search == [LOGIN, SYSTEM]
    assert ["security", "delete-keychain", str(kc)] in runner.calls


def test_the_real_p12_password_never_reaches_argv(tmp_path: Path) -> None:
    """`security import -P <password>` is visible to every user via `ps` for as
    long as it runs. The .p12 is re-encrypted in-process under a throwaway
    password, and only that one is ever passed on a command line."""
    runner = FakeRunner()
    p12, _ = _make_p12(tmp_path)
    with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path):
        pass

    assert not [c for c in runner.calls if any(REAL_PASSWORD in arg for arg in c)]
    imported = _import_call(runner)
    assert imported[2] != str(p12)


def test_the_reencrypted_copy_opens_with_the_password_it_was_imported_with(
    tmp_path: Path,
) -> None:
    """And holds the same identity — otherwise codesign -s <sha1> finds nothing."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.serialization import pkcs12

    seen: dict[str, bytes] = {}

    class Capturing(FakeRunner):
        def run(self, cmd, *, cwd=None, check=True):
            if cmd[:2] == ["security", "import"]:
                seen["bytes"] = Path(cmd[2]).read_bytes()
                seen["mode"] = Path(cmd[2]).stat().st_mode & 0o777
                seen["password"] = cmd[cmd.index("-P") + 1].encode()
            return super().run(cmd, cwd=cwd, check=check)

    p12, sha1 = _make_p12(tmp_path)
    with lc_sign.temp_keychain(Capturing(), p12, REAL_PASSWORD, tmp_path):
        pass

    _, cert, _ = pkcs12.load_key_and_certificates(seen["bytes"], seen["password"])
    assert cert.fingerprint(hashes.SHA1()).hex().upper() == sha1
    assert seen["mode"] == 0o600


@pytest.mark.parametrize("fail_on", ["import", "codesign"])
def test_the_reencrypted_copy_is_deleted_on_success_and_failure(
    tmp_path: Path, fail_on: str
) -> None:
    runner = FakeRunner(fail_when=lambda cmd: cmd[:2] == ["security", "import"]
                        if fail_on == "import" else cmd[:2] == ["codesign", "-f"])
    p12, _ = _make_p12(tmp_path)
    with pytest.raises(SigningError):
        with lc_sign.temp_keychain(runner, p12, REAL_PASSWORD, tmp_path) as kc:
            _sign_into(tmp_path, runner, kc)

    assert not Path(_import_call(runner)[2]).exists()

    # On success it is gone as soon as the import is — not merely by the end.
    ok = FakeRunner()
    with lc_sign.temp_keychain(ok, p12, REAL_PASSWORD, tmp_path):
        assert not Path(_import_call(ok)[2]).exists()


def _sign_into(tmp_path: Path, runner: FakeRunner, keychain: Path) -> None:
    app = _make_app(tmp_path / "x")
    by_id, decoded = _profiles(tmp_path)
    lc_sign.sign_app(
        app, profile_for=by_id.__getitem__, identity="ABCDEF", keychain=keychain,
        runner=runner, workdir=tmp_path, load_profile=decoded.__getitem__,
    )


# ---- App ID prefix ----


def test_each_bundle_is_signed_with_its_own_profiles_app_id_prefix(tmp_path: Path) -> None:
    """The prefix comes from each profile's `ApplicationIdentifierPrefix`, the
    same field `mobileprovision.cache_problem` judges a cached profile by — not
    from the team id, which differs on accounts from before 2011."""
    prefix = "PREFIX9876"
    app = _make_app(tmp_path / "x")
    by_id, decoded = _profiles(tmp_path)
    for plist in decoded.values():
        plist["ApplicationIdentifierPrefix"] = [prefix]
        ents = dict(plist["Entitlements"])
        ents["application-identifier"] = ents["application-identifier"].replace(
            f"{TEAM}.", f"{prefix}.", 1
        )
        # Such an account's profiles grant keychain groups under the prefix too.
        ents["keychain-access-groups"] = [
            g.replace(f"{TEAM}.", f"{prefix}.", 1) for g in ents["keychain-access-groups"]
        ]
        plist["Entitlements"] = ents
    runner = FakeRunner()

    reports = lc_sign.sign_app(
        app, profile_for=by_id.__getitem__, identity="ABCDEF", keychain=tmp_path / "k.db",
        runner=runner, workdir=tmp_path, load_profile=decoded.__getitem__,
    )

    main = _entitlements_passed_for(runner, app)
    share = _entitlements_passed_for(runner, app / "PlugIns" / "ShareExtension.appex")
    assert main["application-identifier"] == f"{prefix}.{MAIN_ID}"
    assert share["application-identifier"] == f"{prefix}.{MAIN_ID}.ShareExtension"
    assert main["com.apple.developer.team-identifier"] == TEAM
    assert reports[-1].application_identifier == f"{prefix}.{MAIN_ID}"
    # The keychain hook's groups survive, under the prefix.
    assert reports[-1].keychain_groups == 128
    assert main["keychain-access-groups"][0] == f"{prefix}.com.kdt.livecontainer.shared"
    assert main["com.apple.security.application-groups"] == [SIDESTORE_GROUP]


def test_a_profile_with_no_prefix_falls_back_to_the_team(tmp_path: Path) -> None:
    """`_profiles` declares no `ApplicationIdentifierPrefix` at all."""
    runner = FakeRunner()
    app, _ = _sign(tmp_path, runner)

    assert _entitlements_passed_for(runner, app)["application-identifier"] == f"{TEAM}.{MAIN_ID}"


# ---- identity ----


def test_the_identity_is_the_certificate_sha1(tmp_path: Path) -> None:
    p12, sha1 = _make_p12(tmp_path, "pw")

    assert lc_sign.p12_identity(p12, "pw") == sha1
    with pytest.raises(SigningError):
        lc_sign.p12_identity(p12, "wrong")
