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
record = {"tool": "ipatool", "argv": argv}

def done(code=0):
    with open(here / "calls.jsonl", "a") as f:
        f.write(json.dumps(record) + "\n")
    sys.exit(code)

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
    if scenario["signed_in"]:
        print(json.dumps({"level": "info", "email": EMAIL, "name": NAME,
                          "success": True, "time": "2026-11-17T00:00:00Z"}))
        done()
    print(json.dumps({"level": "error", "error": "failed to get account: "
                      "item could not be found in the keyring", "success": False}))
    done(1)

if argv[:1] == ["download"]:
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

print(f"fake ipatool: unexpected argv {argv}", file=sys.stderr)
done(2)
'''

_UNFAIRPLAY = r'''
import json, shutil, sys
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

print(f"fake unfairplay: unexpected argv {argv}", file=sys.stderr)
done(2)
'''


def macho(cryptid: int) -> bytes:
    """A 56-byte arm64 Mach-O with one `LC_ENCRYPTION_INFO_64`.

    Small enough to write inline and real enough for `otool -l`, which prints
    `cmd LC_ENCRYPTION_INFO_64` / `cryptid <n>` for it — checked by hand before
    any test relied on it, so `check_cryptid` runs for real rather than mocked.
    """
    header = struct.pack("<IiiIIIII", 0xFEEDFACF, 0x0100000C, 0, 2, 1, 24, 0, 0)
    command = struct.pack("<IIIIII", 0x2C, 24, 0x4000, 0x1000, cryptid, 0)
    return header + command


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
        }
        for path, body in ((self.ipatool, _IPATOOL), (self.unfairplay, _UNFAIRPLAY)):
            source = body.replace("EMAIL", repr(FAKE_EMAIL)).replace("NAME", repr(FAKE_NAME))
            path.with_suffix(".py").write_text(source)
            # A `/bin/sh` trampoline rather than a `#!<python>` line: this
            # checkout may live under a path with a space in it (the owner's
            # does), and a shebang cannot carry one.
            path.write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "$0.py" "$@"\n'
            )
            path.chmod(0o755)
        self.save()

    def set(self, **changes: Any) -> FakeTools:
        self.scenario.update(changes)
        self.save()
        return self

    def save(self) -> None:
        (self.root / "scenario.json").write_text(json.dumps(self.scenario))

    def calls(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.calls_file.exists():
            return []
        rows = [json.loads(line) for line in self.calls_file.read_text().splitlines()]
        return [r for r in rows if tool is None or r["tool"] == tool]

    def appstore_section(self) -> dict[str, str]:
        return {
            "ipatool": str(self.ipatool),
            "unfairplay": str(self.unfairplay),
            "unfairplay_models": str(self.models),
        }
