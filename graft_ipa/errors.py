"""Custom exception classes for graft pipeline stages."""

from __future__ import annotations

import errno


class GraftError(Exception):
    """Base class for all graft errors. Carries an exit code + a hint."""

    exit_code: int = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class Stopped(SystemExit):
    """The run was stopped by a signal: the GUI's Stop (SIGTERM), or a hang-up.

    Raised by `lc_manage.exit_on_termination` in place of the default
    disposition, which kills the process where it stands and runs no
    `finally`. A `SystemExit`, with the shell's status for the signal
    (128 + its number), so it passes every `except Exception` on its way out
    — a stopped multi-app build attempts no further app — and the process
    still exits 143 for a SIGTERM. Its own class because the build envelope
    must tell a stop from a failure: a stopped build records no history row
    and ends `build-end {ok: false, stopped: true, error: null}` (C-STOP).
    """

    def __init__(self, signum: int) -> None:
        super().__init__(128 + signum)
        self.signum = signum


#: What `open` fails with when macOS won't let Graft read a file: EPERM
#: when its privacy controls refuse it (a file in Downloads, Desktop,
#: Documents or iCloud Drive whose prompt was denied), EACCES for plain
#: permissions.
NOT_LET_READ = frozenset({errno.EPERM, errno.EACCES})


def not_let_read_hint(shown: str, code: int = errno.EPERM) -> str:
    """The hint for a file the user chose that macOS wouldn't let Graft
    read (`NOT_LET_READ`), by `code`, the `open` failure's errno.

    EPERM is macOS's privacy controls: the app hands the CLI the file's
    path, and the prompt that raises is Graft's to allow (the whole-diff
    review's app-2). macOS asks on behalf of the app that started the CLI,
    which from Terminal is Terminal, so both are named.

    EACCES is the file's own owner and permissions, which no privacy
    setting changes: a .p12 copied with sudo belongs to root (the try14
    delta audit's d14-cli-5).
    """
    if code == errno.EACCES:
        return (
            f"your user can't read {shown}: it may belong to another user, root for one if it "
            "was copied with sudo, or have no read permission. Check its owner in Finder › Get Info "
            "› Sharing & Permissions, or copy it somewhere as yourself and choose the copy"
        )
    return (
        f"macOS didn't let Graft read {shown}: allow Graft (or Terminal, if you ran graft there) in "
        "System Settings › Privacy & Security › Files and Folders, or move the file out of "
        "Downloads/Desktop/Documents and choose it again"
    )


class ConfigError(GraftError):
    """Raised when the config file is missing, malformed, or references missing paths."""

    exit_code = 2


class PaidAccountRequiredError(ConfigError):
    """Raised when a free-Apple-ID config asks for something only a paid
    Apple Developer Program membership can do: signing with a .p12 and
    profile, installing LiveContainer, App Store Connect provisioning.

    A ConfigError, and the same exit code, because what is missing is a
    `signing:` section in config.yaml. Every refusal is built by
    `config.paid_account_required`, so the message and the way out read the
    same whichever path the user hit.
    """


class PreflightError(GraftError):
    """Raised when a required CLI tool, cert, profile, or device is missing."""

    exit_code = 3


class DeveloperModeOffError(PreflightError):
    """The device a development-signed build installs on has Developer Mode
    off (iOS 16 and later): the app would install and then not open. Raised
    before the build starts (`developer_mode.require_on`). A device that
    isn't ready, so a `PreflightError`, with no code of its own; the build
    that hits it exits 1, as any failed build does, with the message and the
    hint in its `build-end.error`."""


class IPANotFoundError(GraftError):
    """Raised when a build has no decrypted IPA to start from in the cache."""

    exit_code = 4


class IPAInspectionError(GraftError):
    """Raised when an IPA is malformed or fails decryption verification."""

    exit_code = 5


class CyanError(GraftError):
    """Raised when cyan fails to inject tweaks."""

    exit_code = 6


class SigningError(GraftError):
    """Raised when zsign fails to sign the IPA."""

    exit_code = 7


class DeviceNotRegisteredError(SigningError):
    """The device an install targets isn't registered (enabled) in the Apple
    developer account, so no profile can include it. Graft never registers
    one itself: that would spend one of the account's yearly device slots."""


class InstallError(GraftError):
    """Raised when installing the IPA on the device fails."""

    exit_code = 8


class NoDeviceConnectedError(InstallError):
    """No iPhone or iPad is on usbmux, so a build has nowhere to install
    (`installer.resolve_target`, `installer.select_device`)."""


class SeveralDevicesError(InstallError):
    """Several devices are connected, none of them the default, and none was
    picked for this build: the person chooses (spec §5b's table).

    An `InstallError`, for the callers that catch every "nowhere to install"
    (`installer.resolve_target`'s three), with a code of its own: nothing
    was installed, and nothing failed but the choice of device."""

    exit_code = 22


class DeviceNotConnectedError(InstallError):
    """The device named for this install (`--device`, a pick on the app's
    bar, the device `--update` goes to) isn't on usbmux.

    `udid` is that device and `label` what to call it: its last-known name
    (`devices_seen`), else the UDID. The message stays "requested device …
    not connected", which the app's bar quotes; `--update` says
    "<label> isn't connected" (spec §11.6)."""

    def __init__(self, udid: str, *, label: str | None = None, hint: str | None = None) -> None:
        super().__init__(f"requested device {udid} not connected", hint=hint)
        self.udid = udid
        self.label = label or udid


class RepoError(GraftError):
    """Raised when an APT repository is unreachable, malformed, or not a valid repo."""

    exit_code = 12


class ITunesError(GraftError):
    """Raised when an iTunes lookup fails or an App Store URL is malformed."""

    exit_code = 13


class IPAValidationError(GraftError):
    """Raised when a user-supplied IPA fails to validate (missing fields,
    wrong bundle id, unreadable zip)."""

    exit_code = 14


class TweakNotFoundError(GraftError):
    """Raised when a tweak filename can't be found in `cfg.tweaks_dir`."""

    exit_code = 15


class TweakInUseError(GraftError):
    """Raised when deleting a tweak that's still attached to one or more apps."""

    exit_code = 16


class StagedIPAInUseError(GraftError):
    """Raised when deleting the last staged IPA an app has to build from.

    A sibling of `TweakInUseError` rather than a reuse of it: that one is about
    a tweak attached to apps and its docstring says so, and the two carry
    different sentences and different exit codes. Unlike a tweak-in-use this is
    overridable — deleting the file and re-acquiring later is a legitimate
    thing to want — which is why the verb takes a force flag and this is a
    refusal rather than a prohibition.
    """

    exit_code = 17


class AppStoreError(GraftError):
    """Raised when getting an app from the App Store, or decrypting one, fails.

    Covers both of the user's own tools — ipatool (download) and UnfairPlay
    (decrypt) — because to the person reading the hint they are one step:
    "get this app". Every raise carries a hint, and hints name the place in
    Graft.app and the Terminal command, since both front-ends can sign in and
    enroll (see `appstore.py`).

    20, not 18: another branch was adding error classes at the same time, and
    two branches each taking "the next free number" merge cleanly as text and
    collide as meaning.
    """

    exit_code = 20


# The status for "the thing reading our stdout hung up" — `graft --json
# ... | head`. Not an exception class, because a reader that stops reading is
# not a failure of the pipeline; it is what `| head` means. It lives here
# anyway because this file is the only place that answers "which exit codes are
# taken?", and the person who adds the next `exit_code = ` above needs to see
# this number while they do it.
#
# 141 is 128 + SIGPIPE (13), the status a shell reports for a child that SIGPIPE
# actually killed — measured here, not recalled: under bash, `git log | head`
# and `yes | head` both leave 141 in `PIPESTATUS[0]`. Python sets SIGPIPE to
# SIG_IGN at startup and raises `BrokenPipeError` instead, so nothing in this
# process produces 141 unless we choose it. Choosing it makes graft
# answer a hung-up reader with the same number every other tool on the box
# does, and puts that answer far enough from the 1-17 taxonomy above that it
# can never be misread as a pipeline error — 1 in particular, the generic
# `GraftError` code, would have been indistinguishable from a real one.
#
# Not 0: the output really was cut off mid-stream. A script that captured
# `--json` into a consumer which then died has an incomplete event stream, and
# saying "success" to that is a lie it cannot detect any other way. Non-zero
# does mean `set -o pipefail` treats `graft ... | head` as a failed
# pipeline — but that is true of `git log | head` too, and is the shell
# behavior users of `pipefail` already expect.
EXIT_BROKEN_PIPE = 141

# Four error classes live beside the code that raises them; their codes live
# here, so this file stays the one answer to "which exit codes are taken?".
EXIT_CANISTER = 9
EXIT_PORTAL = 10
EXIT_DECRYPT = 11
#: LiveContainer's own, since it shared 11 with decrypt failures. 21, not 18
#: or 19, for the reason AppStoreError's docstring gives for skipping them.
EXIT_LIVECONTAINER = 21

#: Every exit status graft uses, and what it means. Checked against every
#: `exit_code` in the package (tests/test_exit_codes_and_usbmuxd.py), so a
#: class with a code not listed here, or two classes with one code, fails.
EXIT_CODES: dict[int, str] = {
    1: "GraftError: a failure with no code of its own, or one graft has no translation for",
    2: "ConfigError (PaidAccountRequiredError too); Click's usage errors also exit 2",
    3: "PreflightError, and `--check` with a failing row",
    4: "IPANotFoundError",
    5: "IPAInspectionError",
    6: "CyanError",
    7: "SigningError",
    8: "InstallError",
    EXIT_CANISTER: "CanisterError (canister.py)",
    EXIT_PORTAL: "PortalError (apple_portal.py)",
    EXIT_DECRYPT: "DecryptError (decrypt_runner.py)",
    12: "RepoError",
    13: "ITunesError",
    14: "IPAValidationError",
    15: "TweakNotFoundError",
    16: "TweakInUseError — the app keys on it",
    17: "StagedIPAInUseError — the app keys on it",
    20: "AppStoreError",
    EXIT_LIVECONTAINER: "LiveContainerError (lc_runner.py)",
    22: "SeveralDevicesError: several devices are connected, and none was named or picked",
    64: "a build with no app named (usage)",
    EXIT_BROKEN_PIPE: "the reader of stdout hung up",
    128 + 1: "Stopped by SIGHUP",
    128 + 15: "Stopped by SIGTERM: the app's Stop",
}
