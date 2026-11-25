"""Stand-ins for the user's ipatool and UnfairPlay, for tests only.

Never the real tools: those sign in with a real Apple ID and decrypt with a
model recovered from this Mac's enrollment, and nothing in this suite may touch
either. These are tiny Python scripts written into a tmp dir and pointed at by
the config's `appstore:` section — the same seam production uses — that print
the JSON the real ones were seen printing on 2026-11-17 (recorded in the spec).

A `scenario.json` beside them says how to behave, and every invocation is
appended to `calls.jsonl` with its argv, so a test can assert what was (and,
as often, what was *not*) run.
"""

from __future__ import annotations

import json
import plistlib
import struct
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

# Deliberately distinctive, so a test can assert they never reach any output.
FAKE_EMAIL = "fake-apple-id@example.invalid"
FAKE_NAME = "Fake Appleseed"

_IPATOOL = r'''
import json, os, shutil, stat, sys
from pathlib import Path

here = Path(__file__).resolve().parent
scenario = json.loads((here / "scenario.json").read_text())
argv = sys.argv[1:]
# What reached it of the environment: the Unicorn variable Graft.app once
# set (now never), and the .p12 password variable, which child_env keeps out.
record = {"tool": "ipatool", "argv": argv, "unicorn": os.environ.get("IPATOOL_UNICORN_LIBRARY"),
          "p12_secret": os.environ.get("GRAFT_P12_PASS")}

def done(code=0):
    with open(here / "calls.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    sys.exit(code)

# --- The login keychain, when the scenario names one (`FakeSecurity(store=)`).
# ipatool's items as ipatool 2.8.0 keeps them (byteness/keyring): `Set` adds
# an item, or updates the one that is there in place, keeping its creation
# date and its access list; `Remove` deletes it. An item may be read or
# updated without a dialog only by the program that made it; anything else
# is a Keychain dialog, written down as a `keychain-dialog` record and
# answered with `keychain_answer` (Deny unless told otherwise).
KC = scenario.get("keychain")
ME = str(here)
SERVICE = "ipatool-auth.service"
NOT_FOUND = "The specified item could not be found in the keyring"

class Denied(Exception):
    pass

def _cdat(tick):
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 11, 1, 12, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=7) * tick).strftime("%Y%m%d%H%M%SZ")

def kc_state():
    path = Path(KC)
    return json.loads(path.read_text()) if path.exists() else {"tick": 0, "items": []}

def kc_row(data, key):
    return next((r for r in data["items"] if r["service"] == SERVICE and r["account"] == key), None)

def dialog(what):
    with open(here / "calls.jsonl", "a") as f:
        f.write(json.dumps({"tool": "keychain-dialog", "by": ME, "argv": argv, "what": what}) + "\n")
    if scenario.get("keychain_answer", "deny") != "allow":
        raise Denied(what)

def kc_get(key):
    row = kc_row(kc_state(), key)
    if row is None:
        return None
    if row["owner"] != ME:
        dialog(f"read {key}")
    return row["secret"]

def kc_set(key, secret):
    data = kc_state()
    row = kc_row(data, key)
    data["tick"] += 1
    if row is not None:
        if row["owner"] != ME:
            dialog(f"update {key}")
        row["secret"], row["mdat"] = secret, _cdat(data["tick"])
    else:
        data["items"].append({"service": SERVICE, "account": key, "secret": secret,
                              "cdat": _cdat(data["tick"]), "mdat": _cdat(data["tick"]), "owner": ME})
    Path(KC).write_text(json.dumps(data))

def kc_remove(key):
    data = kc_state()
    row = kc_row(data, key)
    if row is None:
        return False
    data["items"].remove(row)
    Path(KC).write_text(json.dumps(data))
    return True

def kc_account_or_fail():
    """`account` read as ipatool reads it, failing as ipatool fails."""
    try:
        found = kc_get("account")
    except Denied:
        found = None  # K1: a denied dialog reads as "not found"
    if found is None:
        print(json.dumps({"level": "error", "error": "failed to get account: failed to get item: "
                          + NOT_FOUND, "success": False}))
        done(1)

if argv == ["--version"]:
    print(f"ipatool version {scenario['ipatool_version']}")
    done()

if argv[:2] == ["auth", "info"]:
    if scenario.get("auth_sleep"):
        import time
        time.sleep(scenario["auth_sleep"])
    if scenario.get("auth_raw") is not None:
        print(scenario["auth_raw"])
        done()
    if KC:
        kc_account_or_fail()
        print(json.dumps({"level": "info", "email": EMAIL, "name": NAME,
                          "success": True, "time": "2026-11-17T00:00:00Z"}))
        done()
    if scenario["signed_in"]:
        print(json.dumps({"level": "info", "email": EMAIL, "name": NAME,
                          "success": True, "time": "2026-11-17T00:00:00Z"}))
        done()
    print(json.dumps({"level": "error", "error": "failed to get account: "
                      "item could not be found in the keyring", "success": False}))
    done(1)

if argv[:1] == ["download"]:
    if KC:
        kc_account_or_fail()
        # ipatool 2.8.0's storeAMDProof (appstore_amd.go): the device item
        # read, its error ignored, then saved back. Without one it can't make
        # the proof ("store AMD state is not provisioned"): only a login
        # provisions it.
        try:
            device = kc_get("anisette-v4")
        except Denied:
            device = None
        if device is None:
            print(json.dumps({"level": "error", "error": "generate Store AMD proof: store AMD state is "
                              "not provisioned", "success": False}))
            done(1)
        kc_set("anisette-v4", device)
        # cachedKBSync, then storeKBSync (appstore_kbsync_cache.go): read,
        # then saved, both best effort.
        try:
            kc_get("kbsync-v1")
        except Denied:
            pass
        try:
            kc_set("kbsync-v1", "cache")
        except Denied:
            pass
    bundle = argv[argv.index("-b") + 1]
    out_dir = Path(argv[argv.index("-o") + 1])
    record["out_dir_mode"] = stat.S_IMODE(out_dir.stat().st_mode)
    if scenario.get("download_sleep"):
        # Long enough to be stopped mid-download; the work dir already exists.
        with open(here / "calls.jsonl", "a") as f:
            f.write(json.dumps({**record, "sleeping": True, "pid": os.getpid()}) + "\n")
        import time
        time.sleep(scenario["download_sleep"])
    if scenario.get("download_raw") is not None:
        print(scenario["download_raw"])
        done(scenario.get("download_exit", 0))
    wanted = argv[argv.index("--external-version-id") + 1] if "--external-version-id" in argv else None
    if wanted is not None and wanted in (scenario.get("gone_versions") or []):
        print(json.dumps({"level": "error", "error": "received error: This item is no longer available.",
                          "success": False}))
        done(1)
    if scenario.get("download_error"):
        stream = sys.stderr if scenario.get("error_on_stderr") else sys.stdout
        print(json.dumps({"level": "error", "error": scenario["download_error"],
                          "success": False}), file=stream)
        done(1)
    dest = Path(scenario.get("download_output") or out_dir / f"{bundle}_100000002_1.0.ipa")
    shutil.copyfile(scenario["download_ipa"], dest)
    verdict = {"level": "info", "output": str(dest), "purchased": False,
               "success": True, "time": "2026-11-17T00:00:00Z"}
    if scenario.get("download_no_output") == "null":
        verdict["output"] = None
    elif scenario.get("download_no_output") == "omit":
        del verdict["output"]
    print(json.dumps(verdict))
    done()

if argv[:2] == ["auth", "login"]:
    import hashlib, termios, time, tty

    with open(here / "calls.jsonl", "a") as f:
        f.write(json.dumps({"tool": "ipatool", "argv": argv, "started": True,
                            "pid": os.getpid()}) + "\n")
    record["env"] = dict(os.environ)
    record["stdin_tty"] = os.isatty(0)
    record["stdout_tty"] = os.isatty(1)

    def say(obj):
        print(json.dumps(obj), flush=True)

    def prompt(text, masked):
        # ipatool 2.8.0 cmd/prompt.go. Masked (the password): raw mode first,
        # so a pipe fails before anything is asked; then the prompt on stderr,
        # `*` per character, `\r\n` after. Not masked (the Apple ID, the
        # code): a plain line read, left to the terminal's own echo.
        saved = None
        if masked:
            saved = termios.tcgetattr(0)
            tty.setraw(0)
        try:
            os.write(2, text.encode())
            got = bytearray()
            while True:
                ch = os.read(0, 1)
                if not ch:
                    raise EOFError
                if ch in (b"\r", b"\n"):
                    break
                got += ch
                if masked:
                    os.write(2, b"*")
            if masked:
                os.write(2, b"\r\n")
            return bytes(got)
        finally:
            if saved is not None:
                termios.tcsetattr(0, termios.TCSANOW, saved)

    def ask(text, masked, what):
        try:
            return prompt(text, masked)
        except termios.error:
            say({"level": "error", "success": False, "error": f"failed to read {what}: "
                 "failed to configure terminal: inappropriate ioctl for device"})
            done(1)
        except EOFError:
            say({"level": "error", "success": False, "error": f"failed to read {what}: EOF"})
            done(1)

    def echo_on():
        # What the terminal would echo back of a line read now (ECHO), which
        # Graft clears on the pty before ipatool starts.
        try:
            return bool(termios.tcgetattr(0)[3] & termios.ECHO)
        except termios.error:
            return None

    def busy_while_silent():
        # A first sign-in downloads ipatool's components while saying nothing
        # on the terminal. `login_chatter` logs on stdout as it goes;
        # `login_grow` writes into a download folder instead, as ipatool 2.8.0
        # does: its Unicorn into ~/Library/Caches/ipatool, the macOS assets into
        # a `$TMPDIR/ipatool-macos-27-*` workspace.
        chatter, grow = scenario.get("login_chatter"), scenario.get("login_grow")
        if chatter:
            until = time.monotonic() + chatter["for"]
            while time.monotonic() < until:
                time.sleep(chatter["every"])
                say({"level": "debug", "message": "fetching components", "time": "2026-11-19T00:00:00Z"})
        if grow:
            if grow["where"] == "cache":
                folder = Path.home() / "Library" / "Caches" / "ipatool" / "unicorn" / "2.1.4"
                name = ".unicorn-artifact-fake"
            else:
                folder = Path(os.environ.get("TMPDIR") or "/tmp") / "ipatool-macos-27-fake" / "frameworks"
                name = "CoreFP"
            folder.mkdir(parents=True, exist_ok=True)
            until = time.monotonic() + grow["for"]
            with open(folder / name, "ab") as f:
                while time.monotonic() < until:
                    time.sleep(grow["every"])
                    f.write(b"\0" * 4096)
                    f.flush()

    # Only asked when `-e`/`--email` is absent, as ipatool does. Graft never
    # passes it (the Apple ID would show in `ps`), so this is the prompt it meets.
    record["echo"] = []
    given = next((argv[i + 1] for i, a in enumerate(argv[:-1]) if a in ("-e", "--email")), None)
    if scenario.get("login_preamble"):
        # Something prompt-like on the terminal first ("note: "), and only
        # half a second later the real prompt: not a prompt left waiting.
        os.write(2, scenario["login_preamble"].encode())
        time.sleep(0.5)
    if given is None:
        record["echo"].append(echo_on())
        # `id_prompt`: another build's wording ("Apple ID: "), which Graft
        # doesn't know and so never answers.
        typed = ask(scenario.get("id_prompt") or "enter Apple ID (email address or phone number): ",
                    False, "Apple ID")
        record["email_sha256"] = hashlib.sha256(typed).hexdigest()
    password = ask("enter password: ", True, "password")
    record["password_sha256"] = hashlib.sha256(password).hexdigest()
    if scenario.get("login_password_twice"):
        # A build that asks again: Graft must not type the password twice.
        again = ask("enter password: ", True, "password")
        record["password_again_sha256"] = hashlib.sha256(again).hexdigest()
    say({"level": "info", "message": "preparing authentication; the first login may "
         "take a few minutes", "time": "2026-11-19T00:00:00Z"})
    if KC:
        # nativeLogin: read the device state (a failed read is ignored and the
        # device provisioned afresh), then save it, before Apple is asked.
        try:
            try:
                kc_get("anisette-v4")
            except Denied:
                pass
            kc_set("anisette-v4", "device")
        except Denied:
            say({"level": "error", "success": False, "error": "prepare Apple authentication: save "
                 "Apple authentication state: failed to set item: The user name or passphrase you "
                 "entered is not correct."})
            done(1)
    if scenario.get("login_passphrase"):
        # The file backend's prompt, which macOS never uses.
        ask("enter passphrase to unlock /fake/keyring (this is separate from your Apple ID "
            "password): ", True, "passphrase")
    busy_while_silent()
    time.sleep(scenario.get("login_silent", 0))
    if scenario.get("login_error"):
        say({"level": "error", "error": scenario["login_error"], "success": False})
        done(1)
    if scenario.get("login_two_factor", True):
        if scenario.get("login_code_then_exit"):
            # Asks for the code, then dies (a crash, an outside kill) while the
            # person looks for it, maybe after saying why.
            os.write(2, b"enter 2FA code: ")
            time.sleep(scenario.get("login_exit_after", 0.5))
            if scenario.get("login_dying_words"):
                say({"level": "error", "error": scenario["login_dying_words"], "success": False})
            done(1)
        record["echo"].append(echo_on())
        code = ask("enter 2FA code: ", False, "2FA code")
        record["code_sha256"] = hashlib.sha256(code).hexdigest()
        if scenario.get("login_code_twice"):
            again = ask("enter 2FA code: ", False, "2FA code")
            record["code_again_sha256"] = hashlib.sha256(again).hexdigest()
        time.sleep(scenario.get("login_answer_delay", 0))
        if scenario.get("login_code_error"):
            say({"level": "error", "error": scenario["login_code_error"], "success": False})
            done(1)
    if KC:
        try:
            kc_set("account", "session")
        except Denied:
            say({"level": "error", "success": False, "error": "failed to save account in keychain: "
                 "failed to set item: The user name or passphrase you entered is not correct."})
            done(1)
    say({"level": "info", "name": NAME, "email": EMAIL, "success": True,
         "time": "2026-11-19T00:00:00Z"})
    done()

if argv[:2] == ["auth", "revoke"]:
    if scenario.get("revoke_error"):
        print(json.dumps({"level": "error", "error": scenario["revoke_error"], "success": False}))
        done(1)
    if KC:
        # appstore_revoke.go: `account` and `kbsync-v1`; `anisette-v4` stays.
        gone = kc_remove("account")
        kc_remove("kbsync-v1")
        if not gone:
            print(json.dumps({"level": "error", "success": False, "error": "failed to remove account "
                              "from keychain: failed to remove item: The specified item could not "
                              "be found in the keychain."}))
            done(1)
    print(json.dumps({"level": "info", "success": True}))
    done()

if argv[:1] == ["purchase"]:
    if KC:
        kc_account_or_fail()
    if scenario.get("purchase_error"):
        print(json.dumps({"level": "error", "error": scenario["purchase_error"], "success": False}))
        done(1)
    print(json.dumps({"level": "info", "alreadyOwned": bool(scenario.get("already_owned")),
                      "success": True}))
    done()

# The older-versions commands, as ipatool 2.8.0 prints them with `--format
# json`: zerolog's `level` and `time` around the fields each command logs
# (cmd/list_versions.go, cmd/get_version_metadata.go), and on failure the
# root command's `{"level":"error","error":...,"success":false}` (cmd/root.go).
if argv[:1] == ["list-versions"]:
    if KC:
        kc_account_or_fail()
    bundle = argv[argv.index("-b") + 1]
    if scenario.get("list_versions_error"):
        print(json.dumps({"level": "error", "error": scenario["list_versions_error"],
                          "success": False, "time": "2026-11-24T00:00:00Z"}))
        done(1)
    print(json.dumps({"level": "info", "externalVersionIdentifiers": scenario.get("versions", []),
                      "bundleID": bundle, "success": True, "time": "2026-11-24T00:00:00Z"}))
    done()

if argv[:1] == ["get-version-metadata"]:
    if KC:
        kc_account_or_fail()
    wanted = argv[argv.index("--external-version-id") + 1]
    if scenario.get("metadata_trickle"):
        # A read that keeps a little coming and so never stalls, which the
        # bundled patch's 30 s wait for silence lets run on: a debug line every
        # `every` seconds for `for` seconds, and only then the answer. Says
        # its pid first, so a test can tell it was killed.
        import time
        with open(here / "calls.jsonl", "a") as f:
            f.write(json.dumps({**record, "trickling": True, "pid": os.getpid()}) + "\n")
        trickle = scenario["metadata_trickle"]
        until = time.monotonic() + trickle["for"]
        while time.monotonic() < until:
            print(json.dumps({"level": "debug", "message": "reading", "time": "2026-11-24T00:00:00Z"}),
                  flush=True)
            time.sleep(trickle["every"])
    meta = (scenario.get("version_metadata") or {}).get(wanted)
    error = scenario.get("metadata_error") or (meta or {}).get("error")
    if meta is None and not error:
        # What a version Apple no longer serves looks like: the download
        # request's customer message (appstore_get_version_metadata.go).
        error = "received error: This item is no longer available."
    if error:
        print(json.dumps({"level": "error", "error": error, "success": False,
                          "time": "2026-11-24T00:00:00Z"}))
        done(1)
    # `releaseDate` is zerolog's `Time`, RFC 3339 by default.
    print(json.dumps({"level": "info", "externalVersionID": wanted,
                      "displayVersion": meta["version"], "releaseDate": meta["date"],
                      "success": True, "time": "2026-11-24T00:00:00Z"}))
    done()

print(f"fake ipatool: unexpected argv {argv}", file=sys.stderr)
done(2)
'''

_UNFAIRPLAY = r'''
import json, os, shutil, sys
from pathlib import Path

here = Path(__file__).resolve().parent
scenario = json.loads((here / "scenario.json").read_text())
argv = sys.argv[1:]
record = {"tool": "unfairplay", "argv": argv}

def done(code=0):
    with open(here / "calls.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    sys.exit(code)

def opt(name):
    return argv[argv.index(name) + 1]

if argv == ["--version"]:
    print(f"unfairplay version {scenario['unfairplay_version']}")
    done()

if argv[:1] == ["decrypt"]:
    src, out, report = Path(opt("--input")), Path(opt("--output")), Path(opt("--report"))
    sibling = src.with_name(src.stem + ".decrypted.json")
    if scenario.get("write_default_report"):
        sibling.write_text("{}")
    if scenario.get("decrypt_error"):
        print(scenario["decrypt_error"], file=sys.stderr)
        done(1)
    shutil.copyfile(scenario["decrypted_ipa"], out)
    report.write_text("{}")
    if scenario.get("decrypt_raw") is not None:
        print(scenario["decrypt_raw"])
        done()
    if scenario.get("decrypt_preamble"):
        print(scenario["decrypt_preamble"])
    print(json.dumps({
        "OutputPath": str(out), "OutputSHA256": "00" * 32,
        "ReportPath": str(sibling if scenario.get("write_default_report") else report),
        "EncryptedImages": scenario.get("encrypted_images", 8),
        "DecryptedPages": scenario.get("decrypted_pages", 51088),
        "PlaintextVerified": scenario.get("plaintext_verified", True),
        "LogicalKeySHA256": "11" * 32, "LogicalKeySHA256BySINF": {"a": "b"},
        "AccountDataStripped": True, "RemovedLicenseFiles": 41,
        "RemovedMetadataFields": 2,
    }, indent=2))
    done()

if argv[:1] == ["setup"]:
    # UnfairPlay v0.4.0's setup: it creates --model-dir (0700) and writes the
    # model there, then prints its JSON result (S2.1's keys). Records who ran
    # it, and from where, so a test can tell a copy from the original.
    import hashlib
    record["uid"] = os.getuid()
    record["argv0"] = os.environ.get("FAKE_ARGV0", "")
    models = Path(opt("--model-dir"))
    # What it was given to read: the Wrapper's entries, and every file of the
    # app inside with its bytes' hash.
    import hashlib
    wrapper = Path(opt("--app")) / "Wrapper"
    if wrapper.is_dir():
        record["wrapper"] = sorted(os.listdir(wrapper))
        record["files"] = {
            str(f.relative_to(wrapper)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(wrapper.rglob("*")) if f.is_file() and not f.is_symlink()
        }
    if scenario.get("setup_sleep"):
        # A setup that hangs: says it started (its pid, and its child's when
        # `setup_child`, a sleep of its own, as the real one's sw_vers and
        # ioreg are), then sleeps, so a test can stop it.
        started = {"tool": "unfairplay", "argv": argv, "started": True, "pid": os.getpid()}
        if scenario.get("setup_child"):
            import subprocess
            started["child"] = subprocess.Popen(["/bin/sleep", str(scenario["setup_sleep"])]).pid
        import signal
        if scenario.get("setup_ignore_term"):
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        else:
            def on_term(signum, frame):
                # Dies of it, as the real one does, saying so first: a stop
                # that gave it TERM before KILL.
                with open(here / "calls.jsonl", "a") as f:
                    f.write(json.dumps({"tool": "unfairplay-signal", "signal": "TERM"}) + "\n")
                os._exit(128 + signum)

            signal.signal(signal.SIGTERM, on_term)
        with open(here / "calls.jsonl", "a") as f:
            f.write(json.dumps(started) + "\n")
        import time
        time.sleep(scenario["setup_sleep"])
    # The real one prints a progress line to stderr per recovery step, as it
    # goes (cmd/unfairplay printRecoveryProgress), and, being Go, dies of
    # SIGPIPE when fd 1 or 2 is a pipe whose reader has gone.
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    print("recovery build=fake profile=minimal-current-sinf phase=start", file=sys.stderr, flush=True)
    if scenario.get("setup_error"):
        print(scenario["setup_error"], file=sys.stderr)
        done(scenario.get("setup_exit", 1))
    models.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = hashlib.sha256(opt("--app").encode()).hexdigest()[:16] + ".bin"
    (models / name).write_bytes(b"model for " + opt("--app").encode())
    print(json.dumps({"success": True, "plaintext_verified": True, "model_reused": False}))
    done()

print(f"fake unfairplay: unexpected argv {argv}", file=sys.stderr)
done(2)
'''


def itunes_offline(bundle_id: str, *, country: str | None = None):
    """`itunes.lookup_by_bundle_id` as if this Mac were offline.

    A get refused for want of a license looks the app's price up
    (`appstore._listing`), and so does `--appstore-claim`. Installed autouse
    wherever a test can reach either, so none of them touches the network: a
    test that wants a listing answers the lookup itself.
    """
    from graft_ipa.errors import ITunesError

    raise ITunesError(f"iTunes lookup request failed for {bundle_id}: no network in tests")


def macho(cryptid: int) -> bytes:
    """A 56-byte arm64 Mach-O with one `LC_ENCRYPTION_INFO_64`.

    Small enough to write inline and real enough for `otool -l`, which prints
    `cmd LC_ENCRYPTION_INFO_64` / `cryptid <n>` for it — checked by hand before
    any test relied on it, so `check_cryptid` runs for real rather than mocked.
    """
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    command = struct.pack("<IIIIII", 0x2C, 24, 0x4000, 0x1000, cryptid, 0)
    return header + command


def make_ios_app(
    root: Path,
    name: str = "Example",
    *,
    bundle_id: str = "com.example.app",
    wrapper: bool = True,
    sinfs: int = 1,
    encrypted: bool = True,
    plist: bool = True,
) -> Path:
    """An iPhone app as the Mac App Store installs it on Apple silicon (Task
    0, S2.5): `<name>.app/Wrapper/<name>.app` with `SC_Info/<name>.sinf` (and
    the `.supf`/`.supp` files beside it, which aren't licenses) and an
    executable carrying `LC_ENCRYPTION_INFO_64` (cryptid 1 when `encrypted`).
    Returns the outer `.app`."""
    outer = root / f"{name}.app"
    inner = outer / "Wrapper" / f"{name}.app" if wrapper else outer
    (inner / "SC_Info").mkdir(parents=True)
    if wrapper:
        (outer / "Wrapper" / "iTunesMetadata.plist").write_bytes(plistlib.dumps({}))
    if plist:
        (inner / "Info.plist").write_bytes(plistlib.dumps({
            "CFBundleIdentifier": bundle_id, "CFBundleExecutable": name,
            "CFBundleDisplayName": name,
        }))
    (inner / name).write_bytes(macho(1 if encrypted else 0))
    for i in range(sinfs):
        (inner / "SC_Info" / f"{name}{i or ''}.sinf").write_bytes(b"sinf")
    for extra in (".supf", ".supp", ".supx"):
        (inner / "SC_Info" / f"{name}{extra}").write_bytes(b"sup")
    return outer


def make_ipa(
    path: Path,
    *,
    bundle_id: str = "com.example.app",
    version: str = "21.46.3",
    encrypted: bool | None = None,
    marker: str = "",
) -> Path:
    """A minimal IPA. `encrypted=None` gives a non-Mach-O main binary, the
    shape every pre-existing `--provide-ipa` fixture has; True/False give a
    real Mach-O with cryptid 1/0. `marker` lands in a resource file so two
    fixtures with the same identity are still distinguishable byte-wise."""
    path.parent.mkdir(parents=True, exist_ok=True)
    binary = b"\x00\x00" if encrypted is None else macho(1 if encrypted else 0)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Payload/App.app/Info.plist", plistlib.dumps({
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "CFBundleName": "App",
            "CFBundleExecutable": "App",
        }))
        zf.writestr("Payload/App.app/App", binary)
        zf.writestr("Payload/App.app/marker.txt", marker)
    return path


class FakeTools:
    """The two fakes plus their scenario, in `root`."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.ipatool = root / "ipatool"
        self.unfairplay = root / "unfairplay"
        self.models = root / "models"
        self.models.mkdir(exist_ok=True)
        (self.models / "enrollment.bin").write_bytes(b"model")
        self.calls_file = root / "calls.jsonl"
        fixtures = root / "fixtures"
        self.scenario: dict[str, Any] = {
            "ipatool_version": "2.8.0",
            "signed_in": True,
            "download_error": None,
            "download_output": None,
            "download_ipa": str(make_ipa(fixtures / "encrypted.ipa", encrypted=True)),
            "unfairplay_version": "dev-d7503082a59e",
            "decrypt_error": None,
            "plaintext_verified": True,
            "decrypted_ipa": str(make_ipa(fixtures / "decrypted.ipa", encrypted=False)),
            "write_default_report": False,
            "login_two_factor": True,
            "login_silent": 0,
            "login_error": None,
            "login_code_error": None,
            "revoke_error": None,
            "purchase_error": None,
            "already_owned": False,
        }
        for path, body in ((self.ipatool, _IPATOOL), (self.unfairplay, _UNFAIRPLAY)):
            source = body.replace("EMAIL", repr(FAKE_EMAIL)).replace("NAME", repr(FAKE_NAME))
            path.with_suffix(".py").write_text(source)
            # A `/bin/sh` trampoline rather than a `#!<python>` line: this
            # checkout may live under a path with a space in it (the owner's
            # does), and a shebang cannot carry one. The script by its full
            # path, so a copy of the trampoline (the enrollment script's root
            # copy) still runs it; `FAKE_ARGV0` says which file ran.
            # `FAKE_WARM_UP` ends it before Python starts (`warm_up`).
            path.write_text(
                '#!/bin/sh\n[ -z "${FAKE_WARM_UP-}" ] || exit 0\n'
                f'FAKE_ARGV0="$0" exec "{sys.executable}" "{path}.py" "$@"\n'
            )
            path.chmod(0o755)
        self.save()

    def set(self, **changes: Any) -> FakeTools:
        self.scenario.update(changes)
        self.save()
        return self

    def save(self) -> None:
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))

    def warm_up(self) -> None:
        """Run the fake ipatool once, before anything times it.

        macOS checks a new executable, a script too, on its first exec:
        syspolicyd looks up a notarization ticket online, then XProtect scans
        the file. The child waits inside `execve` until that's done, while
        Popen has already returned. The verdict is kept per file, so later
        runs start at once. On this Mac the lookup took about 0.1 s before
        2026-11-23. Over a phone hotspot it takes about 0.7 s, and up to its
        own 3 s timeout. A sign-in starts its silence limit at the spawn, so a
        2 s limit sometimes ran out before the fake had printed its first
        prompt. This pays that cost before the clock starts. `FAKE_WARM_UP`
        makes the trampoline exit before Python starts, so this run leaves no
        record in `calls.jsonl`.
        """
        subprocess.run([str(self.ipatool)], env={"FAKE_WARM_UP": "1"}, check=True, timeout=30)

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.calls_file.exists():
            return []
        rows = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def dialogs(self) -> list[str]:
        """Every Keychain dialog this fake ipatool would have shown, in order
        ("read account", "update anisette-v4", ...). Needs `keychain=`."""
        return [c["what"] for c in self.calls("keychain-dialog")]

    def login_runs(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """(started, finished) records of every `auth login` run. A run that
        was killed has a started record and no finished one."""
        logins = [c for c in self.calls("ipatool") if c["argv"][:2] == ["auth", "login"]]
        return [c for c in logins if c.get("started")], [c for c in logins if not c.get("started")]

    def appstore_section(self) -> dict[str, str]:
        return {
            "ipatool": str(self.ipatool),
            "unfairplay": str(self.unfairplay),
            "unfairplay_models": str(self.models),
        }
