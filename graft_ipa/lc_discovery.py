"""Which LiveContainers are on a device, and which one a push goes into.

One answer for every caller: `--lc-status` (what is installed, in which role),
delivery (`lc_runner`, which container gets the IPA), `--lc-clean` and the
free-account `--check` row. They used to answer separately — status by the
configured team id, delivery by a `com.kdt.livecontainer` prefix — and a free
Apple ID has no team id in config.yaml to answer with at all.

**What the installers do.** Every way of putting LiveContainer on a device
without a jailbreak installs it as `com.kdt.livecontainer.<TEAMID>`:
AltStore/SideStore, iloader (through isideload, `format!("{}.{}",
main_bundle_id, team.team_id)`) and Impactor (`format!("{identifier}.
{team_id}")`). The LiveContainer+SideStore build has the same main bundle id.
Extra instances are `com.kdt.LiveContainer2.<TEAMID>` and
`com.kdt.LiveContainer3.<TEAMID>`, case varying by who made them. A team id is
ten characters, letters and digits. So the patterns below find a LiveContainer
whatever team installed it, and a free Apple ID needs no configuration for it.
Case-insensitively, all of them: nothing guarantees every installer agrees.

TrollStore and a rootful jailbreak install upstream's own id,
`com.kdt.livecontainer`, as a System app; `installed_bundle_ids` lists those
too. On-device re-signers leave other suffixes. Every id under the
`com.kdt.livecontainer` prefix is therefore a candidate (`legacy` when no
pattern names it), and `choose` uses the only one there is, whatever it is.

`role_of` with a team keeps the old exact-id reading too, so a paid config
whose team id is not ten characters (a test fixture, say) still recognises the
LiveContainers `--lc-install` put on under it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from . import lc_instance
from .config import FREE_DEVICE_STEPS
from .lc_runner import LiveContainerError

#: The primary, as every installer names it. Case-insensitive like the
#: secondaries: `com.kdt.LiveContainer.<TEAM>` is a primary too.
PRIMARY_RE = re.compile(r"^com\.kdt\.livecontainer\.[A-Za-z0-9]{10}$", re.IGNORECASE)
#: LiveContainer2 and 3. `LiveContainer2` is what AltStore and `--lc-install`
#: write, and nothing guarantees every installer agrees.
SECONDARY_RE = re.compile(r"^com\.kdt\.livecontainer([23])\.[A-Za-z0-9]{10}$", re.IGNORECASE)

#: Anything under this prefix that the patterns do not match is `legacy`: the
#: bare upstream id, or an odd suffix an on-device re-signer left. Shown by
#: `--lc-status`, used by a push when it is the only LiveContainer there, and
#: never chosen over one the patterns name.
FAMILY_PREFIX = lc_instance.LC_ORIGINAL_ID

#: Where to send someone with a free Apple ID who has no LiveContainer yet:
#: the device half of `config.FREE_SETUP_STEPS`, which `--init` prints too.
FREE_SETUP_HINT = (
    "set LiveContainer up with your free Apple ID: "
    + " ".join(f"{n}. {step}" for n, step in enumerate(FREE_DEVICE_STEPS, 1))
    + " Graft looks for a bundle id starting com.kdt.livecontainer"
)
PAID_SETUP_HINT = (
    "install LiveContainer with `graft --lc-install --device <udid>`, or through "
    "SideStore/AltStore"
)


#: What a LiveContainer listing asks the device for. Every application type,
#: not just `User`: TrollStore and a rootful jailbreak register what they
#: install as a System app, the bare `com.kdt.livecontainer` included.
APPLICATION_TYPE = "Any"
#: The one attribute discovery needs. A System listing with every attribute
#: is the whole OS's app catalogue, and nothing here reads it.
BUNDLE_ID_ONLY = ["CFBundleIdentifier"]


async def installed_bundle_ids(lock) -> list[str]:
    """Every app's bundle id on the device behind `lock`, User and System
    alike: what `choose` is asked of, by the push and by `--check`."""
    from pymobiledevice3.services.installation_proxy import InstallationProxyService

    from .device_session import closing_best_effort

    proxy = InstallationProxyService(lockdown=lock)
    async with closing_best_effort(proxy):
        found = await proxy.lookup(
            {"ApplicationType": APPLICATION_TYPE, "ReturnAttributes": BUNDLE_ID_ONLY}
        )
    return sorted(found or {})


def role_of(bundle_id: str, team: str | None = None) -> str | None:
    """`primary`, `2`, `3`, `legacy` (any other LiveContainer), or None.

    `--lc-status` and delivery both ask this, so what `--lc-status` calls the
    primary is where an unpinned push goes. `team` is None for a free Apple
    ID, and the bundle id patterns answer alone.
    """
    if team:
        for n in lc_instance.INSTANCES:
            if bundle_id.lower() == lc_instance.instance_bundle_id(n, team).lower():
                return "primary" if n == 1 else str(n)
    if PRIMARY_RE.match(bundle_id):
        return "primary"
    secondary = SECONDARY_RE.match(bundle_id)
    if secondary:
        return secondary.group(1)
    if bundle_id.lower().startswith(FAMILY_PREFIX):
        return "legacy"
    return None


def is_livecontainer_id(bundle_id: str) -> bool:
    """Whether `bundle_id` can name a LiveContainer: what an `lc_bundle_id:`
    pin must be. Any id `role_of` recognises, in any case, the prefix forms
    `choose` accepts (`com.kdt.LiveContainer2`) included."""
    return bool(bundle_id) and role_of(bundle_id) is not None


def livecontainers(installed: Iterable[str], team: str | None = None) -> dict[str, str]:
    """Every LiveContainer in `installed` (bundle ids), mapped to its role."""
    found = {}
    for bundle_id in installed:
        role = role_of(bundle_id, team)
        if role is not None:
            found[bundle_id] = role
    return dict(sorted(found.items()))


def choose(
    installed: Iterable[str],
    *,
    pinned: str | None = None,
    team: str | None = None,
    free_account: bool = False,
) -> tuple[str, list[str]]:
    """The LiveContainer a push goes into, and the others that are installed.

    Every id `role_of` recognises is a candidate, `legacy` ones included.
    Exactly one is used:

    1. with `pinned` (an app's `lc_bundle_id`), that one: by id in any case,
       or as the prefix of one candidate. A pin that names none of them, or
       names an app that is not LiveContainer, is an error, never a fallback;
    2. the only candidate, when there is one, whatever its role;
    3. the configured team's primary, when it is installed;
    4. the only primary. The bare upstream id `com.kdt.livecontainer` counts
       as one: it is what a jailbreak or TrollStore installs, and it was the
       push target before discovery existed.

    Anything else is ambiguous, and the error lists what was found with a pin
    to copy; guessing would put the IPA where the user is not looking, and
    nothing would say so. That includes the bare id beside a suffixed primary:
    one came from a jailbreak and one from an installer, nothing on the device
    says which the user imports from, and a pin settles it for good.
    """
    ids = list(installed)
    roles = livecontainers(ids, team)
    candidates = sorted(roles)
    if pinned:
        chosen = _pinned(pinned, candidates)
    else:
        chosen = _unpinned(roles, candidates, team, free_account)
    return chosen, [b for b in candidates if b != chosen]


#: How to change or drop a pin, for every refusal of one.
PIN_HINT = (
    "pin one of them with `graft --set-app-options --app <name> --lc-bundle-id <id>`, "
    "or drop the pin with `--no-lc-bundle-id`"
)


def _pinned(pinned: str, candidates: list[str]) -> str:
    """The pin, as installed: by id in any case, or as the prefix of exactly
    one LiveContainer. Anything else is an error that names the pin.

    A pin that names no LiveContainer used to be ignored when one other was
    installed, which is the case a pin exists for: the user said "not that
    one". And a pin to an app that is not LiveContainer at all was accepted,
    putting the IPA in a Documents folder LiveContainer never reads.
    """
    if not is_livecontainer_id(pinned):
        raise LiveContainerError(
            f"lc_bundle_id {pinned!r} is not a LiveContainer bundle id (they start "
            f"{FAMILY_PREFIX}); LiveContainers found: {', '.join(candidates) or 'none'}",
            hint=PIN_HINT,
        )
    want = pinned.lower()
    exact = [b for b in candidates if b.lower() == want]
    if exact:
        # Two ids differing only in case cannot both be installed in practice;
        # if they ever are, the one spelled as pinned wins.
        return pinned if pinned in exact else exact[0]
    prefixed = [b for b in candidates if b.lower().startswith(want + ".")]
    if len(prefixed) == 1:
        return prefixed[0]
    raise LiveContainerError(
        f"lc_bundle_id {pinned!r} is not installed; LiveContainers found: "
        f"{', '.join(candidates) or 'none'}",
        hint=PIN_HINT,
    )


def _unpinned(
    roles: dict[str, str], candidates: list[str], team: str | None, free_account: bool
) -> str:
    if not candidates:
        raise LiveContainerError(
            "no LiveContainer found on the device",
            hint=FREE_SETUP_HINT if free_account else PAID_SETUP_HINT,
        )
    if len(candidates) == 1:
        return candidates[0]
    primaries = [
        b for b in candidates
        if roles[b] == "primary" or b.lower() == lc_instance.LC_ORIGINAL_ID
    ]
    if team:
        ours = lc_instance.instance_bundle_id(1, team).lower()
        matched = [b for b in primaries if b.lower() == ours]
        if len(matched) == 1:
            return matched[0]
    if len(primaries) == 1:
        return primaries[0]
    several = primaries or candidates
    raise LiveContainerError(
        f"{len(several)} LiveContainers are installed and Graft will not guess "
        f"which to use: {', '.join(several)}",
        hint=(
            f"pin the one you import from: set `lc_bundle_id: {several[0]}` "
            f"(or {' or '.join(several[1:])}) on this app in config.yaml"
        ),
    )
