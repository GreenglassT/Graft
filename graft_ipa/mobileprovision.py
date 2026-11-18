"""Reading a .mobileprovision, and deciding whether a cached one is still usable.

A provisioning profile is a CMS-signed plist, so `plistlib` alone cannot open
it — `/usr/bin/security cms -D` does the unwrapping. That one fact used to live
only in `preflight.parse_mobileprovision`, which put it above `signer` and
`apple_portal` in the import graph and therefore out of reach of the code that
decides whether to re-provision. This module sits below all three so both
answers come from one decoder.

`cache_problem` is the other half: the auto-provisioning cache under
`app_store_connect.profile_cache_dir` is derived data with a knowable validity
condition, and until this existed nothing checked it. `ensure_profile_for_bundle`
returned the cached file whenever it existed — its comment said "signer.py
validates expiry upstream", which signer.py did not do — while app-aware
pre-flight *did* check expiry and device membership and hard-failed the build
before stage 4 could refresh anything. The result was an app whose every build
failed pre-flight with no in-product way out: the only remedy was deleting a
file under ~/.config by hand.
"""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from .errors import PreflightError


def decode(profile: Path) -> dict:
    """Decode a .mobileprovision and return the embedded plist as a dict."""
    if not profile.exists():
        raise PreflightError(f"provisioning profile not found: {profile}")
    try:
        out = subprocess.run(
            ["security", "cms", "-D", "-i", str(profile)],
            check=True,
            capture_output=True,
        ).stdout
    except FileNotFoundError as e:
        raise PreflightError(
            "/usr/bin/security not found (expected on macOS)",
        ) from e
    except subprocess.CalledProcessError as e:
        raise PreflightError(
            f"failed to decode {profile}: {e.stderr.decode(errors='replace').strip()}"
        ) from e
    return plistlib.loads(out)


def expiration(plist: dict) -> datetime | None:
    """The profile's expiry as an aware UTC datetime, or None if it declares none.

    plistlib hands back naive datetimes for some profiles; Apple writes them in
    UTC, and every caller compares against `datetime.now(UTC)`.
    """
    value = plist.get("ExpirationDate")
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def provisioned_devices(plist: dict) -> list[str]:
    """The UDIDs the profile lists. Empty for a distribution profile."""
    return list(plist.get("ProvisionedDevices") or [])


def app_id_prefix(plist: dict, fallback: str = "") -> str:
    """The App ID prefix a decoded profile's application-identifier starts with.

    Not the team id: accounts from before 2011 can have a prefix that differs
    from their team id, and an identifier built from the team is covered by
    none of their profiles, fresh ones included. `ApplicationIdentifierPrefix`
    first, then `TeamIdentifier` (they are the same on every newer account),
    then `fallback`. One rule for the two places that need it: judging a cached
    profile (`cache_problem`) and signing with one (`lc_sign`).
    """
    return (plist.get("ApplicationIdentifierPrefix") or plist.get("TeamIdentifier") or [fallback])[0]


def cache_problem(
    profile: Path,
    *,
    device_udids: Iterable[str] = (),
    application_identifier: str | None = None,
) -> str | None:
    """Why this cached profile can't be reused as-is, or None if it can.

    The two conditions are the two the user cannot fix from inside the product:

    * expired — a profile is good for a year and this cache is refreshed by
      nothing else, so left alone it eventually fails every build of the app;
    * missing a device — connect a new iPhone and the cached profile, generated
      before it existed, does not list it.

    A file that won't decode counts as a problem too. That direction is
    deliberately the safe one: re-provisioning costs an App Store Connect round
    trip, while trusting a profile we cannot read costs a signed IPA the phone
    refuses to install.

    Returns the reason as a sentence rather than a bool because it is worth
    saying out loud — the refresh is otherwise invisible, and "why did this
    build talk to Apple?" is a fair question.

    `application_identifier` (the bundle id or wildcard pattern, without the
    prefix) adds a third condition: the profile must be *for* that App ID —
    `<the profile's ApplicationIdentifierPrefix>.<it>`, falling back to the
    team id, case-insensitively. A profile built against the wrong App ID
    (as `matches[0]` once did, picking `….LiveProcess` for the main app) is
    valid in every other respect and would otherwise be reused forever. A
    profile that declares no application-identifier is not judged on it.
    """
    try:
        plist = decode(profile)
    except PreflightError as exc:
        return f"the cached profile could not be read ({exc.message})"

    if application_identifier is not None:
        declared = str((plist.get("Entitlements") or {}).get("application-identifier", ""))
        wanted = f"{app_id_prefix(plist)}.{application_identifier}"
        if declared and declared.lower() != wanted.lower():
            return f"the cached profile is for {declared}, not {wanted}"

    expiry = expiration(plist)
    if expiry is not None and expiry <= datetime.now(UTC):
        return f"the cached profile expired on {expiry.isoformat()}"

    # An empty ProvisionedDevices list is a distribution profile, which covers
    # every device by not naming any. Absence of the key is not absence of the
    # device, and treating it as such would re-provision on every single build.
    listed = {u.lower() for u in provisioned_devices(plist)}
    if listed:
        missing = sorted(u for u in device_udids if u.lower() not in listed)
        if missing:
            return (
                "the cached profile does not list "
                + ", ".join(missing)
                + " among its provisioned devices"
            )
    return None
