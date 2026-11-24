"""Can this device dump an app right now, and if not, what is stopping it?

A dump has seven preconditions and every one of them used to be discovered the
same way: run it, wait, read "Timeout was reached". The device has to be
reachable, jailbroken, unlocked and awake; frida's client and server majors have
to match; the frida-ios-dump path needs OpenSSH and the right root password; and
the app has to be *foreground-active*, not merely running. Any one missing costs
around thirty seconds and tells you nothing.

This checks them up front, in about two seconds, and names the one that is in
the way. It is deliberately read-only — no spawning, no dumping — so it is safe
to run whenever a pane appears.

What it does **not** claim is worth stating. There is no cheap, honest way to
ask iOS "are you locked": frida reports no frontmost application both for a
locked phone and for one sitting on the home screen, and a passcode-set flag
answers a different question. So this reports *what is on screen*, which is the
thing that actually decides whether a dump can run, and the advice for an empty
answer covers both cases. Better one true line than two, one of which is a
guess.
"""

from __future__ import annotations

import re
import shlex
import shutil
import socket
from dataclasses import dataclass, field

from . import decrypt_runner


@dataclass
class Check:
    """One precondition, and what to do about it when it fails."""

    key: str
    label: str
    ok: bool
    detail: str
    #: What the user should do. None when `ok`, since there is nothing to fix.
    fix: str | None = None
    #: Whether failing this stops *every* dump, or only one route to one.
    #:
    #: "App on screen" is the reason this exists. Both backends can launch the
    #: app themselves — frida-ios-dump spawns when there is no pid, bagbak
    #: spawns unconditionally — so a foreground app is not required to dump at
    #: all. What it buys is quality: a spawned app's main binary is often still
    #: encrypted, because the kernel decrypts pages as they execute and a
    #: freshly launched app has not run enough. Rendering that as a hard
    #: failure told people the feature was broken when it was merely going to
    #: be less reliable.
    blocking: bool = True
    #: Which failure this is, as a stable word, for a check whose failures
    #: need different responses — today only `ssh` (the `SSH_*` values
    #: below). None when `ok`, and for every other check.
    reason: str | None = None


#: Why the `ssh` check failed, reported as its `reason`. The Mac app's decrypt
#: card collapses to "needs a jailbroken device" on `SSH_NO_SERVER` alone: the
#: other two are fixable from here on a phone that *is* jailbroken (install
#: libimobiledevice; store the right root password), and hiding the checks
#: hid the fix. Pinned against `CLIContract.DecryptCheck.SSHReason` by
#: tests/test_decrypt_check_keys.py.
SSH_NO_IPROXY = "no_iproxy"
SSH_NO_SERVER = "no_server"
SSH_LOGIN_REJECTED = "login_rejected"


@dataclass
class BackendStatus:
    name: str
    usable: bool
    #: Why not, in the user's terms. None when usable.
    reason: str | None = None
    #: Something true and worth knowing that is *not* a reason it cannot run.
    #:
    #: Separate from `reason` so a suspicion never gets promoted to a verdict.
    #: A minor-version skew between bagbak's frida and the device's is the
    #: current example: frida's documented rule is that *majors* must match, so
    #: refusing to offer the backend would be asserting more than is known —
    #: but when a dump hangs right after "spawned app pid", this is the first
    #: thing to look at, and the user cannot see it otherwise.
    note: str | None = None


@dataclass
class Readiness:
    device: str | None
    device_name: str | None
    checks: list[Check] = field(default_factory=list)
    backends: list[BackendStatus] = field(default_factory=list)
    #: The app on screen, or None. `(bundle_id, name)`.
    frontmost: tuple[str, str] | None = None

    @property
    def ready(self) -> bool:
        """Whether the *app on screen* can be dumped right now.

        This is the frontmost button's precondition specifically, not the
        feature's: see `can_dump_from_list`.
        """
        return self.can_dump_from_list and self.frontmost is not None

    @property
    def can_dump_from_list(self) -> bool:
        """Whether any app could be dumped, foreground or not.

        True whenever a backend can run, because both of them launch the app
        themselves. The frontmost route is the *reliable* one, not the only one.
        """
        return any(b.usable for b in self.backends)

    @property
    def recommended_backend(self) -> str | None:
        """The backend to use, preferring the one that gets the main binary.

        Deferred to `decrypt_runner.preferred_backend`, which is the same
        function the dump itself uses. This used to hold its own copy of the
        rule — and once the dump learned to launch the app, the copy went stale
        and the card started naming a backend the tool would not use.
        """
        return decrypt_runner.preferred_backend(
            [b.name for b in self.backends if b.usable]
        )

    @property
    def blockers(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


#: A `frida` failure that is about *this* machine — a missing module, a version
#: skew — rather than about the phone. Those must not be read as "no jailbreak":
#: the device may be perfectly healthy and simply unreachable from here.
_CLIENT_SIDE = re.compile(r"version|install|module|import", re.I)


def check(udid: str | None = None, device_name: str | None = None) -> Readiness:
    """Probe everything a dump needs, without starting one."""
    r = Readiness(device=udid, device_name=device_name)

    frida_ok, frida_detail, third = _check_frida(udid)
    # `third` is the server version on success and, on a *client-side* failure,
    # that error's own hint — so a broken install is never reported as a broken
    # jailbreak.
    server_version = third if frida_ok else None
    frida_fix = None if frida_ok else (
        third or "open the Dopamine app on the phone to (re)start the jailbreak"
    )

    ssh_ok, ssh_detail, ssh_fix, ssh_reason = _check_ssh(udid)
    # A key authenticates the dump, not bagbak: see `_check_ssh`.
    key_only = bool(decrypt_runner.os.environ.get("GRAFT_SSH_KEY"))
    # Nothing answered on port 22, which is what an *un-jailbroken* phone
    # looks like — and `_check_ssh` cannot tell, because on its own "no
    # banner" is equally a stopped OpenSSH. Alone it says "install it from
    # Sileo; if it is, respring", which on a stock phone is a fix for a problem
    # it does not have and cannot acquire. Measured 2026-08-17: that hint sent
    # a whole session chasing SSH on an iOS 26.1 device with no jailbreak
    # available for it. Only this function sees both answers, so the
    # correction lives here rather than in either probe.
    #
    # Only for that outcome. A missing iproxy is this Mac's problem and a
    # rejected login is a jailbroken phone with the wrong stored password;
    # both keep `_check_ssh`'s own fix, which is the one that works.
    if ssh_reason == SSH_NO_SERVER:
        ssh_fix = (
            "either this phone has no jailbreak — decryption needs one, so pass "
            "--device with a jailbroken phone — or OpenSSH has stopped on it: "
            "respring, or restart it from Sileo"
        )
        # Both possibilities, in that order, because nothing here can tell them
        # apart and the previous wording picked the wrong one by default.
        #
        # It cannot be settled by asking frida either, which an earlier version
        # of this branch tried. **frida reachability is not a jailbreak test on
        # iOS 17+.** Measured 2026-08-18 against two phones on this desk: the
        # jailbroken 13 Pro (16.3.1) enumerated 113 applications, and a *stock*
        # 15 Pro on 26.1 enumerated 105 — frida reaches the latter over
        # RemoteXPC with no frida-server involved at all. So `frida_ok` is true
        # on phones that can never dump, and gating this sentence on it left it
        # unreachable for exactly the modern devices that most need it.
        if not frida_ok and not _CLIENT_SIDE.search(frida_detail):
            frida_fix = ssh_fix

    r.checks.append(Check(
        key="frida",
        # Named for what it measures. It was "Jailbreak (frida-server)", which
        # it is not: see the note in `check` — frida answers on stock iOS 17+
        # devices too, so a green line here says the phone is reachable, not
        # that it can be dumped. SSH below is the line that gates every backend.
        label="frida reachable",
        ok=frida_ok,
        detail=frida_detail,
        fix=frida_fix,
    ))
    r.checks.append(Check(
        key="ssh", label="SSH login", ok=ssh_ok, detail=ssh_detail, fix=ssh_fix,
        reason=ssh_reason,
    ))

    front = _frontmost(udid) if frida_ok else None
    r.frontmost = front
    r.backends = _backends(frida_ok, ssh_ok, server_version, key_only=key_only,
                           frida_unchecked=not frida_ok and bool(_CLIENT_SIDE.search(frida_detail)))
    r.checks.append(Check(
        key="foreground",
        label="App on screen",
        ok=front is not None,
        detail=f"{front[1]} ({front[0]})" if front else "nothing on screen",
        # Not blocking while a backend is available: the dump launches the app
        # itself, so an empty screen costs nothing but the ability to say
        # "whatever is in front of me". Saying otherwise reads as "decryption is
        # broken" when the truth is "name the app instead".
        blocking=not any(b.usable for b in r.backends),
        fix=None if front else (
            "nothing to fix — a dump launches the app itself. This only names "
            "what `--decrypt-frontmost` would pick, and with an empty screen "
            "there is nothing to pick, so name the app you want"
            if r.can_dump_from_list else
            "unlock the phone and open the app you want to decrypt"
        ),
    ))
    return r


def _check_frida(udid: str | None) -> tuple[bool, str, str | None]:
    """Whether frida-server answers, and what version it is.

    Enumerating *processes* rather than applications on purpose: frida can list
    applications on a stock device over lockdown with no jailbreak at all, so
    that would pass on a phone that cannot dump anything.
    """
    try:
        frida = decrypt_runner._import_frida("check the jailbreak")
    except decrypt_runner.DecryptError as e:
        # A missing or skewed `frida` module is *our* problem, not the phone's.
        # Folded into the generic branch below it told the user to restart the
        # jailbreak on a perfectly healthy device, and threw away the one
        # actionable sentence — reinstall the CLI — that the error carried.
        # The message alone: the hint is the fix, shown on its own line.
        return False, e.message, e.hint
    try:
        device = decrypt_runner._frida_device(frida, udid)
        count = len(device.enumerate_processes())
    except decrypt_runner.FridaNotImportable as e:
        # This Mac's, like the missing module above: never "restart the
        # jailbreak" (the whole-diff review's cli-10).
        err = decrypt_runner.frida_not_importable(e, "check the jailbreak")
        return False, err.message, err.hint
    except Exception as e:
        text = str(e)
        if re.search(r"major versions match", text, re.I):
            # Name *our* version and say to change it here. This used to return
            # no hint, so the caller fell through to "open the Dopamine app to
            # (re)start the jailbreak" — advice for a phone that was entirely
            # healthy. Measured 2026-08-17: this checkout's venv had drifted to
            # frida 17.17.0 against its own `frida<17` pin while the device ran
            # 16.x, and restarting the jailbreak could not have fixed it.
            # Ours is frida-ios-dump's: Graft asks frida through its virtualenv.
            ours = getattr(frida, "__version__", "unknown")
            major = ours.split(".")[0] if ours[:1].isdigit() else "16"
            pip = shlex.quote(str(decrypt_runner.frida_ios_dump_pip()))
            return False, (
                f"frida-server's major version differs from ours (we have {ours})"
            ), (
                "the phone is fine — align this end, in frida-ios-dump's virtualenv: "
                f"`{pip} install 'frida=={major}.*'` to match the device's frida-server"
            )
        return False, f"frida-server is not reachable: {text[:90]}", None
    return True, f"reachable, {count} processes", _server_version(udid)


def _server_version(udid: str | None) -> str | None:
    """The device's frida-server version, over SSH — or None if we can't ask.

    Only used to explain a backend that cannot work, so a failure here costs a
    sentence rather than a check.
    """
    paramiko = decrypt_runner._import_paramiko()
    if paramiko is None:
        return None

    def read(port: int) -> str | None:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                "127.0.0.1", port=port,
                username=decrypt_runner.os.environ.get("GRAFT_SSH_USER", "root"),
                password=decrypt_runner.resolve_ssh_password(udid),
                timeout=decrypt_runner._PROBE_TIMEOUT, allow_agent=False, look_for_keys=False,
            )
            _in, out, _err = client.exec_command(
                "frida-server --version 2>/dev/null", timeout=decrypt_runner._PROBE_TIMEOUT
            )
            return out.read().decode(errors="replace")
        except Exception:
            return None
        finally:
            client.close()

    out = decrypt_runner._with_tunnel(udid, read)
    if not out:
        return None
    m = re.search(r"(\d+\.\d+\.\d+)", out)
    return m.group(1) if m else None


def _check_ssh(udid: str | None) -> tuple[bool, str, str | None, str | None]:
    """Whether the device answers SSH, and whether our credential is accepted.

    Returns `(ok, detail, fix, reason)`; `reason` is one of the `SSH_*` words
    for a failure, None otherwise.

    Four distinguishable outcomes, because they need four different responses
    and collapsing any two of them produces a message that sends people the
    wrong way:

        no iproxy            we cannot even reach the device's ports
        no banner            OpenSSH is not running on the phone
        banner, auth failed  it is running and the password is wrong
        banner, auth OK      nothing to do

    The banner is read over a raw socket rather than with a library, so this
    stage has no dependencies at all. Authentication needs `paramiko`, which
    the CLI does *not* ship — it lives in frida-ios-dump's own virtualenv — and
    when it is missing this says so instead of guessing. The first version of
    this function swallowed that ImportError and reported "no SSH server
    answered", which was a flat lie about a phone whose SSH was fine, and
    exactly the sort of error this whole check exists to abolish.
    """
    if shutil.which("iproxy") is None:
        return False, "iproxy is missing", "brew install libimobiledevice", SSH_NO_IPROXY

    banner = _ssh_banner(udid)
    if banner is None:
        # Not necessarily "never installed": iproxy accepts the local end
        # regardless, so silence here means nothing on the phone answered on
        # port 22 — which covers a missing OpenSSH *and* one that was running an
        # hour ago and has since stopped. Both were being told to install
        # software they already had, so the advice names both.
        return False, "no SSH server answered on the device", (
            "if OpenSSH is not installed, install it from Sileo; if it is, it has "
            "stopped — respring or restart it from your jailbreak app. "
            "frida-ios-dump pulls the app bundle over SSH"
        ), SSH_NO_SERVER

    if decrypt_runner.os.environ.get("GRAFT_SSH_KEY"):
        # Answered for the *frida-ios-dump* route, which takes `-K`. bagbak has
        # no key support at all — `bagbak_env` sends SSH_PASSWORD and nothing
        # else — so `_backends` is told separately (`key_only`) rather than
        # reading this as "SSH works for everyone". Reporting bagbak usable here
        # reopened exactly the auth failure the guard below it was added to
        # pre-empt.
        # `_dump_frida_ios_dump` prefers a key over the password, so a phone
        # configured that way authenticates fine while a password attempt fails.
        # Reporting that as "the login was rejected" greys out the only backend
        # that reliably decrypts the main binary, and advises storing a password
        # the user deliberately does not use.
        return True, f"{banner} — key auth configured (GRAFT_SSH_KEY), not verified here", None, None

    auth = _ssh_auth(udid)
    if auth is None:
        # Reachable, unverified. Not a failure: the dump may well work, and
        # calling it one would send the user to fix something that isn't broken.
        return True, f"{banner} — password not verified (paramiko not installed)", None, None
    if auth is True:
        return True, f"{banner}, authenticated as root", None, None
    return False, f"{banner}, but the login was rejected", (
        "set the phone's root password with `graft --set-ssh-password` "
        "(or from the device's row in the Devices pane)"
    ), SSH_LOGIN_REJECTED


def _ssh_banner(udid: str | None) -> str | None:
    """The SSH server's identification string, or None if nothing answers.

    A raw socket read, so "is SSH up" needs nothing installed. iproxy accepts
    the local connection whether or not the device end works, so the banner —
    which only the device can send — is the real test.
    """
    return decrypt_runner._with_tunnel(udid, _read_banner)


def _read_banner(port: int) -> str | None:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=decrypt_runner._PROBE_TIMEOUT) as sock:
            sock.settimeout(decrypt_runner._PROBE_TIMEOUT)
            data = sock.recv(255)
    except OSError:
        return None
    text = data.decode(errors="replace").strip()
    return text.splitlines()[0] if text else None


def _ssh_auth(udid: str | None) -> bool | None:
    """True/False if we could test the login, None if we could not.

    None is a real answer here, not an error swallowed — see `_check_ssh`.
    """
    if decrypt_runner._import_paramiko() is None:
        return None

    def attempt(port: int) -> bool:
        paramiko = decrypt_runner._import_paramiko()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            client.connect(
                "127.0.0.1", port=port,
                username=decrypt_runner.os.environ.get("GRAFT_SSH_USER", "root"),
                password=decrypt_runner.resolve_ssh_password(udid),
                timeout=decrypt_runner._PROBE_TIMEOUT, allow_agent=False, look_for_keys=False,
            )
            return True
        except Exception:
            return False
        finally:
            client.close()

    return decrypt_runner._with_tunnel(udid, attempt)






def _frontmost(udid: str | None) -> tuple[str, str] | None:
    """The app on screen as `(bundle_id, display name)`, or None."""
    try:
        bundle = decrypt_runner.frontmost_app(udid)
    except Exception:
        return None
    if not bundle:
        return None
    try:
        frida = decrypt_runner._import_frida("read the foreground app")
        device = decrypt_runner._frida_device(frida, udid)
        for app in device.enumerate_applications():
            if app.identifier == bundle:
                return bundle, app.name
    except Exception:
        pass
    return bundle, bundle


#: bagbak's note when frida-server couldn't be checked from this Mac.
FRIDA_SERVER_UNCHECKED = ("frida-server not checked (Graft checks it with frida-ios-dump's Python, "
                          "which isn't set up)")


def _backends(
    frida_ok: bool, ssh_ok: bool, server_version: str | None, *, key_only: bool = False,
    frida_unchecked: bool = False,
) -> list[BackendStatus]:
    """Which backends could run, given what the probes found.

    `frida_unchecked`: the frida probe failed on this Mac's side (frida-ios-dump
    not set up, or its frida broken), so frida-server's state is unknown, not
    bad. bagbak brings its own frida and needs none of Graft's, so a check
    Graft can't run doesn't block it: it is usable, unverified
    (FRIDA_SERVER_UNCHECKED), and an unreachable frida-server is bagbak's own
    error to report when it runs."""
    out: list[BackendStatus] = []

    if decrypt_runner.frida_ios_dump_available() is None:
        # The command alone in `note`, which the app offers to copy; the
        # alternative is prose, so it goes in `reason` (R11).
        out.append(BackendStatus(
            decrypt_runner.FRIDA, False,
            f"frida-ios-dump is not set up: run the setup command, "
            f"{decrypt_runner.FRIDA_IOS_DUMP_ELSEWHERE}",
            note=decrypt_runner.frida_ios_dump_setup_command(),
        ))
    elif not frida_ok:
        out.append(BackendStatus(decrypt_runner.FRIDA, False, "frida-server is not reachable"))
    elif not ssh_ok:
        out.append(BackendStatus(decrypt_runner.FRIDA, False, "it pulls the app over SSH"))
    else:
        out.append(BackendStatus(decrypt_runner.FRIDA, True))

    bag_version = _bagbak_frida_version()
    if decrypt_runner.bagbak_available() is None:
        out.append(BackendStatus(decrypt_runner.BAGBAK, False, "bagbak is not installed"))
    elif not frida_ok and not frida_unchecked:
        out.append(BackendStatus(decrypt_runner.BAGBAK, False, "frida-server is not reachable"))
    elif decrypt_runner.mode_is_supported() is False and (not ssh_ok or key_only):
        # bagbak 4.x pulls the bundle over SSH (see `bagbak_env`), and 4.x is
        # the major that can talk to a 16.x frida-server — i.e. the one this
        # setup runs. Reporting it usable without SSH enabled the Decrypt button
        # for a dump that dies on an ssh2 auth error a minute later, which is
        # the exact failure this probe exists to pre-empt.
        out.append(BackendStatus(
            decrypt_runner.BAGBAK, False,
            "this bagbak pulls the app over SSH with a password, and this device "
            "is set up for key auth (GRAFT_SSH_KEY)" if key_only else
            "this bagbak pulls the app over SSH, which is not available",
        ))
    elif frida_ok and _frida_mismatch(bag_version, server_version):
        out.append(BackendStatus(
            decrypt_runner.BAGBAK, False,
            f"bagbak bundles frida {bag_version} and the phone runs frida-server "
            f"{server_version} — install a bagbak whose frida matches "
            f"({decrypt_runner.BAGBAK_INSTALL} for a 16.x server)",
        ))
    else:
        out.append(BackendStatus(
            decrypt_runner.BAGBAK, True,
            note=FRIDA_SERVER_UNCHECKED if frida_unchecked else _version_skew_note(bag_version, server_version),
        ))
    return out


def _bagbak_frida_version() -> str | None:
    """The frida version bagbak ships with, read from its own package tree."""
    exe = decrypt_runner.bagbak_available()
    if not exe:
        return None
    import json
    from pathlib import Path

    root = Path(exe).resolve().parent.parent / "lib" / "node_modules" / "bagbak"
    for candidate in (root, Path(exe).resolve().parent.parent):
        pkg = candidate / "node_modules" / "frida" / "package.json"
        if pkg.is_file():
            try:
                return json.loads(pkg.read_text()).get("version")
            except (OSError, ValueError):
                return None
    return None


def _version_skew_note(client: str | None, server: str | None) -> str | None:
    """A same-major, different-version pairing worth mentioning but not blocking.

    Observed: bagbak with frida 16.7.19 against frida-server 16.6.6 connects
    fine — the handshake only checks majors — pulls the bundle, spawns the app,
    and then its injected agent never reports. Whether the skew causes that is
    unproven, which is exactly why this is a note and not a `reason`.
    """
    if not client or not server or client == server:
        return None
    if client.split(".")[0] != server.split(".")[0]:
        return None
    return (
        f"bagbak bundles frida {client}, the phone runs {server}. Majors match so it "
        f"connects — but if a dump hangs just after \"spawned app pid\", try matching "
        f"them exactly."
    )


def _frida_mismatch(client: str | None, server: str | None) -> bool:
    """Whether these two frida versions cannot work together.

    Majors must match — that is frida's own rule, and a mismatch is the error it
    prints. Unknown on either side is not treated as a mismatch: refusing to
    offer a backend because a *version lookup* failed would be worse than
    letting it try.
    """
    if not client or not server:
        return False
    return client.split(".")[0] != server.split(".")[0]
