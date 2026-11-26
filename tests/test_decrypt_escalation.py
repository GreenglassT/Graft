"""The ladder that turns "dump this app" into one command.

Every rung costs a real dump — a minute or more on the phone — so the tests
that matter here are about *which* rung comes next, not about dumping.
"""

from __future__ import annotations

import pathlib
import tempfile
from types import SimpleNamespace

import pytest

from graft_ipa import cli, decrypt_runner, ipa_inspect

#: Enough Config for the ladder: the fallback device, and somewhere to put the
#: lock that keeps two dumps off one phone.
CFG = SimpleNamespace(
    default_device_udid=None,
    cache_dir=pathlib.Path(tempfile.mkdtemp(prefix="tb-ladder-tests-")),
)


def still(extensions_only: bool, n: int = 1) -> decrypt_runner.StillEncryptedError:
    return decrypt_runner.StillEncryptedError(
        "still encrypted",
        paths=[f"Payload/X.app/PlugIns/E{i}.appex/E{i}" for i in range(n)],
        extensions_only=extensions_only,
    )


def rung(error, backend, tried=(), *, pinned=False):
    return cli._next_rung(error, backend, set(tried), pinned=pinned)


@pytest.fixture(autouse=True)
def no_real_phone(monkeypatch):
    """Keep the readiness probe off real hardware.

    With no backend pinned, the ladder asks `decrypt_ready.check` which tool to
    start with, and that asks frida on the actual USB device:
    `get_usb_device(timeout=10)` cost ten seconds a test with nothing plugged
    in (100 of the suite's ~125), and with a phone visible it went on to
    `enumerate_processes()`, which has no timeout and stalled whole runs.
    Answering as the probe does when it cannot reach a device — by failing —
    is the branch these tests were in fact exercising; the classes that test
    the probe's answer patch `check` themselves, over this.
    """
    from graft_ipa import decrypt_ready

    def unreachable(*_a, **_k):
        raise decrypt_runner.DecryptError("tests do not talk to a phone")

    monkeypatch.setattr(decrypt_ready, "check", unreachable)


@pytest.fixture(autouse=True)
def both_backends(monkeypatch):
    monkeypatch.setattr(
        decrypt_runner, "available_backends",
        lambda: [decrypt_runner.FRIDA, decrypt_runner.BAGBAK],
    )


class TestWhichRungComesNext:

    def test_extensions_left_encrypted_end_the_climb(self):
        """Not because nothing is done about them — the dump that found them
        already dropped them from the tree it had extracted and staged the rest.
        Asking for another attempt cost a whole extra dump: Alpha took 1m35
        re-dumping to strip, and 50 seconds once it stopped."""
        assert rung(still(extensions_only=True, n=5), decrypt_runner.BAGBAK) is None

    def test_an_encrypted_main_binary_switches_tools(self):
        """Not a structural limit — the dump came back incomplete, and the
        other tool has a real chance."""
        nxt = rung(still(extensions_only=False), decrypt_runner.FRIDA)
        assert nxt is not None
        backend, why = nxt
        assert backend == decrypt_runner.BAGBAK
        assert "main binary" in why

    def test_a_failed_dump_switches_tools(self):
        """A timeout says nothing about the app, only about the tool that hit
        it, so the other one is worth exactly one attempt."""
        nxt = rung(decrypt_runner.DecryptError("timed out"), decrypt_runner.BAGBAK)
        assert nxt is not None and nxt[0] == decrypt_runner.FRIDA

    def test_the_other_tool_is_not_tried_twice(self):
        assert rung(
            decrypt_runner.DecryptError("timed out"), decrypt_runner.BAGBAK,
            tried=(decrypt_runner.BAGBAK, decrypt_runner.FRIDA),
        ) is None

    def test_a_named_backend_is_not_second_guessed(self):
        """`--decrypt-backend frida-ios-dump` is an answer, not a hint. Quietly
        dumping with the other tool would produce an IPA the user did not ask
        for and could not explain."""
        assert rung(still(extensions_only=False), decrypt_runner.FRIDA, pinned=True) is None
        assert rung(decrypt_runner.DecryptError("boom"), decrypt_runner.FRIDA, pinned=True) is None

    def test_an_uninstalled_backend_is_never_proposed(self, monkeypatch):
        monkeypatch.setattr(
            decrypt_runner, "available_backends", lambda: [decrypt_runner.BAGBAK]
        )
        assert rung(decrypt_runner.DecryptError("timed out"), decrypt_runner.BAGBAK) is None


class TestWhichToolOpens:
    """One ranking, held by the readiness probe.

    Three copies of this rule existed, and they disagreed where it hurt: with
    the phone's sshd stopped the probe ruled out frida-ios-dump and the pane
    labeled every row "will be launched and dumped with bagbak", while the CLI
    ranked merely-*installed* backends and ran frida-ios-dump anyway — twenty
    seconds to fail with advice about installing an OpenSSH the phone had.
    """

    @pytest.fixture
    def probe(self, monkeypatch):
        from graft_ipa import decrypt_ready

        answer = SimpleNamespace(recommended_backend=decrypt_runner.FRIDA)
        monkeypatch.setattr(decrypt_ready, "check", lambda _udid=None, **_k: answer)
        return answer

    def test_the_probes_answer_is_the_answer(self, probe):
        assert cli._decrypt_backend_for("com.x", None) == decrypt_runner.FRIDA
        probe.recommended_backend = decrypt_runner.BAGBAK
        assert cli._decrypt_backend_for("com.x", None) == decrypt_runner.BAGBAK, (
            "a backend the probe ruled out must not be the one that runs"
        )

    def test_a_probe_with_no_usable_backend_falls_back_to_what_is_installed(
        self, probe, monkeypatch
    ):
        """The dump's own error explains a dead device better than a refusal
        here would."""
        probe.recommended_backend = None
        monkeypatch.setattr(
            decrypt_runner, "available_backends", lambda: [decrypt_runner.BAGBAK]
        )
        assert cli._decrypt_backend_for("com.x", None) == decrypt_runner.BAGBAK

    def test_a_probe_that_cannot_run_does_not_stop_the_dump(self, monkeypatch):
        from graft_ipa import decrypt_ready

        def boom(*_a, **_k):
            raise RuntimeError("frida-server is not reachable")

        monkeypatch.setattr(decrypt_ready, "check", boom)
        monkeypatch.setattr(
            decrypt_runner, "available_backends", lambda: [decrypt_runner.BAGBAK]
        )
        assert cli._decrypt_backend_for("com.x", None) == decrypt_runner.BAGBAK

    def test_with_nothing_anywhere_it_still_names_one(self, monkeypatch):
        """So the error is the honest "install a backend" rather than silence."""
        from graft_ipa import decrypt_ready

        monkeypatch.setattr(
            decrypt_ready, "check",
            lambda *_a, **_k: SimpleNamespace(recommended_backend=None),
        )
        monkeypatch.setattr(decrypt_runner, "available_backends", list)
        assert cli._decrypt_backend_for("com.x", None) == decrypt_runner.FRIDA


class TestTheWholeLadder:
    """`_decrypt_escalating` drives `_do_decrypt`; here that is a stub whose
    failures are scripted, so a three-rung climb costs no phone time."""

    @pytest.fixture
    def attempts(self, monkeypatch, tmp_path):
        seen: list[tuple[str, bool]] = []
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: None)
        return seen

    def _drive(self, monkeypatch, seen, outcomes):
        def fake(cfg, bundle_id, **kw):
            seen.append((kw["backend"], kw["strip_encrypted_extensions"]))
            outcome = outcomes[len(seen) - 1]
            if outcome is not None:
                raise outcome

        monkeypatch.setattr(cli, "_do_decrypt", fake)

    def test_the_common_path_is_one_call(self, monkeypatch, attempts):
        self._drive(monkeypatch, attempts, [None])
        cli._decrypt_escalating(
            CFG, "com.x", device=None, backend=None, strip=None,
            verbose=False, emitter=None,
        )
        assert attempts == [(decrypt_runner.FRIDA, True)]

    def test_a_wrong_tool_is_swapped_without_asking(self, monkeypatch, attempts, capsys):
        """The case this exists for: attach came back with the main binary
        encrypted, so the tool that launches the app itself gets a turn — and
        the user is told why rather than watching a second dump start for no
        stated reason."""
        self._drive(monkeypatch, attempts, [still(extensions_only=False), None])
        cli._decrypt_escalating(
            CFG, "com.x", device=None, backend=None, strip=None,
            verbose=False, emitter=None,
        )
        assert attempts == [
            (decrypt_runner.FRIDA, True),
            (decrypt_runner.BAGBAK, True),
        ]
        assert "main binary" in capsys.readouterr().out

    def test_undumpable_extensions_cost_only_one_dump(self, monkeypatch, attempts):
        """The dump strips from the tree it already has, so this never becomes
        a second minute on the phone."""
        self._drive(monkeypatch, attempts, [still(extensions_only=True, n=5)])
        with pytest.raises(decrypt_runner.StillEncryptedError):
            cli._decrypt_escalating(
                CFG, "com.x", device=None, backend=None, strip=None,
                verbose=False, emitter=None,
            )
        assert len(attempts) == 1

    def test_it_stops_rather_than_looping(self, monkeypatch, attempts):
        self._drive(monkeypatch, attempts, [
            still(extensions_only=False),
            still(extensions_only=False),
        ])
        with pytest.raises(decrypt_runner.DecryptError):
            cli._decrypt_escalating(
                CFG, "com.x", device=None, backend=None, strip=None,
                verbose=False, emitter=None,
            )
        assert len(attempts) <= 2

    def test_asking_to_strip_is_distinguishable_from_defaulting_to_it(
        self, monkeypatch, attempts
    ):
        """The tri-state's whole point, and it was untested: both states drop
        cosmetic extensions, but only an explicit ask may drop one the app *is*.

        Without this, mutating `strip_extensions_forced=strip is True` to
        `strip is not False` left every suite green while making the default
        dump force-strip essentials — so `--decrypt com.example.blocker` staged a
        0.1 MB IPA reported as decrypted instead of refusing.
        """
        forced: list[bool] = []

        def fake(cfg, bundle_id, **kw):
            forced.append(kw["strip_extensions_forced"])

        monkeypatch.setattr(cli, "_do_decrypt", fake)
        for policy in (None, True, False):
            cli._decrypt_escalating(
                CFG, "com.x", device=None, backend=None, strip=policy,
                verbose=False, emitter=None,
            )
        assert forced == [False, True, False], (
            "only a user who typed --strip-encrypted-extensions may drop an "
            "extension the app cannot work without"
        )

    def test_forbidding_the_strip_is_honoured(self, monkeypatch, attempts):
        """For the user who would rather have nothing than a partial app."""
        self._drive(monkeypatch, attempts, [None])
        cli._decrypt_escalating(
            CFG, "com.x", device=None, backend=None, strip=False,
            verbose=False, emitter=None,
        )
        assert attempts == [(decrypt_runner.FRIDA, False)]

    def test_the_original_failure_is_what_surfaces(self, monkeypatch, attempts):
        """When the ladder runs out, the user needs the error that describes
        the app — not a generic "gave up" that throws away the hint."""
        self._drive(monkeypatch, attempts, [still(extensions_only=True), None])
        with pytest.raises(decrypt_runner.StillEncryptedError):
            cli._decrypt_escalating(
                CFG, "com.x", device=None, backend=None, strip=False,
                verbose=False, emitter=None,
            )
        assert len(attempts) == 1


class TestLaunchingTheAppToDumpIt:
    """`ensure_frontmost` is what turns two actions into one — the user no
    longer has to pick up the phone and open the app first. It answers with a
    reason rather than a boolean, because the ways a launch fails want opposite
    advice."""

    def test_an_app_already_on_screen_is_left_alone(self, monkeypatch):
        """Respawning would kill and relaunch it, throwing away whatever state
        made the user open it."""
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: "com.x")

        def explode(*_a, **_k):
            raise AssertionError("must not relaunch an app that is already on screen")

        monkeypatch.setattr(decrypt_runner, "_import_frida", explode)
        assert decrypt_runner.ensure_frontmost("com.x", None) == decrypt_runner.LAUNCH_OK

    def test_a_backgrounded_app_is_launched_and_waited_for(self, monkeypatch):
        """iOS freezes a backgrounded app, so it keeps its pid and passes a
        "is it running" check while the attach times out. Launching it is the
        fix; confirming it arrived is what makes the dump reliable."""
        answers = iter([None, None, "com.x"])
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: next(answers))
        spawned = []
        monkeypatch.setattr(
            decrypt_runner, "_import_frida", lambda _need: _FakeFrida(spawned)
        )
        monkeypatch.setattr(decrypt_runner, "_frida_device", lambda f, u: f.device)
        monkeypatch.setattr(decrypt_runner, "_pid_alive", lambda *_a: True)
        monkeypatch.setattr(decrypt_runner.time, "sleep", lambda _s: None)
        assert decrypt_runner.ensure_frontmost("com.x", None) == decrypt_runner.LAUNCH_OK
        assert spawned == [["com.x"]], "launched exactly once"

    def test_a_refused_launch_is_reported_not_raised(self, monkeypatch):
        """iOS will not launch anything while locked. The caller's error names
        that better than this can, so it answers with a reason."""
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: None)

        class Refuses:
            def spawn(self, _):
                raise RuntimeError("unable to launch: device is locked")

        monkeypatch.setattr(decrypt_runner, "_import_frida", lambda _need: object())
        monkeypatch.setattr(decrypt_runner, "_frida_device", lambda f, u: Refuses())
        assert decrypt_runner.ensure_frontmost("com.x", None) == decrypt_runner.LAUNCH_REFUSED

    def test_an_app_that_kills_itself_is_retried_through_the_device_launcher(self, monkeypatch):
        """Ticketmaster exits within a second of a frida spawn. Some apps only
        inspect their process for a debugger *as they boot*, and `uiopen` gives
        them a boot with no frida in it — so the death is worth one clean retry
        before it becomes a verdict."""
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: None)
        monkeypatch.setattr(
            decrypt_runner, "_import_frida", lambda _need: _FakeFrida([])
        )
        monkeypatch.setattr(decrypt_runner, "_frida_device", lambda f, u: f.device)
        monkeypatch.setattr(decrypt_runner, "_pid_alive", lambda *_a: False)
        monkeypatch.setattr(decrypt_runner.time, "sleep", lambda _s: None)
        tried = []
        monkeypatch.setattr(
            decrypt_runner, "_launch_via_uiopen",
            lambda b, u: tried.append(b) or False,
        )
        assert decrypt_runner.ensure_frontmost("com.x", None) == decrypt_runner.LAUNCH_DIED_ONCE
        assert tried == ["com.x"], "the second launcher must actually be tried"

    def test_an_app_alive_but_never_on_screen_is_not_called_dead(self, monkeypatch):
        """A slow start and a refusal to run want opposite advice."""
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: None)
        monkeypatch.setattr(
            decrypt_runner, "_import_frida", lambda _need: _FakeFrida([])
        )
        monkeypatch.setattr(decrypt_runner, "_frida_device", lambda f, u: f.device)
        monkeypatch.setattr(decrypt_runner, "_pid_alive", lambda *_a: True)
        monkeypatch.setattr(decrypt_runner.time, "sleep", lambda _s: None)
        clock = iter(range(0, 200))
        monkeypatch.setattr(decrypt_runner.time, "time", lambda: next(clock))
        assert decrypt_runner.ensure_frontmost("com.x", None) == decrypt_runner.LAUNCH_SLOW



class _FakeFrida:
    def __init__(self, spawned):
        self.device = _FakeDevice(spawned)


class _FakeDevice:
    def __init__(self, spawned):
        self._spawned = spawned

    def spawn(self, argv):
        self._spawned.append(argv)
        return 4242

    def resume(self, pid):
        assert pid == 4242

class TestAnAppThatWillNotRun:
    """Ticketmaster opens and quits within a second on this jailbroken phone.

    Nothing can dump an app that will not stay running, so the ladder has to
    stop rather than launch the crashing app again with the other tool — which
    it did, in front of the user, for another minute.
    """

    def test_the_ladder_stops(self):
        assert rung(
            decrypt_runner.AppWillNotRunError("com.x started and then exited"),
            decrypt_runner.FRIDA,
        ) is None

    def test_an_ordinary_failure_still_escalates(self):
        """The distinction has to earn its keep: a dump that failed for any
        other reason is still worth the other backend."""
        nxt = rung(decrypt_runner.DecryptError("timed out"), decrypt_runner.FRIDA)
        assert nxt is not None and nxt[0] == decrypt_runner.BAGBAK

    def test_a_slow_app_is_not_treated_as_a_dead_one(self):
        """It is running; the other backend may still get it on screen."""
        nxt = rung(
            decrypt_runner.DecryptError("com.x launched but never reached the foreground"),
            decrypt_runner.FRIDA,
        )
        assert nxt is not None

    def test_only_one_dump_is_spent_on_it(self, monkeypatch, tmp_path):
        attempts = []

        def fake(cfg, bundle_id, **kw):
            attempts.append(kw["backend"])
            raise decrypt_runner.AppWillNotRunError("com.x started and then exited")

        monkeypatch.setattr(cli, "_do_decrypt", fake)
        with pytest.raises(decrypt_runner.AppWillNotRunError):
            cli._decrypt_escalating(
                CFG, "com.x", device=None, backend=None, strip=None,
                verbose=False, emitter=None,
            )
        assert len(attempts) == 1

class TestOneDumpPerDevice:
    """A dump puts its app on the screen, and a phone has one screen.

    Tested for real before it was written: two overlapping dumps left the first
    hung waiting for an attach that could no longer work — the second app had
    taken the foreground — and the second fell through to bagbak and timed out.
    Two failures and three minutes from two commands that each looked fine.
    """

    def test_a_second_dump_is_refused_while_one_holds_the_device(self, tmp_path):
        lock = tmp_path / "locks" / "UDID.lock"
        with decrypt_runner.device_dump_lock(lock, "UDID"):
            with pytest.raises(decrypt_runner.DumpInProgressError) as e:
                with decrypt_runner.device_dump_lock(lock, "UDID"):
                    pass
        assert "already running" in str(e.value)

    def test_the_device_is_released_afterwards(self, tmp_path):
        lock = tmp_path / "locks" / "UDID.lock"
        with decrypt_runner.device_dump_lock(lock, "UDID"):
            pass
        with decrypt_runner.device_dump_lock(lock, "UDID"):
            pass  # no exception: the first one let go

    def test_a_failed_dump_does_not_keep_the_device(self, tmp_path):
        """Otherwise one crash means nothing dumps until someone finds a file."""
        lock = tmp_path / "locks" / "UDID.lock"
        with pytest.raises(RuntimeError):
            with decrypt_runner.device_dump_lock(lock, "UDID"):
                raise RuntimeError("dump blew up")
        with decrypt_runner.device_dump_lock(lock, "UDID"):
            pass

    def test_two_devices_do_not_block_each_other(self, tmp_path):
        """The constraint is one screen per phone, not one dump per machine."""
        with decrypt_runner.device_dump_lock(tmp_path / "a.lock", "A"):
            with decrypt_runner.device_dump_lock(tmp_path / "b.lock", "B"):
                pass

    def test_the_refusal_names_the_device(self, tmp_path):
        lock = tmp_path / "UDID-13.lock"
        with decrypt_runner.device_dump_lock(lock, "UDID-13"):
            with pytest.raises(decrypt_runner.DumpInProgressError) as e:
                with decrypt_runner.device_dump_lock(lock, "UDID-13"):
                    pass
        assert "UDID-13" in str(e.value)

class TestAnUnreachableSecondLauncher:
    """`uiopen` failing to *run* is not the app dying again.

    Reporting it as a second death made the error claim the app "quit twice,
    once from the device's own launcher" about an attempt that never happened —
    and ended the ladder on that word, so bagbak, which launches over frida's
    transport and needs no device SSH, was never tried.
    """

    def _dies_under_frida(self, monkeypatch):
        monkeypatch.setattr(decrypt_runner, "frontmost_app", lambda _: None)
        monkeypatch.setattr(decrypt_runner, "_import_frida", lambda _n: _FakeFrida([]))
        monkeypatch.setattr(decrypt_runner, "_frida_device", lambda f, u: f.device)
        monkeypatch.setattr(decrypt_runner, "_pid_alive", lambda *_a: False)
        monkeypatch.setattr(decrypt_runner.time, "sleep", lambda _s: None)

    def test_an_unreachable_launcher_is_its_own_outcome(self, monkeypatch):
        self._dies_under_frida(monkeypatch)
        monkeypatch.setattr(decrypt_runner, "_launch_via_uiopen", lambda *_a: False)
        assert decrypt_runner.ensure_frontmost("com.x", None) == decrypt_runner.LAUNCH_DIED_ONCE

    def test_it_does_not_end_the_ladder(self):
        """One death under one launcher has not shown the app refuses to run."""
        _, hint = decrypt_runner._launch_failure("com.x", decrypt_runner.LAUNCH_DIED_ONCE)
        assert "could not be reached" in hint
        assert "twice" not in hint, "the second launch never happened"

    def test_a_death_seen_through_both_launchers_still_ends_it(self):
        _, hint = decrypt_runner._launch_failure("com.x", decrypt_runner.LAUNCH_DIED)
        assert "twice" in hint


class TestTheFallbackLauncherCredentials:
    """The same credentials the dump uses, in the same order."""

    def _capture(self, monkeypatch):
        seen = {}

        class Client:
            def set_missing_host_key_policy(self, _p): pass
            def connect(self, *_a, **kw): seen.update(kw)
            def exec_command(self, *_a, **_k):
                class S:
                    def read(self): return b""
                return (None, S(), None)
            def close(self): pass

        monkeypatch.setattr(
            decrypt_runner, "_import_paramiko",
            lambda: SimpleNamespace(SSHClient=Client, AutoAddPolicy=object),
        )
        monkeypatch.setattr(decrypt_runner, "_with_tunnel",
                            lambda _u, work: work(2222))
        return seen

    def test_a_key_is_used_when_one_is_configured(self, monkeypatch):
        """`resolve_ssh_password` falls back to "alpine" rather than returning
        empty, so a key user was silently sending the wrong password — and the
        caller then reported the app as having died a second time."""
        monkeypatch.setenv("GRAFT_SSH_KEY", "/home/me/.ssh/id_ed25519")
        seen = self._capture(monkeypatch)
        assert decrypt_runner._launch_via_uiopen("com.x", "U") is True
        assert seen["key_filename"] == "/home/me/.ssh/id_ed25519"
        assert seen["password"] is None

    def test_a_password_is_used_when_no_key_is_configured(self, monkeypatch):
        monkeypatch.delenv("GRAFT_SSH_KEY", raising=False)
        monkeypatch.setattr(decrypt_runner, "resolve_ssh_password", lambda _u: "hunter2")
        seen = self._capture(monkeypatch)
        assert decrypt_runner._launch_via_uiopen("com.x", "U") is True
        assert seen["password"] == "hunter2" and seen["key_filename"] is None

class TestATransientDeviceBlip:
    """`frontmost_app` raises when frida is unreachable, and the launch wait
    polls it thirty times over a phone that was just told to spawn an app."""

    def test_one_unreachable_poll_does_not_abort_the_dump(self, monkeypatch):
        """It aborted with "open the Dopamine app to (re)start the jailbreak"
        about a jailbreak that was fine — and, being a plain DecryptError, the
        ladder then spent another minute re-dumping with bagbak."""
        answers = iter([
            decrypt_runner.DecryptError("couldn't reach frida-server"),
            None,
            "com.x",
        ])

        def flaky(_udid):
            nxt = next(answers)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt

        monkeypatch.setattr(decrypt_runner, "frontmost_app", flaky)
        monkeypatch.setattr(decrypt_runner, "_pid_alive", lambda *_a: True)
        monkeypatch.setattr(decrypt_runner.time, "sleep", lambda _s: None)
        assert decrypt_runner._wait_for_foreground("com.x", None, pid=1) == decrypt_runner.LAUNCH_OK


class TestKeyAuthAndBagbak:
    """A key authenticates frida-ios-dump, which takes `-K`. bagbak has none:
    `bagbak_env` sends SSH_PASSWORD and nothing else."""

    def _backends(self, monkeypatch, *, key_only):
        from graft_ipa import decrypt_ready

        monkeypatch.setattr(decrypt_runner, "bagbak_available", lambda: "/usr/bin/bagbak")
        monkeypatch.setattr(decrypt_runner, "frida_ios_dump_available", lambda: "/x/dump.py")
        monkeypatch.setattr(decrypt_runner, "mode_is_supported", lambda *_a: False)
        monkeypatch.setattr(decrypt_ready, "_bagbak_frida_version", lambda: "16.6.6")
        return {
            b.name: b for b in decrypt_ready._backends(
                True, True, "16.6.6", key_only=key_only
            )
        }

    def test_bagbak_is_not_offered_to_a_key_only_device(self, monkeypatch):
        """Reporting it usable greenlit the Decrypt button for a dump that dies
        on an ssh2 auth error a minute later — the exact failure the guard
        beside it exists to pre-empt."""
        bagbak = self._backends(monkeypatch, key_only=True)[decrypt_runner.BAGBAK]
        assert not bagbak.usable
        assert "key auth" in (bagbak.reason or "")

    def test_a_password_device_still_gets_bagbak(self, monkeypatch):
        assert self._backends(monkeypatch, key_only=False)[decrypt_runner.BAGBAK].usable

class TestTheEssentialExtensionRefusal:
    """`_do_decrypt` itself, driven with a fake backend, so the headline guard
    of the sweep is exercised end to end.

    `extension_importance` was unit-tested in isolation and nothing asserted
    that a dump *acts* on it — the gap that let a 0.1 MB content blocker be staged and
    called decrypted.
    """

    @pytest.fixture
    def dumped(self, monkeypatch, tmp_path):
        """A backend that produces an IPA whose extension is left encrypted."""
        def fake_dump(bundle_id, out_dir, **_kw):
            root = pathlib.Path(out_dir) / "extracted"
            app = root / "Payload/X.app"
            (app / "PlugIns/Tunnel.appex").mkdir(parents=True)
            (app / "X").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
            (app / "PlugIns/Tunnel.appex/Tunnel").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
            import plistlib
            with (app / "Info.plist").open("wb") as fh:
                plistlib.dump({
                    "CFBundleIdentifier": bundle_id, "CFBundleExecutable": "X",
                    "CFBundleShortVersionString": "1.0",
                }, fh)
            with (app / "PlugIns/Tunnel.appex/Info.plist").open("wb") as fh:
                plistlib.dump({"NSExtension": {
                    "NSExtensionPointIdentifier": self.point
                }}, fh)
            ipa = pathlib.Path(out_dir) / "made.ipa"
            ipa_inspect.repack_ipa(root, ipa)
            return decrypt_runner.DecryptResult(ipa_path=ipa, bundle_id=bundle_id, stdout="")

        monkeypatch.setattr(decrypt_runner, "decrypt_app", fake_dump)

        # Only the extension comes back encrypted; the app's own binary is fine.
        # Real `otool` would say the same about a real dump — this just spares
        # the test a genuine FairPlay binary.
        def fake_verify(extract_dir):
            return [
                SimpleNamespace(
                    binary=b,
                    cryptids=[1] if ".appex/" in str(b) else [0],
                    is_decrypted=".appex/" not in str(b),
                )
                for b in ipa_inspect.list_macho_in_extract(extract_dir)
            ]

        monkeypatch.setattr(cli, "verify_decrypted", fake_verify)
        return SimpleNamespace(cache=tmp_path)

    point = "com.apple.networkextension.packet-tunnel"

    def test_an_essential_extension_refuses_rather_than_staging_a_shell(
        self, monkeypatch, dumped, tmp_path
    ):
        cfg = SimpleNamespace(default_device_udid=None, apps={}, cache_dir=tmp_path / "cache")
        with pytest.raises(decrypt_runner.StillEncryptedError) as e:
            cli._do_decrypt(
                cfg, "com.example.vpn", device="U",
                backend=decrypt_runner.FRIDA, strip_encrypted_extensions=True,
                strip_extensions_forced=False,
            )
        assert "losing what it is for" in str(e.value)
        assert not list((tmp_path / "cache").glob("*.ipa")), "a shell was staged anyway"

    def test_the_same_dump_succeeds_when_the_user_forces_it(
        self, monkeypatch, dumped, tmp_path
    ):
        cfg = SimpleNamespace(default_device_udid=None, apps={}, cache_dir=tmp_path / "cache2")
        cli._do_decrypt(
            cfg, "com.example.vpn", device="U",
            backend=decrypt_runner.FRIDA, strip_encrypted_extensions=True,
            strip_extensions_forced=True,
        )
        staged = list((tmp_path / "cache2").glob("*.ipa"))
        assert staged, "forcing must still work"
        note = ipa_inspect.stripped_note(staged[0])
        assert note is not None
        assert note.removed == ["Payload/X.app/PlugIns/Tunnel.appex"]
        assert note.importance == {"Payload/X.app/PlugIns/Tunnel.appex": "essential"}
        # `lost` names only the capabilities with a name worth saying, and a
        # packet tunnel isn't one of them — so this is `[]` even though what
        # was just removed is the whole reason the app exists. `lost == []`
        # is *not* "nothing important was lost"; it only ever means "nothing
        # that vanishes silently was among the casualties". Essential
        # extension points and the capability map are disjoint sets, so this
        # is the one path that makes that gap visible: an empty `lost` next
        # to an `importance` entry that says `"essential"`.
        assert note.lost == []

    def test_a_complete_re_dump_clears_the_partial_dump_note(
        self, monkeypatch, dumped, tmp_path
    ):
        """Staging replaces the IPA but not the note beside it, so a complete
        re-dump of the same version inherited the previous run's sidecar and
        every later build warned about extensions that were sitting right
        there — the one warning that exists to tell the truth about a partial
        app, lying, until someone deleted a file they were never told about.
        """
        cfg = SimpleNamespace(default_device_udid=None, apps={}, cache_dir=tmp_path / "cache3")
        cli._do_decrypt(
            cfg, "com.example.vpn", device="U",
            backend=decrypt_runner.FRIDA, strip_encrypted_extensions=True,
            strip_extensions_forced=True,
        )
        staged = next((tmp_path / "cache3").glob("*.ipa"))
        assert ipa_inspect.stripped_extensions(staged), "the partial dump left no note"

        # Now the same version dumps complete: nothing is left encrypted.
        monkeypatch.setattr(cli, "verify_decrypted", lambda d: [
            SimpleNamespace(binary=b, cryptids=[0], is_decrypted=True)
            for b in ipa_inspect.list_macho_in_extract(d)
        ])
        cli._do_decrypt(
            cfg, "com.example.vpn", device="U",
            backend=decrypt_runner.FRIDA, strip_encrypted_extensions=True,
            strip_extensions_forced=True,
        )
        assert ipa_inspect.stripped_extensions(staged) == [], (
            "a complete dump still carries the previous run's note"
        )


class TestWhatTheSidecarRecords:
    """A dump that strips must leave behind the words, not just the filenames.

    Same fake-backend harness as `TestTheEssentialExtensionRefusal`, with an
    extension point that is *major* rather than essential — so the strip
    happens unasked, the IPA stages, and there is a sidecar to read.
    """

    point = "com.apple.intents-service"

    @pytest.fixture
    def dumped(self, monkeypatch):
        def fake_dump(bundle_id, out_dir, **_kw):
            import plistlib
            root = pathlib.Path(out_dir) / "extracted"
            app = root / "Payload/X.app"
            (app / "PlugIns/Intents.appex").mkdir(parents=True)
            (app / "X").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 64)
            (app / "PlugIns/Intents.appex/Intents").write_bytes(
                b"\xcf\xfa\xed\xfe" + b"\x00" * 64
            )
            with (app / "Info.plist").open("wb") as fh:
                plistlib.dump({
                    "CFBundleIdentifier": bundle_id, "CFBundleExecutable": "X",
                    "CFBundleShortVersionString": "1.0",
                }, fh)
            with (app / "PlugIns/Intents.appex/Info.plist").open("wb") as fh:
                plistlib.dump({"NSExtension": {
                    "NSExtensionPointIdentifier": self.point
                }}, fh)
            ipa = pathlib.Path(out_dir) / "made.ipa"
            ipa_inspect.repack_ipa(root, ipa)
            return decrypt_runner.DecryptResult(
                ipa_path=ipa, bundle_id=bundle_id, stdout=""
            )

        monkeypatch.setattr(decrypt_runner, "decrypt_app", fake_dump)

        def fake_verify(extract_dir):
            return [
                SimpleNamespace(
                    binary=b,
                    cryptids=[1] if ".appex/" in str(b) else [0],
                    is_decrypted=".appex/" not in str(b),
                )
                for b in ipa_inspect.list_macho_in_extract(extract_dir)
            ]

        monkeypatch.setattr(cli, "verify_decrypted", fake_verify)

    def test_the_sidecar_names_the_capability_and_the_importance(
        self, dumped, tmp_path
    ):
        cfg = SimpleNamespace(default_device_udid=None, apps={}, cache_dir=tmp_path / "cache")
        cli._do_decrypt(
            cfg, "com.example.books", device="U",
            backend=decrypt_runner.FRIDA, strip_encrypted_extensions=True,
            strip_extensions_forced=False,
        )
        staged = list((tmp_path / "cache").glob("*.ipa"))
        assert len(staged) == 1, "the strip should have produced a buildable IPA"
        note = ipa_inspect.stripped_note(staged[0])
        assert note is not None
        assert note.removed == ["Payload/X.app/PlugIns/Intents.appex"]
        # The words, not the filename. This is the whole task.
        assert note.lost == ["Siri and Shortcuts"]
        assert note.importance == {"Payload/X.app/PlugIns/Intents.appex": "major"}
