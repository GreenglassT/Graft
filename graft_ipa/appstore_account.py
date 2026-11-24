"""What Graft knows about the App Store sign-in, beyond what ipatool says.

ipatool keeps its sign-in in the login keychain under the service
`ipatool-auth.service`, as three generic-password items:

- `account`: the session, and the password ipatool saves to renew it;
- `anisette-v4`: its device provisioning;
- `kbsync-v1`: a cache.

It makes them with `KeychainTrustApplication`, which means only the program
that created an item may read it without asking.

A 1.x user signed in with their own ipatool. Graft 2's bundled ipatool is a
different program (signed with Graft's Developer ID), so macOS won't hand it
those items without a dialog (Task 0, K1), and signing in over them asked
twice for the login keychain password (K2). A dialog must never pop up in
front of a status check, and nothing in the app would explain those. So
Graft keeps a record of which items its own ipatool made.

**The record.** After a sign-in through the bundled ipatool, the marker in
Graft's state dir records each of the three items that exists by its
creation date (`cdat`), which `security find-generic-password` prints among
the attributes, with no secret and no dialog. An item is Graft's only while
its creation date is still the recorded one. That holds across Graft's own
rewrites: ipatool 2.8.0's keyring (byteness/keyring, `keychain_macos.go`)
saves over an item that is there with `SecItemUpdate`, in place, keeping
its creation date and its access list, and a purchase or download that
signs in again silently saves the session that way. It stops holding the
moment another program deletes and recreates an item (`auth revoke` and a
new login, a reset keychain, a state dir copied to another Mac), because
the new item has a new date. `kbsync-v1` is different: a login never makes
it, the first download after one does, and ipatool deletes and remakes it in
normal use (`invalidateKBSync` after Apple rejects the cached blob). So it
goes on the record after a bundled download or purchase that made it
(`adopt_cache`), and one that isn't Graft's is deleted before the bundled
ipatool downloads or buys (`clear_foreign_cache`) as well as before a
sign-in, which costs a cache refill and nothing else. Another program's
`anisette-v4` stops a Get instead (`foreign_device`): a download can't do
without one, and only a sign-in makes another.

- `legacy_signin`: with the bundled ipatool, an `account` item that isn't
  Graft's. Status then doesn't ask ipatool (`appstore.status`).
- `needs_clearing` / `clear_items`: before the bundled ipatool signs in,
  every item that isn't Graft's is deleted (by its exact service and account,
  nothing else), so the new ones belong to it and it reads them silently
  from then on (K3). Deleting them raises no dialog (K2b). A user's own
  ipatool in Terminal must then sign in again (decision 2).
- Any other ipatool (a pipx install's, one set in config.yaml or found on
  PATH) is the mirror image (the whole-diff review's signin-1): what the
  record vouches for, the bundled ipatool made, and that one can read it
  only behind a Keychain dialog, K1 and K2 the other way round. So for it,
  those are the other program's items: `graft_signin` is an `account` that
  is Graft's, which status doesn't ask it about either, and its sign-in
  deletes them first (`needs_clearing`). Items the record doesn't vouch for
  are its own business, and with no record `security` isn't even asked.
  Its sign-in vouches for nothing: the record is emptied, since the items
  it had dates for are gone.
- `signed_out`: `auth revoke` removes `account` and `kbsync-v1` and leaves
  `anisette-v4`, which stays on the record, so signing in again after a Sign
  Out doesn't delete Graft's own device item.

A marker from before the record (no `bundled_items`) vouches for nothing:
such a sign-in reads as another ipatool's and the next sign-in replaces it.

Nothing here reads an item's secret: `keychain.find_item` asks `security`
for attributes only. When `security` can't answer (a locked keychain, a
dialog nobody answered), these say so (None, `KeychainUnanswered`) rather
than guess, and stop at the first such answer, so no more than one
`keychain.TIMEOUT_SECONDS` wait ever stands in front of a status check or a
sign-in.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import data_dirs, keychain, tools
from .errors import AppStoreError

IPATOOL_SERVICE = "ipatool-auth.service"
IPATOOL_ITEMS = ("account", "anisette-v4", "kbsync-v1")
#: What `auth revoke` deletes (ipatool 2.8.0, `appstore_revoke.go`).
REVOKED_ITEMS = ("account", "kbsync-v1")
MARKER = "appstore-signin.json"
#: The marker's shape: 2 has `bundled_items`.
MARKER_FORMAT = 2

#: What a sign-in says when `security` couldn't answer for ipatool's items.
KEYCHAIN_UNANSWERED = (
    "Graft couldn't check your login keychain for an older ipatool sign-in, so it didn't "
    "sign in (signing in over one would ask for your login password twice)"
)
KEYCHAIN_UNANSWERED_HINT = (
    "unlock the login keychain in Keychain Access (or restart this Mac if it won't unlock), "
    "then sign in again"
)
#: What a Get or a claim says when another ipatool's download cache
#: (`kbsync-v1`) couldn't be deleted first. Not a sign-in: that meets the
#: same delete (`needs_clearing`), and the Get clears it itself once
#: `security` can (delta audit #8).
CLEAR_CACHE_HINT = (
    "try again (no need to sign in again); if it fails the same way, unlock the login keychain in "
    "Keychain Access, or restart this Mac if it won't unlock, then try again"
)


class KeychainUnanswered(AppStoreError):
    """`security` couldn't say whether one of ipatool's items is there."""

    def __init__(self, name: str) -> None:
        super().__init__(f"{KEYCHAIN_UNANSWERED} (no answer about `{name}`)",
                         hint=KEYCHAIN_UNANSWERED_HINT)
        self.item = name


class ClearFailed(AppStoreError):
    """An item that isn't Graft's couldn't be deleted before a sign-in."""

    def __init__(self, name: str) -> None:
        super().__init__(
            f"Graft couldn't remove an older ipatool sign-in (`{name}`) from your login keychain, "
            "so it didn't sign in (signing in over it would ask for your login password twice)",
            hint=KEYCHAIN_UNANSWERED_HINT,
        )
        self.item = name


def marker_path() -> Path:
    return data_dirs.state_dir() / MARKER


def _read_marker() -> dict[str, Any]:
    try:
        data = json.loads(marker_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def vouched() -> dict[str, str]:
    """The items the bundled ipatool made, by name, with their creation dates,
    as the marker records them. Nothing for an old marker, or none."""
    items = _read_marker().get("bundled_items")
    if not isinstance(items, dict):
        return {}
    return {k: v for k, v in items.items() if k in IPATOOL_ITEMS and isinstance(v, str) and v}


def _write_marker(body: dict[str, Any]) -> None:
    """0600, atomically: a temp file beside it, then a rename."""
    path = marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".appstore-signin-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(body) + "\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _record_items() -> dict[str, str]:
    """Each of ipatool's items that is there, by its creation date. Raises
    `KeychainUnanswered` at the first item `security` can't answer for."""
    found: dict[str, str] = {}
    for name in IPATOOL_ITEMS:
        look = keychain.find_item(IPATOOL_SERVICE, name)
        if look.exists is None or (look.exists and look.created is None):
            # Not "none of them": an item that is there and can't be dated
            # can't be told from another program's later, so it isn't
            # recorded as Graft's, and the sign-in says so.
            raise KeychainUnanswered(name)
        if look.exists:
            found[name] = look.created
    return found


def mark_signed_in(source: str) -> None:
    """After a sign-in through the ipatool from `source`, write the marker:
    which ipatool, when, and, for the bundled one, the creation date of each
    of its items that is there. Nothing about the account.

    Any other ipatool vouches for nothing, and the record is emptied: that
    sign-in deleted every item the record vouched for first
    (`needs_clearing`), so none of those dates is any item's now. Raises
    `KeychainUnanswered` when `security` can't say, and OSError when the
    marker can't be written.
    """
    items = _record_items() if source == tools.SOURCE_BUNDLED else {}
    _write_marker({
        "format": MARKER_FORMAT,
        "ipatool_source": source,
        "signed_in_at": _now(),
        "bundled_items": items,
    })


def signed_out() -> None:
    """After `auth revoke`: `account` and `kbsync-v1` are gone; the record of
    `anisette-v4`, which revoke leaves, stays."""
    data = _read_marker()
    if not data:
        return
    kept = {k: v for k, v in vouched().items() if k not in REVOKED_ITEMS}
    _write_marker({**data, "format": MARKER_FORMAT, "bundled_items": kept})


def forget_signed_in() -> None:
    marker_path().unlink(missing_ok=True)


def _ours(name: str, look: keychain.Lookup, record: dict[str, str]) -> bool:
    return bool(look.exists) and look.created is not None and record.get(name) == look.created


def needs_clearing(source: str) -> list[str]:
    """Which of ipatool's items a sign-in through `source`'s ipatool must
    delete first: each that is there and is another program's (`_foreign`).
    With the bundled one, each that isn't Graft's; with any other, each the
    record vouches for, which Graft's bundled ipatool made (the module
    docstring).

    Raises `KeychainUnanswered` at the first item `security` can't answer
    for, before asking about the next.
    """
    clear: list[str] = []
    for name in IPATOOL_ITEMS:
        foreign = _foreign(source, name)
        if foreign is None:
            raise KeychainUnanswered(name)
        if foreign:
            clear.append(name)
    return clear


def _foreign(source: str, name: str) -> bool | None:
    """Whether `name` is there and another program made it, so that the
    ipatool from `source` can read it only behind a Keychain dialog. With
    the bundled ipatool, that is any item the record doesn't vouch for; with
    any other, one it does: Graft's bundled ipatool made that (signin-1).
    None when `security` couldn't say. For any other ipatool, a name the
    record doesn't vouch for is its own business, and `security` isn't
    asked."""
    bundled = source == tools.SOURCE_BUNDLED
    record = vouched()
    if not bundled and name not in record:
        return False
    look = keychain.find_item(IPATOOL_SERVICE, name)
    if look.exists is None:
        return None
    if not look.exists:
        return False
    return _ours(name, look, record) != bundled


def legacy_signin(source: str) -> bool | None:
    """With the bundled ipatool: whether the `account` item is there and isn't
    Graft's, a sign-in it can't read without a dialog. None when `security`
    couldn't say, which is not "no": status then asks nobody. False for any
    other ipatool (`graft_signin` is its counterpart)."""
    if source != tools.SOURCE_BUNDLED:
        return False
    return _foreign(source, "account")


def graft_signin(source: str) -> bool | None:
    """With any ipatool but the bundled one: whether the `account` item is
    Graft's own, the bundled ipatool's, which this one can't read without a
    dialog (signin-1). None when `security` couldn't say. Status and a Get
    then ask nobody, and its sign-in deletes Graft's items first. False for
    the bundled ipatool, and, without asking `security`, when the record
    vouches for no `account`."""
    if source == tools.SOURCE_BUNDLED:
        return False
    return _foreign(source, "account")


#: What ipatool 2.8.0 reads and saves on every download, besides `account`:
#: its download cache (`cachedKBSync`, `storeKBSync`), which a login never
#: makes, so the first download after one does ...
KBSYNC = "kbsync-v1"
#: ... and its device provisioning (`storeAMDProof`), read on a silent
#: re-login too. Only a login provisions it: a download without one fails
#: ("store AMD state is not provisioned").
DEVICE = "anisette-v4"


def foreign_device(source: str) -> bool | None:
    """Whether `anisette-v4` is there and another program made it
    (`_foreign`: with the bundled ipatool, one that isn't Graft's; with any
    other, one that is). A Get then stops before ipatool runs (`appstore._require_signed_in`):
    reading it is a dialog, and deleting it would only make the download
    fail, since nothing but a sign-in makes another. None when `security`
    couldn't say."""
    return _foreign(source, DEVICE)


def clear_foreign_cache(source: str) -> bool:
    """Before ipatool downloads or buys: a `kbsync-v1` another program made
    (`_foreign`) is deleted, so the ipatool that runs makes its own rather
    than read another program's behind a Keychain dialog (the fix re-check's
    problem 6: another ipatool's download after Graft's sign-in makes one;
    and, for any other ipatool, one Graft's own made: signin-1). It is a
    cache, which ipatool fills again, and deleting it raises no dialog (K2b).
    Returns whether one was deleted. Raises `KeychainUnanswered` when
    `security` can't say, and `ClearFailed` when it is still there."""
    foreign = _foreign(source, KBSYNC)
    if foreign is None:
        raise KeychainUnanswered(KBSYNC)
    return bool(foreign) and bool(clear_items([KBSYNC]))


def cache_before(source: str) -> keychain.Lookup | None:
    """`kbsync-v1` as it is just before the bundled ipatool runs, for
    `adopt_cache` to tell a cache that run made from one already there.
    None for any other ipatool."""
    if source != tools.SOURCE_BUNDLED:
        return None
    return keychain.find_item(IPATOOL_SERVICE, KBSYNC)


def adopt_cache(source: str, before: keychain.Lookup | None) -> bool:
    """After a successful download or purchase by the bundled ipatool: a
    `kbsync-v1` that run made goes on the record, so the next Get keeps it
    rather than delete Graft's own cache (`clear_foreign_cache`). A login
    makes none, so the first download after a sign-in does.

    "That run made" is what `before` (`cache_before`) shows: there was none
    then, or there was one with another creation date, which ipatool
    deleted and remade (`invalidateKBSync`). One that was there before and
    still has its date is never adopted, so neither is another program's.
    Only onto the record of a bundled sign-in that vouches for `account`,
    which the Get's own check required. Returns whether it recorded one.
    Raises `KeychainUnanswered` and OSError, which a caller may ignore: the
    cost is a cache refill.

    The lock is read again first: the Get's own check read it before a
    download that can take many minutes, and a keychain locked since then
    could make this lookup wait, or ask for its password (the try13 delta
    audit's #16). Locked, nothing is asked and nothing recorded, so the
    next Get deletes the cache as another program's: a refill, no dialog."""
    if source != tools.SOURCE_BUNDLED or before is None or before.exists is None:
        return False
    data = _read_marker()
    items = vouched()
    if data.get("format") != MARKER_FORMAT or "account" not in items:
        return False
    if keychain.login_keychain_locked():
        return False
    look = keychain.find_item(IPATOOL_SERVICE, KBSYNC)
    if look.exists is None:
        raise KeychainUnanswered(KBSYNC)
    if not look.exists or look.created is None or items.get(KBSYNC) == look.created:
        return False
    if before.exists and before.created == look.created:
        return False
    _write_marker({**data, "bundled_items": {**items, KBSYNC: look.created}})
    return True


def keychain_refused(error: str) -> bool | None:
    """Whether ipatool's "not found" is really macOS refusing it the item.

    After a denied Keychain dialog, ipatool says "The specified item could not
    be found in the keyring" (Task 0, K1), which is untrue: the item is there,
    and ipatool wasn't let read it. Asked of `security` by attributes only.
    None when ipatool said "not found" and `security` can't say whether the
    item is there: then it is neither a refusal nor "signed out".
    """
    if "could not be found in the keyring" not in error.lower():
        return False
    return keychain.item_exists(IPATOOL_SERVICE, "account")


def clear_items(names: tuple[str, ...] | list[str]) -> list[str]:
    """Delete these of ipatool's items; return those that were there.

    Raises `ClearFailed` at the first that `security` didn't delete for any
    reason but "there is none", before trying the next: a sign-in must not
    go ahead over an item that is still there.
    """
    gone: list[str] = []
    for name in names:
        if name not in IPATOOL_ITEMS:
            raise ValueError(f"not one of ipatool's items: {name}")
        deleted = keychain.delete_item(IPATOOL_SERVICE, name)
        if deleted is None:
            raise ClearFailed(name)
        if deleted:
            gone.append(name)
    return gone


def clear_legacy_items() -> list[str]:
    """Delete all three of ipatool's items; return those that were there."""
    return clear_items(IPATOOL_ITEMS)
