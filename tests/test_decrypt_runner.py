"""Driving bagbak to dump a decrypted IPA.

The dump itself needs a jailbroken device with frida-server, so what a test
pins is the argv (the interface bagbak actually has), the two failures worth
naming over a raw Node traceback, and that a missing tool is caught before
anything runs.
"""

from __future__ import annotations

import types
import typing
from pathlib import Path

import pytest

from graft_ipa import decrypt_runner
from graft_ipa.decrypt_runner import DecryptError, build_command


def test_the_argv_matches_bagbaks_real_interface(tmp_path: Path):
    """`bagbak -D <udid> -o <dir> <bundle> <mode>` — verified against bagbak
    5.1.2's --help. -D pins the device (two phones on the wire), and -o is a
    directory because a filename tripped an internal assertion in main mode.

    `major` is explicit. Without it this asked the *installed* bagbak, so the
    assertion silently described whichever version happened to be on the
    machine — and started failing the moment one was installed whose argv it
    wasn't written for. A test that documents 5.x's interface has to say 5.
    """
    cmd = build_command("com.x", tmp_path, udid="UDID-1", major=5)
    assert cmd == ["bagbak", "-D", "UDID-1", "-o", str(tmp_path), "com.x", "all"]


def test_the_default_mode_is_all():
    """Only `all` produces a full app bundle the pipeline can inject into;
    `main` writes a bare binaries.zip with no Payload. (5.x only — 4.x has no
    mode positional, see TestBagbakMajors.)"""
    cmd = build_command("com.x", Path("/tmp/d"), udid=None, major=5)
    assert cmd[-1] == "all"


def test_the_udid_is_omitted_when_none(tmp_path: Path):
    cmd = build_command("com.x", tmp_path / "a.ipa", udid=None)
    assert "-D" not in cmd


def test_a_missing_bagbak_is_caught_before_running(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: None)
    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app("com.x", tmp_path / "a.ipa", udid=None)
    assert "not found" in str(e.value)


def test_dry_run_reports_the_command_without_running(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/usr/local/bin/bagbak")
    result = decrypt_runner.decrypt_app("com.x", tmp_path / "a.ipa", udid="U", dry_run=True)
    assert "bagbak" in result.stdout and "com.x" in result.stdout


@pytest.mark.parametrize("output,needle", [
    ("Error: unable to connect to remote frida-server", "frida-server"),
    ("ECONNREFUSED 127.0.0.1", "frida-server"),
    ("Error: application not found: com.x", "not installed"),
    # The real first-dump failure: iOS won't launch an app to decrypt while
    # the phone is locked.
    ("Unable to launch com.x because the device was not, or could not be, unlocked.", "unlock"),
    # The hit-or-miss extension failure the user flagged.
    ("Retry 2/3 for com.x.share-extension...\nUnable to find process with pid 2049", "extension"),
])
def test_the_two_named_failures_are_diagnosed(monkeypatch, tmp_path: Path, output, needle):
    """bagbak's own output for 'no jailbreak running' and 'app not installed'
    is a Node stack trace; these turn it into the one useful sentence."""
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/bin/bagbak")

    # `_run_streaming` is the seam now: the backends are spawned with Popen and
    # read line by line, so there is no `subprocess.run` left to stand in for.
    monkeypatch.setattr(
        decrypt_runner, "_run_streaming", lambda *a, **k: (1, output)
    )
    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app("com.x", tmp_path / "a.ipa", udid="U")
    assert needle in str(e.value) + str(e.value.hint or "")


def test_an_unrecognized_failure_carries_bagbaks_tail(monkeypatch, tmp_path: Path):
    """A failure that isn't one of the two named ones still surfaces bagbak's
    last lines rather than swallowing them."""
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/bin/bagbak")

    monkeypatch.setattr(
        decrypt_runner, "_run_streaming",
        lambda *a, **k: (2, "line one\nsomething specific went wrong\n"),
    )
    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app("com.x", tmp_path / "a.ipa", udid="U")
    assert "something specific" in str(e.value.hint or "")


# --- the frida-ios-dump backend: attach to a running app, pull over SSH ---


def test_frida_backend_availability_needs_both_script_and_venv(monkeypatch, tmp_path: Path):
    """A clone without its venv would fail deep in paramiko; the preflight
    requires both the script and the interpreter."""
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    assert decrypt_runner.frida_ios_dump_available() is None
    # Script alone is not enough — a clone with no venv fails deep in paramiko.
    (tmp_path / "dump.py").write_text("x")
    assert decrypt_runner.frida_ios_dump_available() is None
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("x")
    assert decrypt_runner.frida_ios_dump_available() is not None


def test_available_backends_reflects_what_is_installed(monkeypatch):
    monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/bin/bagbak")
    monkeypatch.setattr(decrypt_runner, "frida_ios_dump_available", lambda: None)
    assert decrypt_runner.available_backends() == [decrypt_runner.BAGBAK]
    monkeypatch.setattr(decrypt_runner, "frida_ios_dump_available", lambda: Path("/p"))
    assert decrypt_runner.available_backends() == [decrypt_runner.BAGBAK, decrypt_runner.FRIDA]


def test_the_frida_dry_run_argv_uses_ssh_over_the_forwarded_port(monkeypatch, tmp_path: Path, fake_iproxy):
    """`dump.py -o <ipa> -H localhost -p <port> -u root -P … <bundle>` — the
    SSH form frida-ios-dump documents, over the port iproxy binds. The password
    is set in-process by its launcher, never shown (`test_frida_dump_password`).

    The port is chosen per dump rather than fixed: two dumps at once used to
    fight over 2222, and the loser's iproxy exited silently while its dump read
    from the winner's tunnel."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)

    result = decrypt_runner.decrypt_app(
        "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA, dry_run=True
    )
    argv = result.stdout
    assert "dump.py" in argv and "-H localhost" in argv
    port = int(argv.split("-p ")[1].split()[0])
    assert port != 2222 and 1024 < port < 65536, f"expected an own port, got {port}"
    assert "-u root" in argv and "-P <from $" in argv and "com.x" in argv
    assert "alpine" not in argv


def test_frida_ssh_creds_come_from_the_environment(monkeypatch, tmp_path: Path, fake_iproxy):
    """A device whose root password was changed (common, good practice) is
    otherwise unreachable; a key takes precedence over a password."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)

    monkeypatch.setenv("GRAFT_SSH_USER", "mobile")
    monkeypatch.setenv("GRAFT_SSH_PASS", "hunter2")
    argv = decrypt_runner.decrypt_app(
        "com.x", tmp_path / "o", udid="U", backend=decrypt_runner.FRIDA, dry_run=True
    ).stdout
    # The user from the environment; the password reaches dump.py through its
    # launcher, never on argv or in this rendering.
    assert "-u mobile" in argv and "hunter2" not in argv

    monkeypatch.setenv("GRAFT_SSH_KEY", "/home/me/.ssh/id_ed25519")
    argv = decrypt_runner.decrypt_app(
        "com.x", tmp_path / "o", udid="U", backend=decrypt_runner.FRIDA, dry_run=True
    ).stdout
    assert "-K /home/me/.ssh/id_ed25519" in argv and "-P" not in argv


@pytest.mark.parametrize(
    "outcome, must_say, must_not_say",
    [
        # An app that kills itself on a jailbroken device was being described as
        # a locked phone, sending the user to hunt for a lock that is not there.
        (decrypt_runner.LAUNCH_DIED, "exited on its own", "unlock"),
        (decrypt_runner.LAUNCH_SLOW, "never reached the foreground", "unlock"),
        (decrypt_runner.LAUNCH_REFUSED, "unlock the phone", "exited"),
    ],
)
def test_each_way_a_launch_can_fail_gets_its_own_advice(
    monkeypatch, tmp_path: Path, outcome, must_say, must_not_say
):
    """Three failures that look identical from a timeout and want opposite
    advice — the reason `ensure_frontmost` answers with a reason rather than a
    boolean."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")
    monkeypatch.setattr(decrypt_runner, "ensure_frontmost", lambda *_a, **_k: outcome)

    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    said = str(e.value).lower()
    assert must_say in said
    assert must_not_say not in said


def test_the_frida_backend_launches_the_app_it_is_asked_to_dump(
    monkeypatch, tmp_path: Path
):
    """The change that makes a dump one action instead of two."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")
    asked: list[str] = []

    def fake_ensure(bundle_id, _udid, **_kw):
        asked.append(bundle_id)
        # Stop before the tunnel; the launch is what is under test.
        return decrypt_runner.LAUNCH_REFUSED

    monkeypatch.setattr(decrypt_runner, "ensure_frontmost", fake_ensure)
    with pytest.raises(DecryptError):
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    assert asked == ["com.x"]


def test_the_frida_backend_needs_iproxy(monkeypatch, tmp_path: Path):
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: None)

    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    assert "iproxy" in str(e.value)


def test_a_frida_backend_that_is_not_set_up_says_so(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path / "nope")
    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    assert "not set up" in str(e.value)


def _install_fake_frida(monkeypatch, *, frontmost: str | None, boom: bool = False):
    """A frida for `frontmost_app`, at the seam it reaches frida through
    (`_import_frida`, which asks frida-ios-dump's venv in real use).

    No real device: the fake answers `get_frontmost_application` with an object
    carrying `.identifier` (or None for the home screen), and raises on connect
    when `boom` is set — the unreachable-device case.
    """
    import types

    fake = types.ModuleType("frida")

    class _App:
        def __init__(self, identifier: str):
            self.identifier = identifier

    class _Device:
        def get_frontmost_application(self):
            return _App(frontmost) if frontmost else None

    def _connect(*_a, **_k):
        if boom:
            raise RuntimeError("failed to enumerate devices")
        return _Device()

    fake.get_usb_device = _connect
    fake.get_device = _connect
    monkeypatch.setattr(decrypt_runner, "_import_frida", lambda _need: fake)


def test_frontmost_app_reports_the_bundle_id_on_screen(monkeypatch):
    _install_fake_frida(monkeypatch, frontmost="com.example.front")
    assert decrypt_runner.frontmost_app(None) == "com.example.front"


def test_frontmost_app_is_none_on_the_home_screen(monkeypatch):
    """No frontmost app is a normal state the caller turns into 'open an app
    first' — it must be a distinguishable None, not an error and not a crash on
    the missing app's `.identifier`."""
    _install_fake_frida(monkeypatch, frontmost=None)
    assert decrypt_runner.frontmost_app(None) is None


def test_frontmost_app_raises_when_the_device_is_unreachable(monkeypatch):
    """A frida connect failure gets its own error and hint, not silently the
    same None the home screen returns — the two need different advice."""
    _install_fake_frida(monkeypatch, frontmost=None, boom=True)
    with pytest.raises(DecryptError) as e:
        decrypt_runner.frontmost_app("U")
    assert "foreground app" in str(e.value)


class TestSSHPasswordResolution:
    """Where the device's root SSH password comes from, and in what order.

    The order is the point. `alpine` is the stock jailbreak password, and a
    device whose root password was changed — which is the correct thing to
    do — is unreachable without a real one. Before this, the only way to
    supply one was `GRAFT_SSH_PASS`, which a Dock-launched GUI never sees: it
    inherits the launchd environment. So decryption worked from a shell and
    silently failed from the app, which is the gap the Keychain step closes.
    """

    def test_the_environment_wins(self, monkeypatch):
        """A one-off run can override stored state without touching it.

        The Keychain is stubbed to a *different* value rather than to None, so
        this fails if the precedence is ever flipped — stubbing it empty would
        pass either way.
        """
        import graft_ipa.keychain as kc

        monkeypatch.setenv("GRAFT_SSH_PASS", "from-env")
        monkeypatch.setattr(kc, "read_ssh_password", lambda udid: "from-keychain")
        assert decrypt_runner.resolve_ssh_password("UDID-1") == "from-env"

    def test_the_keychain_is_used_when_the_environment_is_unset(self, monkeypatch):
        import graft_ipa.keychain as kc

        monkeypatch.delenv("GRAFT_SSH_PASS", raising=False)
        monkeypatch.setattr(kc, "read_ssh_password", lambda udid: "from-keychain")
        assert decrypt_runner.resolve_ssh_password("UDID-1") == "from-keychain"

    def test_the_keychain_is_read_per_device(self, monkeypatch):
        """Keyed by UDID because the password belongs to a *device*. A single
        shared entry would send the jailbroken phone's password to whatever
        else is on the bench."""
        import graft_ipa.keychain as kc

        monkeypatch.delenv("GRAFT_SSH_PASS", raising=False)
        seen = []
        monkeypatch.setattr(
            kc, "read_ssh_password", lambda udid: seen.append(udid) or f"pw-for-{udid}"
        )
        assert decrypt_runner.resolve_ssh_password("UDID-A") == "pw-for-UDID-A"
        assert decrypt_runner.resolve_ssh_password("UDID-B") == "pw-for-UDID-B"
        assert seen == ["UDID-A", "UDID-B"]

    def test_it_falls_back_to_the_stock_password(self, monkeypatch):
        """A stock jailbreak keeps working with no setup at all."""
        import graft_ipa.keychain as kc

        monkeypatch.delenv("GRAFT_SSH_PASS", raising=False)
        monkeypatch.setattr(kc, "read_ssh_password", lambda udid: None)
        assert decrypt_runner.resolve_ssh_password("UDID-1") == decrypt_runner.DEFAULT_SSH_PASSWORD
        assert decrypt_runner.DEFAULT_SSH_PASSWORD == "alpine"

    def test_no_device_means_no_keychain_lookup(self, monkeypatch):
        """There is no per-device entry to read without a UDID, and asking for
        one under a None key would either raise or read a bogus account."""
        import graft_ipa.keychain as kc

        monkeypatch.delenv("GRAFT_SSH_PASS", raising=False)
        monkeypatch.setattr(
            kc, "read_ssh_password", lambda udid: pytest.fail("looked up with no udid")
        )
        assert decrypt_runner.resolve_ssh_password(None) == decrypt_runner.DEFAULT_SSH_PASSWORD


class TestSSHFailureMessages:
    """Auth rejection and unreachability need opposite advice.

    Lumped into one regex, a device with a changed root password — the common,
    correct case — was told to install OpenSSH it already had.
    """

    @pytest.mark.parametrize(
        "line",
        [
            "paramiko.ssh_exception.AuthenticationException: Authentication failed.",
            "Permission denied (publickey,password).",
            "ssh: bad password for root",
        ],
    )
    def test_auth_failures_are_recognised(self, line):
        assert decrypt_runner._FRIDA_SSH_AUTH_RE.search(line)

    @pytest.mark.parametrize(
        "line",
        ["Connection refused", "No route to host", "Operation timed out"],
    )
    def test_unreachable_is_recognised(self, line):
        assert decrypt_runner._FRIDA_SSH_NO_ROUTE_RE.search(line)

    def test_an_auth_failure_is_not_read_as_unreachable(self):
        """The regression: "authentication failed" used to match the
        unreachable pattern, so the hint said to install software the device
        already had."""
        line = "paramiko.ssh_exception.AuthenticationException: Authentication failed."
        assert not decrypt_runner._FRIDA_SSH_NO_ROUTE_RE.search(line)


class TestStillEncryptedHint:
    """What the user is told when a dump comes back partly encrypted.

    Measured on the real device: frida-ios-dump against SmartLife returned 36
    binaries with exactly 11 still encrypted, and all 11 were `PlugIns/*.appex`.
    That is structural — `dump.js` decrypts modules loaded into the host app's
    process, and an extension runs in its own — so the old single hint ("force-
    quit it and retry") was advice that could never work, printed under the
    wrong backend's name.
    """

    def _hint(self, paths, backend, stripped_attempted=False):
        from graft_ipa.cli import _still_encrypted_hint

        return _still_encrypted_hint(paths, backend, stripped_attempted=stripped_attempted)

    # A fixture, never mutated — the annotation says so and quiets RUF012.
    APPEX: typing.ClassVar[list[str]] = [
        "Payload/smartlife.app/PlugIns/ThingWidget.appex/ThingWidget",
        "Payload/smartlife.app/PlugIns/ThingShare.appex/ThingShare",
    ]

    def test_frida_with_only_extensions_left_points_at_the_refusal(self):
        """Reachable only when the dump was told to keep them — dropping them is
        what it does otherwise — so the advice is to take that back, not to
        switch to bagbak, which may be unable to dump them either: an extension
        whose MinimumOSVersion is newer than the device's iOS cannot launch to
        be read."""
        hint = self._hint(self.APPEX, decrypt_runner.FRIDA)
        assert "--keep-encrypted-extensions" in hint
        # And it says why retrying is pointless for this backend.
        assert "own processes" in hint and "retrying will not change" in hint

    def test_bagbak_with_only_extensions_left_says_the_same(self):
        """Same advice, different reason: bagbak *can* dump extensions, so the
        explanation names the case where it can't rather than a blanket "it
        cannot"."""
        hint = self._hint(self.APPEX, decrypt_runner.BAGBAK)
        assert "--keep-encrypted-extensions" in hint
        assert "MinimumOSVersion" in hint

    def test_after_stripping_it_stops_blaming_extensions(self):
        """Extensions were removed and something is still encrypted, so the
        leftovers are the app's own binaries — a failed dump. Repeating the
        strip advice there would loop the user through an option that already
        ran."""
        hint = self._hint(
            ["Payload/x.app/x"], decrypt_runner.FRIDA, stripped_attempted=True
        )
        assert "--strip-encrypted-extensions" not in hint
        assert "outside any app extension" in hint

    def test_frida_never_tells_the_user_to_retry_an_unwinnable_dump(self):
        """The regression: retry advice on a structural failure. Every attempt
        returns the identical result, and force-quitting is worse still — with
        no live pid frida-ios-dump spawns instead of attaching."""
        hint = self._hint(self.APPEX, decrypt_runner.FRIDA)
        assert "force-quit" not in hint

    def test_bagbak_keeps_the_retry_advice_for_non_extension_leftovers(self):
        """bagbak's version of this genuinely is a race — iOS reaping a
        short-lived extension mid-attach — so retrying is the right move. Only
        when the leftovers aren't purely extensions, which has its own branch."""
        hint = self._hint(["Payload/x.app/x"], decrypt_runner.BAGBAK)
        assert "retry" in hint
        assert "bagbak" in hint

    def test_the_hint_never_names_the_backend_that_did_not_run(self):
        """The original hint said "bagbak" whatever had run, which sent people
        reading bagbak's docs after a frida-ios-dump failure."""
        assert "bagbak" not in self._hint(
            ["Payload/x.app/Frameworks/Core.framework/Core"], decrypt_runner.FRIDA
        ).replace("--decrypt-backend bagbak", "").replace("`bagbak`", "")

    def test_a_non_extension_failure_under_frida_is_not_blamed_on_extensions(self):
        """A framework left encrypted is a different problem — the module was
        never loaded — and must not be answered with "use bagbak, extensions"."""
        hint = self._hint(["Payload/x.app/Frameworks/Core.framework/Core"], decrypt_runner.FRIDA)
        assert "extensions run in their own" not in hint


class TestBagbakMajors:
    """bagbak 4.x and 5.x are different tools wearing one name.

    They take different argv, use different transports, and — the part that
    decides which one you can run at all — need different frida majors *on the
    device*. 5.x bundles frida 17; this project pins frida 16 because
    frida-ios-dump's agent uses pre-17 APIs, so the device runs a 16.x
    frida-server and 5.x cannot connect to it ("please ensure that major
    versions match"). Confirmed against the real phone: 5.1.2 failed to
    connect, 4.0.3 connected.
    """

    def test_four_takes_no_mode_positional(self, tmp_path: Path):
        """4.x has no mode at all — it dumps the full bundle by default, which
        is what 5.x calls `all`. Passing "all" would be read as a second
        bundle id."""
        cmd = decrypt_runner.build_command("com.x", tmp_path, udid="U", major=4)
        assert cmd == ["bagbak", "-D", "U", "-o", str(tmp_path), "com.x"]

    def test_five_keeps_the_mode_positional(self, tmp_path: Path):
        cmd = decrypt_runner.build_command("com.x", tmp_path, udid="U", major=5)
        assert cmd[-2:] == ["com.x", "all"]

    def test_an_unknown_version_is_treated_as_the_newer_argv(self, tmp_path: Path):
        """A bagbak we can't identify is likelier to be newer than 4.x, and the
        5.x argv is the one that degrades legibly (an extra positional) rather
        than silently dumping the wrong thing."""
        cmd = decrypt_runner.build_command("com.x", tmp_path, udid="U", major=None)
        assert cmd[-1] == "all" or decrypt_runner.bagbak_major() == 4

    def test_four_gets_ssh_credentials(self, monkeypatch):
        """4.x pulls the bundle over SSH and reads SSH_USERNAME/SSH_PASSWORD,
        defaulting to **mobile**:alpine — a different user than frida-ios-dump
        defaults to, so leaving it unset silently tries the wrong account."""
        import graft_ipa.keychain as kc

        monkeypatch.delenv("GRAFT_SSH_PASS", raising=False)
        monkeypatch.delenv("GRAFT_SSH_USER", raising=False)
        monkeypatch.setattr(kc, "read_ssh_password", lambda udid: "stored-pw")
        env = decrypt_runner.bagbak_env("UDID-1", major=4)
        assert env == {"SSH_USERNAME": "root", "SSH_PASSWORD": "stored-pw"}

    def test_five_gets_no_ssh_credentials(self, monkeypatch):
        """5.x dumps over Frida's own transport and ignores them; setting them
        would imply a dependency it doesn't have."""
        assert decrypt_runner.bagbak_env("UDID-1", major=5) == {}

    def test_the_password_is_shared_with_the_other_backend(self, monkeypatch):
        """One Keychain entry per device, not one per backend — a phone whose
        root password was changed is set up once."""
        import graft_ipa.keychain as kc

        monkeypatch.delenv("GRAFT_SSH_PASS", raising=False)
        seen = []
        monkeypatch.setattr(kc, "read_ssh_password", lambda udid: seen.append(udid) or "pw")
        decrypt_runner.bagbak_env("UDID-A", major=4)
        assert seen == ["UDID-A"]


class TestBagbakFailureDiagnosis:
    def _fail_with(self, monkeypatch, tmp_path, output):
        monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/bin/bagbak")
        monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 5)

        monkeypatch.setattr(
            decrypt_runner, "_run_streaming", lambda *a, **k: (1, output)
        )
        with pytest.raises(DecryptError) as e:
            decrypt_runner.decrypt_app("com.x", tmp_path / "o", udid="U")
        return str(e.value) + " " + str(e.value.hint or "")

    def test_a_frida_major_mismatch_names_the_real_fix(self, monkeypatch, tmp_path: Path):
        """The failure that made bagbak look permanently broken. "open Dopamine"
        is wrong — the jailbreak is running fine, the majors don't match."""
        msg = self._fail_with(
            monkeypatch, tmp_path,
            "[Error: Unable to communicate with remote frida-server; please ensure "
            "that major versions match and that the remote Frida has the feature]",
        )
        assert "bagbak@4.0.3" in msg
        assert "Dopamine" not in msg

    def test_a_bare_timeout_points_at_the_locked_phone(self, monkeypatch, tmp_path: Path):
        """bagbak swallows the FBS "device was not unlocked" error and reports
        only a timeout, so _LOCKED_RE never sees it. Confirmed by spawning the
        same app through frida directly, which says so outright."""
        msg = self._fail_with(monkeypatch, tmp_path, "[Error: Timeout was reached]")
        assert "locked" in msg and "Unlock" in msg

    def test_a_timeout_after_the_bundle_downloaded_is_not_a_locked_phone(
        self, monkeypatch, tmp_path: Path
    ):
        """A timeout arriving *after* the bundle downloaded cannot be the
        locked phone the generic branch claims — a locked phone never gets
        that far.

        Measured 2026-08-18 on `com.example.game`: `--decrypt-check`
        printed `OK App on screen: Example Game` and `ready` immediately before,
        and bagbak downloaded the entire bundle before hanging. The payload
        below is bagbak's real output, trimmed.

        The assertions are negative as well as positive on purpose: a branch
        that merely *added* the extension sentence to the old locked-phone
        advice would still send the reader to unlock a phone that is not
        locked, so the case pins that the wrong advice is gone.

        And `assert "extension" in msg` alone is **not enough** — verified by
        mutation: the old hint ends "check the app's extensions against the
        device's version", so that assertion passes against the very message
        this test exists to replace. `"minimum iOS"` and the two negatives are
        what actually kill it.
        """
        msg = self._fail_with(
            monkeypatch, tmp_path,
            "[info] app bundle downloaded\n"
            "For timeout error, there is an edge case that some apps from appstore "
            "incorrectly\n set the minimum os version requirement. For example "
            "WidgetKitExtension of Example app.\n We are not able to dump such "
            "extensions, because they can not run on your device.\n"
            "[Error: Timeout was reached]",
        )
        # What is certain, and all this may claim: the download finished and
        # the dump then stalled.
        assert "after downloading the bundle" in msg
        # Both causes offered, neither asserted. An earlier version reported the
        # extension as fact; a second unrelated app then produced the identical
        # paragraph, proving it is boilerplate bagbak prints on every timeout.
        assert "extension" in msg and "locked or slept" in msg
        assert "not evidence" in msg
        # And the old advice — go unlock a phone the readiness check just found
        # unlocked — must be gone rather than merely joined.
        assert "most often this is a locked phone" not in msg

    def test_a_named_failure_still_beats_the_timeout_catch_all(self, monkeypatch, tmp_path: Path):
        """The timeout branch is last on purpose — a real extension failure that
        also mentions a timeout must keep its own, more specific advice."""
        msg = self._fail_with(
            monkeypatch, tmp_path,
            "Retry 2/3 for com.x.share-extension...\nUnable to find process with pid 9\n"
            "Timeout was reached",
        )
        assert "extension" in msg


class TestBagbakWorkingDirectory:
    """bagbak runs somewhere it is allowed to write.

    bagbak 4.x creates a `.bagbak` scratch directory in its *current working
    directory*. Inherited, that is the user's project from a shell — where it
    silently litters — and `/` from a Dock-launched GUI, which is not writable.
    The second one is fatal and GUI-only: every dump died on
    `ENOENT: mkdir '.bagbak'` before it reached the device, which is exactly
    the kind of failure that looks like a decrypt bug and isn't.
    """

    def _run_capturing_cwd(self, monkeypatch, tmp_path: Path):
        import graft_ipa.keychain as kc

        monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/bin/bagbak")
        monkeypatch.setattr(decrypt_runner, "bagbak_major", lambda: 4)
        # Stubbed so the suite never reads the developer's real login Keychain
        # (and so the shared-module patch below has one less caller to survive).
        monkeypatch.setattr(kc, "read_ssh_password", lambda udid: "pw")
        seen = {}

        def fake_run(cmd, *, cwd=None, env=None, on_line=None):
            seen["cwd"] = cwd
            assert cwd is not None, (
                "bagbak was spawned with no cwd, so it inherits the process's — "
                "which is the user's project from a shell and an unwritable `/` "
                "from a Dock-launched GUI"
            )
            # The caller globs the output dir; give it something to find.
            (Path(cwd) / "app.ipa").write_bytes(b"PK\x03\x04")
            return 0, ""

        monkeypatch.setattr(decrypt_runner, "_run_streaming", fake_run)
        out = tmp_path / "dump"
        decrypt_runner.decrypt_app("com.x", out, udid="U")
        assert "cwd" in seen, "bagbak was never invoked, so nothing was measured"
        return seen["cwd"], out

    def test_it_runs_inside_the_output_directory(self, monkeypatch, tmp_path: Path):
        cwd, out = self._run_capturing_cwd(monkeypatch, tmp_path)
        assert cwd == str(out)

    def test_the_working_directory_exists_before_it_runs(self, monkeypatch, tmp_path: Path):
        """A cwd that doesn't exist yet fails the spawn itself, which would
        trade one ENOENT for another."""
        cwd, _ = self._run_capturing_cwd(monkeypatch, tmp_path)
        assert Path(cwd).is_dir()

    def test_it_is_never_the_process_working_directory(self, monkeypatch, tmp_path: Path):
        """The regression. Inheriting the CWD is what put `.bagbak` in the
        user's repo and made the GUI fail outright."""
        cwd, _ = self._run_capturing_cwd(monkeypatch, tmp_path)
        assert cwd is not None
        assert Path(cwd) != Path.cwd()


class TestVerboseStreaming:
    """The backend's own output, as it arrives.

    Streamed rather than captured because a dump takes a minute or more and the
    only question while it runs is "progressing or stuck" — which
    `capture_output=True` can answer only after it has finished. bagbak prints
    per-file download progress, so a live view is exactly the difference
    between a hung dump and a slow one.
    """

    def test_lines_reach_the_caller_as_they_arrive(self, tmp_path: Path):
        """Against a real subprocess, not a stub: the point of this function is
        the plumbing to a live process, which a stub would not exercise."""
        seen: list[str] = []
        code, combined = decrypt_runner._run_streaming(
            ["/bin/sh", "-c", "echo one; echo two; echo three"],
            on_line=seen.append,
        )
        assert code == 0
        assert seen == ["one", "two", "three"]
        assert "two" in combined

    def test_stderr_is_interleaved_not_lost(self, tmp_path: Path):
        """Both tools narrate on stderr and report on stdout. Kept separate,
        the transcript reads out of order; dropped, the narration disappears."""
        seen: list[str] = []
        _code, _out = decrypt_runner._run_streaming(
            ["/bin/sh", "-c", "echo to-stdout; echo to-stderr 1>&2"],
            on_line=seen.append,
        )
        assert "to-stdout" in seen and "to-stderr" in seen

    def test_the_exit_code_is_reported(self, tmp_path: Path):
        code, _ = decrypt_runner._run_streaming(["/bin/sh", "-c", "exit 3"])
        assert code == 3

    def test_no_sink_is_fine(self, tmp_path: Path):
        """Quiet runs pass no `on_line`; the output is still collected for the
        failure diagnosis, which reads the tail of it."""
        code, combined = decrypt_runner._run_streaming(["/bin/sh", "-c", "echo hi"])
        assert code == 0 and "hi" in combined


def test_a_dump_tunnel_that_did_not_bind_is_caught(monkeypatch, tmp_path: Path):
    """A `time.sleep` cannot tell a bound tunnel from an iproxy that exited on
    EADDRINUSE — silently, its stderr being DEVNULL. dump.py then talked to
    whatever else held the port, and the run died advising the user to install
    an OpenSSH they already had."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")
    monkeypatch.setattr(decrypt_runner, "ensure_frontmost",
                        lambda *_a, **_k: decrypt_runner.LAUNCH_OK)

    class DeadTunnel:
        def __init__(self, *_a, **_k): pass
        def poll(self): return 1          # exited: the port was taken
        def terminate(self): pass
        def wait(self, timeout=None): return 1
        def kill(self): pass

    monkeypatch.setattr(decrypt_runner, "resolve_ssh_password", lambda _u: "alpine")
    monkeypatch.setattr(decrypt_runner.subprocess, "Popen", DeadTunnel)
    monkeypatch.setattr(decrypt_runner, "_wait_for_port", lambda _p, **_k: True)

    def must_not_run(*_a, **_k):
        raise AssertionError("dump.py must not run down a tunnel that is not ours")

    monkeypatch.setattr(decrypt_runner, "_run_streaming", must_not_run)
    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    assert "tunnel" in str(e.value).lower()


def test_the_dump_port_is_taken_after_the_app_is_on_screen(monkeypatch, tmp_path: Path):
    """`_free_port` only proves a port was free when it asked, and getting the
    app on screen can spend eighteen seconds — enough for its own uiopen tunnel,
    or another dump, to take the number this one was holding on paper."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")

    order: list[str] = []
    monkeypatch.setattr(decrypt_runner, "_free_port",
                        lambda: order.append("port") or 40000)

    def fake_ensure(*_a, **_k):
        order.append("launch")
        return decrypt_runner.LAUNCH_OK

    monkeypatch.setattr(decrypt_runner, "ensure_frontmost", fake_ensure)
    monkeypatch.setattr(decrypt_runner, "resolve_ssh_password", lambda _u: "alpine")
    monkeypatch.setattr(decrypt_runner.subprocess, "Popen",
                        lambda *_a, **_k: _StubTunnel())
    monkeypatch.setattr(decrypt_runner, "_wait_for_port", lambda _p, **_k: True)
    monkeypatch.setattr(decrypt_runner, "_run_streaming", lambda *_a, **_k: (1, "boom"))
    with pytest.raises(DecryptError):
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    assert order == ["launch", "port"], f"port reserved before the launch: {order}"


class _StubTunnel:
    def poll(self): return None
    def terminate(self): pass
    def wait(self, timeout=None): return 0
    def kill(self): pass


@pytest.mark.parametrize(
    "outcome, expected",
    [
        # Seen dead through *both* launchers: nothing else can help.
        (decrypt_runner.LAUNCH_DIED, decrypt_runner.AppWillNotRunError),
        # Dead under frida with the second launcher unreachable — not proof the
        # app refuses to run, and bagbak launches over frida's transport.
        (decrypt_runner.LAUNCH_DIED_ONCE, decrypt_runner.DecryptError),
        # Running, just not on screen: the other backend may yet get it up.
        (decrypt_runner.LAUNCH_SLOW, decrypt_runner.DecryptError),
        # A locked phone stops every backend equally.
        (decrypt_runner.LAUNCH_REFUSED, decrypt_runner.AppWillNotRunError),
    ],
)
def test_only_a_proven_refusal_to_run_ends_the_ladder(
    monkeypatch, tmp_path: Path, outcome, expected
):
    """The exception *class* is what `_next_rung` reads, so it decides whether
    another minute is spent on the other backend. Getting it wrong is invisible
    in the message and expensive in the run."""
    py = tmp_path / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text("x")
    (tmp_path / "dump.py").write_text("x")
    monkeypatch.setattr(decrypt_runner, "FRIDA_DUMP_HOME", tmp_path)
    monkeypatch.setattr(decrypt_runner.shutil, "which", lambda _: "/usr/bin/iproxy")
    monkeypatch.setattr(decrypt_runner, "resolve_ssh_password", lambda _u: "alpine")
    monkeypatch.setattr(decrypt_runner, "ensure_frontmost", lambda *_a, **_k: outcome)

    with pytest.raises(DecryptError) as e:
        decrypt_runner.decrypt_app(
            "com.x", tmp_path / "out", udid="U", backend=decrypt_runner.FRIDA
        )
    assert type(e.value) is expected, (
        f"{outcome} raised {type(e.value).__name__}; "
        f"{'the ladder will stop' if expected is decrypt_runner.DecryptError else 'the ladder will keep going'} "
        "when it should not"
    )


class TestFridaDumpDeviceGuard:
    """`dump.py` takes frida's first USB device and cannot be pointed anywhere.

    So the guard's job is narrow: refuse only when the phone we were asked for
    is present but *not* first. The two silent cases matter as much as the loud
    one — an earlier version refused whenever the udid was not first, including
    for udids that are not devices at all, and failed twelve unit tests that
    never touch a phone.
    """

    class _Dev:
        def __init__(self, id, name, type="usb"):
            self.id, self.name, self.type = id, name, type

    def _with_devices(self, monkeypatch, devices):
        fake = types.SimpleNamespace(enumerate_devices=lambda: devices)
        monkeypatch.setattr(decrypt_runner, "_import_frida", lambda _reason: fake)

    def test_it_refuses_when_the_asked_for_phone_is_not_frida_s_first(self, monkeypatch):
        self._with_devices(monkeypatch, [
            self._Dev("IPAD-UDID", "iPad"), self._Dev("IPHONE-UDID", "iPhone"),
        ])
        with pytest.raises(decrypt_runner.DecryptError) as e:
            decrypt_runner._refuse_if_frida_dump_would_target_another_device("IPHONE-UDID")
        # Names what it *would* dump, which is the fact the user needs; saying
        # only "wrong device" leaves them to guess which one is in the way.
        assert "iPad" in str(e.value)
        assert "IPAD-UDID" in str(e.value)

    def test_it_is_silent_when_the_asked_for_phone_is_already_first(self, monkeypatch):
        self._with_devices(monkeypatch, [
            self._Dev("IPHONE-UDID", "iPhone"), self._Dev("IPAD-UDID", "iPad"),
        ])
        decrypt_runner._refuse_if_frida_dump_would_target_another_device("IPHONE-UDID")

    def test_an_unknown_udid_is_left_alone(self, monkeypatch):
        """A network device, or a fixture. Not this guard's business — and
        refusing here is exactly what broke the unit suite before."""
        self._with_devices(monkeypatch, [self._Dev("IPAD-UDID", "iPad")])
        decrypt_runner._refuse_if_frida_dump_would_target_another_device("SOME-OTHER-UDID")

    def test_a_lone_phone_never_trips_it(self, monkeypatch):
        self._with_devices(monkeypatch, [self._Dev("IPHONE-UDID", "iPhone")])
        decrypt_runner._refuse_if_frida_dump_would_target_another_device("IPHONE-UDID")
