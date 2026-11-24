"""Graft's questions to frida, asked by frida-ios-dump's own Python.

Graft.app bundles no frida (review part 2 #3: its native module statically
links LGPL code whose source the download would have to carry). The one
place frida is needed at all is the jailbroken-device dump, whose tools the
user installs, and frida-ios-dump's virtualenv already has frida 16 for its
own `dump.py`. So `decrypt_runner` runs this file with that virtualenv's
interpreter, once per question:

    <frida-ios-dump>/.venv/bin/python -I frida_probe.py QUESTION [--device UDID] [BUNDLE_ID]

    version                     frida's version
    devices                     every device frida sees: id, name, type
    processes                   the device's processes: pid, name
    applications                its installed apps: identifier, name, pid (0 if not running)
    frontmost                   the app on screen, or null
    launch BUNDLE_ID            spawn the app and resume it: its pid

Without `--device` the question goes to the USB device. Every argument is
checked against a plain pattern and nothing goes through a shell. The answer
is one line of JSON on stdout:

    {"ok": true, "result": ..., "frida": "16.7.19"}
    {"ok": false, "error": "...", "frida": "16.7.19" or null}

with exit status 0, 1 when frida failed (or isn't importable) and 2 for
arguments this refuses. It imports nothing but the standard library and frida:
graft_ipa is not installed where it runs.
"""

from __future__ import annotations

import json
import re
import sys

QUESTIONS = ("version", "devices", "processes", "applications", "frontmost", "launch")
#: Questions about one device, and the one that takes a bundle id.
_ON_A_DEVICE = ("processes", "applications", "frontmost", "launch")
_DEVICE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}")
_BUNDLE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
#: How long frida waits for the device to answer.
TIMEOUT = 10


def parse(argv: list[str]) -> tuple[str, str | None, str | None]:
    """(question, device id or None, bundle id or None). ValueError for
    anything else."""
    if not argv or argv[0] not in QUESTIONS:
        raise ValueError(f"the question must be one of {', '.join(QUESTIONS)}")
    question, rest = argv[0], list(argv[1:])
    device = None
    if rest[:1] == ["--device"]:
        if question not in _ON_A_DEVICE:
            raise ValueError(f"{question} takes no --device")
        if len(rest) < 2 or not _DEVICE_ID.fullmatch(rest[1]):
            raise ValueError("--device needs a device id")
        device, rest = rest[1], rest[2:]
    if question == "launch":
        if len(rest) != 1 or not _BUNDLE_ID.fullmatch(rest[0]):
            raise ValueError("launch takes exactly one bundle id")
        return question, device, rest[0]
    if rest:
        raise ValueError(f"{question} takes no other arguments")
    return question, device, None


def answer(frida, question: str, device_id: str | None, bundle_id: str | None):
    if question == "version":
        return frida.__version__
    if question == "devices":
        return [{"id": d.id, "name": d.name, "type": d.type} for d in frida.enumerate_devices()]
    device = frida.get_device(device_id, timeout=TIMEOUT) if device_id else frida.get_usb_device(timeout=TIMEOUT)
    if question == "processes":
        return [{"pid": p.pid, "name": p.name} for p in device.enumerate_processes()]
    if question == "applications":
        return [{"identifier": a.identifier, "name": a.name, "pid": a.pid} for a in device.enumerate_applications()]
    if question == "frontmost":
        app = device.get_frontmost_application()
        return None if app is None else {"identifier": app.identifier, "name": app.name, "pid": app.pid}
    pid = device.spawn([bundle_id])
    device.resume(pid)
    return {"pid": pid}


def main(argv: list[str]) -> int:
    def reply(code: int, **fields) -> int:
        print(json.dumps(fields))
        return code

    try:
        question, device_id, bundle_id = parse(argv)
    except ValueError as e:
        return reply(2, ok=False, error=f"bad arguments: {e}")
    try:
        import frida
    except ImportError as e:
        return reply(1, ok=False, error=f"frida can't be imported here: {e}", frida=None)
    version = getattr(frida, "__version__", None)
    try:
        result = answer(frida, question, device_id, bundle_id)
    except Exception as e:  # frida's own errors: the caller decides what each means
        return reply(1, ok=False, error=str(e) or type(e).__name__, frida=version)
    return reply(0, ok=True, result=result, frida=version)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
