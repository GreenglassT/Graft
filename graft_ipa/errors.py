"""Custom exception classes for graft pipeline stages."""

from __future__ import annotations


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


class ConfigError(GraftError):
    """Raised when the config file is missing, malformed, or references missing paths."""

    exit_code = 2


class PreflightError(GraftError):
    """Raised when a required CLI tool, cert, profile, or device is missing."""

    exit_code = 3


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


class InstallError(GraftError):
    """Raised when ideviceinstaller fails to install the IPA on the device."""

    exit_code = 8


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
    "get this app". Every raise carries a hint, and most hints are a Terminal
    command, since signing in and enrolling are deliberately never done by
    this tool (see `appstore.py`).

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
# behaviour users of `pipefail` already expect.
EXIT_BROKEN_PIPE = 141
