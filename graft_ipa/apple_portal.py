"""App Store Connect API client for auto-provisioning.

Solves the "I have a wildcard profile that doesn't match this app's bundle id"
problem the same way SideStore/AltStore do: on demand, register a wildcard
Bundle ID matching the app's namespace, generate an iOS Development profile
against the user's cert + device(s), download the .mobileprovision, cache it.

Authentication uses the App Store Connect API key pair (.p8 + key_id + issuer_id).
Set up once in App Store Connect under Users and Access -> Integrations -> Keys.

API base: https://api.appstoreconnect.apple.com/v1
Auth: JWT (ES256) in Authorization: Bearer <token>, expires 20 min, regenerated per session.
"""

from __future__ import annotations

import base64
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import USER_AGENT, mobileprovision
from .errors import EXIT_PORTAL, NOT_LET_READ, GraftError, not_let_read_hint

# `requests` is imported inside `ASCClient.__init__` and `ASCClient._request`,
# and `jwt` inside `make_jwt` — not here; see those comments for the measured
# startup costs. This block is what keeps the `requests.Session` and
# `requests.Response` annotations below resolvable to a type checker (and to
# ruff's F821) without costing anything at runtime: `TYPE_CHECKING` is False
# in a running interpreter, so the body never executes. `jwt` is not in it
# because nothing in this file annotates a `jwt` type — its whole surface here
# is one `jwt.encode` call.
if TYPE_CHECKING:
    import requests

API_BASE = "https://api.appstoreconnect.apple.com/v1"
JWT_AUDIENCE = "appstoreconnect-v1"
JWT_LIFETIME_SECONDS = 1200  # 20 min max per Apple


class PortalError(GraftError):
    """Raised by App Store Connect API operations."""

    exit_code = EXIT_PORTAL


class AppIdNotRegistered(PortalError):
    """The exact App ID a caller needs does not exist, and must not be created.

    Raised only when `ensure_profile_for_bundle(register_missing=False)`. Its
    one caller is LiveContainer, whose App IDs are useless without an App Group
    assigned — and App Group assignment is not something the App Store Connect
    API can do. Registering the bare identifier would succeed and then sign an
    app whose keychain hook can never find its group, which is the defect this
    whole path exists to end. A subclass rather than a message to match, so the
    caller can fall back for an extension and stop for a main app.
    """


# ---------- data models ----------


@dataclass(frozen=True)
class BundleId:
    id: str
    identifier: str  # e.g. "com.example.ios.*" or "com.example.app"
    name: str
    platform: str  # "IOS" / "MAC_OS" / "UNIVERSAL"

    @property
    def is_wildcard(self) -> bool:
        return self.identifier.endswith("*")


@dataclass(frozen=True)
class Device:
    id: str
    udid: str
    name: str
    platform: str
    status: str  # "ENABLED" / "DISABLED"
    # IPHONE / IPAD / IPOD / APPLE_WATCH / APPLE_TV / MAC. `filter[platform]=IOS`
    # lists Watches and Apple TVs too; this is what tells them apart. Empty
    # when App Store Connect didn't say.
    device_class: str = ""


@dataclass(frozen=True)
class Certificate:
    id: str
    certificate_type: str  # IOS_DEVELOPMENT, IOS_DISTRIBUTION, ...
    display_name: str
    serial_number: str | None
    expiration: str | None
    # The certificate itself, base64 DER (`certificateContent`). Its subject's
    # OU is the team that issued it, which is the one way to learn the API
    # key's team before anything is written to the account; see
    # `certificate_team`. None when App Store Connect sent none.
    content_b64: str | None = None


@dataclass(frozen=True)
class Profile:
    id: str
    name: str
    profile_type: str  # IOS_APP_DEVELOPMENT, IOS_APP_STORE, ...
    uuid: str
    expiration: str | None
    profile_content_b64: str | None  # only present when fetched with field=profileContent


# ---------- JWT auth ----------

#: The most of a key file that is ever read. An App Store Connect key is a
#: PEM-wrapped P-256 private key, a few hundred bytes; anything past 64 KiB is
#: not one, and is not read into memory to find that out.
MAX_KEY_BYTES = 64 * 1024

_KEY_HINT = "choose the AuthKey_….p8 you downloaded from App Store Connect"


def read_key_file(path: Path) -> bytes:
    """The .p8 at `path`: a regular file, at most `MAX_KEY_BYTES`, read once.

    Opened non-blocking and checked with `fstat` on the descriptor it is then
    read from. `exists()` is true for a FIFO, and reading one with no writer
    blocks forever, which is what the setup wizard did; and a check on the
    path followed by a read of the path can be raced, while a check on the
    open descriptor cannot. `O_NONBLOCK` changes nothing for a regular file.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        raise PortalError(
            f"ASC API key not found at {path}",
            hint="download the .p8 from App Store Connect -> Users and Access -> Integrations -> Keys",
        ) from None
    except OSError as e:
        raise PortalError(
            f"couldn't open the App Store Connect key at {path}: {e.strerror}",
            hint=not_let_read_hint(str(path)) if e.errno in NOT_LET_READ else _KEY_HINT,
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise PortalError(
                f"{path} is not a file, so it isn't an App Store Connect key", hint=_KEY_HINT
            )
        chunks: list[bytes] = []
        size = 0
        while size <= MAX_KEY_BYTES:
            chunk = os.read(fd, MAX_KEY_BYTES + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
    finally:
        os.close(fd)
    if size > MAX_KEY_BYTES:
        raise PortalError(
            f"{path} is larger than 64 KiB, so it isn't an App Store Connect key",
            hint=_KEY_HINT,
        )
    return b"".join(chunks)


@dataclass
class PortalCredentials:
    key_id: str
    issuer_id: str
    key_path: Path
    #: The key itself, when the caller has already read it (`read_key_file`).
    #: Then what signs is exactly what was read, and the path is not read
    #: again: `--set-portal` checks these bytes and installs these bytes.
    #: Out of the repr: it is the private key, and a dataclass repr — in a
    #: traceback's locals, a log line, an f-string — showed it whole. The only
    #: field here that holds key material; the rest name the key.
    key_bytes: bytes | None = field(default=None, repr=False)

    def load_key(self) -> bytes:
        if self.key_bytes is not None:
            return self.key_bytes
        return read_key_file(self.key_path)


def make_jwt(creds: PortalCredentials) -> str:
    # Function-local, and the second half of the same argument the `requests`
    # imports in this file make. Deferring `requests` left ~18ms of its
    # subtree behind, because `jwt` reaches the same stdlib tail through
    # `jwt.jwks_client` -> `urllib.request` -> `http.client` -> `ssl`,
    # `email`. Moving this import is what actually collects it. Measured the
    # same way — one import toggled back to module scope, A/B blocks
    # alternated so both states ran under the same machine load, min of each
    # set:
    #
    #   `-X importtime -c "import graft_ipa.cli"`   101ms ->  83ms  (n=18)
    #   `graft --version`                       124ms -> 102ms  (n=24)
    #   `graft --devices --brief --json`        137ms -> 117ms  (n=24)
    #
    # ~20ms, on top of the ~28ms deferring `requests` bought — the same run,
    # three states interleaved, so the totals against unmodified master are
    # 152ms -> 102ms for `--version` and 166ms -> 117ms for the device poll.
    #
    # `jwt` is only ever needed to sign, and this is the only line in the
    # project that touches it — `apple_portal` is on the startup path through
    # `cli.py` -> `init_flow` -> `preflight` -> `signer` -> here, so before
    # this the device poll was regenerating nothing and still paying for the
    # library that signs. `tests/test_lazy_imports.py` holds the guard.
    #
    # At the top of the function for the same reason as this file's other two
    # lazy imports, though nothing here needs it as urgently: `jwt` appears in
    # no annotation and no `except` clause anywhere in the project, so this
    # one really is a single `encode` call. Kept in the same position so all
    # three read alike.
    import jwt

    now = int(time.time())
    payload = {
        "iss": creds.issuer_id,
        "iat": now,
        "exp": now + JWT_LIFETIME_SECONDS,
        "aud": JWT_AUDIENCE,
    }
    headers = {"alg": "ES256", "kid": creds.key_id, "typ": "JWT"}
    return jwt.encode(payload, creds.load_key(), algorithm="ES256", headers=headers)


# ---------- HTTP client ----------


class ASCClient:
    """Thin wrapper. Holds creds, regenerates JWT lazily, parses errors."""

    def __init__(self, creds: PortalCredentials, session: requests.Session | None = None) -> None:
        # Function-local, as in the other four modules that speak HTTP — see
        # `artwork._download_app_store_icon` for the measurement (~28ms off
        # every CLI invocation). This module is reached on startup through
        # `cli.py` -> `init_flow` -> `preflight` -> `signer` -> here, so it
        # has to move with the other four or none of them saves anything. The
        # `requests.Session` annotation on the signature above stays as it is;
        # see the `TYPE_CHECKING` block at the top of this file for why that
        # costs nothing.
        import requests

        self.creds = creds
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._token: str | None = None
        self._token_exp = 0

    def _auth_header(self) -> dict[str, str]:
        now = int(time.time())
        if self._token is None or now + 60 >= self._token_exp:
            self._token = make_jwt(self.creds)
            self._token_exp = now + JWT_LIFETIME_SECONDS
        return {"Authorization": f"Bearer {self._token}"}

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        # Function-local, exactly as in `__init__` above — see that comment.
        # Needed here in its own right and not only for the `self.session`
        # call: the `except requests.RequestException` below resolves the name
        # at the moment the exception is *caught*, so without this it would
        # raise `NameError` on top of whatever network failure it was meant to
        # report — and only ever on the failure path, where it would be least
        # welcome and least likely to be noticed in testing.
        import requests

        url = f"{API_BASE}{path}"
        headers = {**self._auth_header()}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            resp = self.session.request(
                method, url, params=params, json=json_body, headers=headers, timeout=30
            )
        except requests.RequestException as e:
            raise PortalError(f"App Store Connect request failed: {e}") from e

        if resp.status_code == 401:
            raise PortalError(
                "App Store Connect rejected the JWT (401)",
                hint=_extract_error_detail(resp)
                or "check key_id / issuer_id / .p8 file; the key may have been revoked",
            )
        if resp.status_code >= 400:
            detail = _extract_error_detail(resp)
            extra = ""
            if resp.status_code == 403:
                extra = (
                    " (the API key role may not be high enough; if `detail` doesn't "
                    "explain why, try regenerating the key with role=App Manager)"
                )
            raise PortalError(
                f"App Store Connect {resp.status_code} on {method} {path}{extra}",
                hint=detail or resp.text[:500],
            )
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # ---- Bundle IDs ----

    def list_bundle_ids(self, *, identifier: str | None = None, limit: int = 200) -> list[BundleId]:
        params: dict[str, Any] = {"limit": limit}
        if identifier:
            params["filter[identifier]"] = identifier
        data = self._request("GET", "/bundleIds", params=params)
        return [_bundle_id_from_resource(r) for r in data.get("data", [])]

    def create_bundle_id(self, identifier: str, name: str, *, platform: str = "IOS") -> BundleId:
        body = {
            "data": {
                "type": "bundleIds",
                "attributes": {
                    "identifier": identifier,
                    "name": name,
                    "platform": platform,
                },
            }
        }
        data = self._request("POST", "/bundleIds", json_body=body)
        return _bundle_id_from_resource(data["data"])

    # ---- Devices ----

    def list_devices(self, *, platform: str = "IOS") -> list[Device]:
        data = self._request(
            "GET",
            "/devices",
            params={"filter[platform]": platform, "limit": 200},
        )
        return [_device_from_resource(r) for r in data.get("data", [])]

    # ---- Certificates ----

    def list_certificates(
        self, *, certificate_type: str | list[str] | None = None
    ) -> list[Certificate]:
        """List certificates. Pass certificate_type=None for all.

        Apple's ASC API uses 'DEVELOPMENT' for newer accounts and 'IOS_DEVELOPMENT'
        for older ones. Pass a list to filter by multiple types.
        """
        params: dict[str, Any] = {"limit": 200}
        if certificate_type is None:
            pass
        elif isinstance(certificate_type, list):
            params["filter[certificateType]"] = ",".join(certificate_type)
        else:
            params["filter[certificateType]"] = certificate_type
        data = self._request("GET", "/certificates", params=params)
        return [_cert_from_resource(r) for r in data.get("data", [])]

    # ---- Profiles ----

    def list_profiles(self, *, profile_type: str = "IOS_APP_DEVELOPMENT") -> list[Profile]:
        data = self._request(
            "GET",
            "/profiles",
            params={"filter[profileType]": profile_type, "limit": 200},
        )
        return [_profile_from_resource(r) for r in data.get("data", [])]

    def get_profile(self, profile_id: str, *, with_content: bool = True) -> Profile:
        params: dict[str, Any] = {}
        if with_content:
            params["fields[profiles]"] = (
                "name,uuid,expirationDate,profileContent,profileType"
            )
        data = self._request("GET", f"/profiles/{profile_id}", params=params)
        return _profile_from_resource(data["data"])

    def create_profile(
        self,
        *,
        name: str,
        bundle_id: BundleId,
        certificate_ids: list[str],
        device_ids: list[str],
        profile_type: str = "IOS_APP_DEVELOPMENT",
    ) -> Profile:
        body = {
            "data": {
                "type": "profiles",
                "attributes": {
                    "name": name,
                    "profileType": profile_type,
                },
                "relationships": {
                    "bundleId": {"data": {"type": "bundleIds", "id": bundle_id.id}},
                    "certificates": {
                        "data": [{"type": "certificates", "id": c} for c in certificate_ids]
                    },
                    "devices": {
                        "data": [{"type": "devices", "id": d} for d in device_ids]
                    },
                },
            }
        }
        data = self._request("POST", "/profiles", json_body=body)
        return _profile_from_resource(data["data"])

    def delete_profile(self, profile_id: str) -> None:
        self._request("DELETE", f"/profiles/{profile_id}")


# ---------- resource parsers ----------


def _bundle_id_from_resource(r: dict[str, Any]) -> BundleId:
    a = r.get("attributes", {})
    return BundleId(
        id=r["id"],
        identifier=a.get("identifier", ""),
        name=a.get("name", ""),
        platform=a.get("platform", ""),
    )


def _device_from_resource(r: dict[str, Any]) -> Device:
    a = r.get("attributes", {})
    return Device(
        id=r["id"],
        udid=a.get("udid", ""),
        name=a.get("name", ""),
        platform=a.get("platform", ""),
        status=a.get("status", ""),
        device_class=a.get("deviceClass", "") or "",
    )


def _cert_from_resource(r: dict[str, Any]) -> Certificate:
    a = r.get("attributes", {})
    return Certificate(
        id=r["id"],
        certificate_type=a.get("certificateType", ""),
        display_name=a.get("displayName", ""),
        serial_number=a.get("serialNumber"),
        expiration=a.get("expirationDate"),
        content_b64=a.get("certificateContent"),
    )


def _profile_from_resource(r: dict[str, Any]) -> Profile:
    a = r.get("attributes", {})
    return Profile(
        id=r["id"],
        name=a.get("name", ""),
        profile_type=a.get("profileType", ""),
        uuid=a.get("uuid", ""),
        expiration=a.get("expirationDate"),
        profile_content_b64=a.get("profileContent"),
    )


def _extract_error_detail(resp: requests.Response) -> str | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    errs = data.get("errors") or []
    if not errs:
        return None
    parts = []
    for e in errs:
        title = e.get("title") or ""
        detail = e.get("detail") or ""
        parts.append(f"{title}: {detail}".strip(": "))
    return " | ".join(parts)


# ---------- high-level provisioning ----------


# Match bundle id segments. We treat the prefix-up-to-the-last-segment as the
# namespace and use a wildcard at that level. e.g.
#   com.example.ios.app              -> com.example.ios.*
#   com.delta.app.IntentExt  -> com.delta.*
#   com.charlie.client                  -> com.charlie.*
#
# We anchor on the *vendor* segment because one app's plugins live under
# com.delta.app.<thing>, and a wildcard com.delta.app.*
# would NOT cover com.delta.app (the main app). com.delta.* covers all.
_NAMESPACE_RE = re.compile(r"^([a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+)(?:\..*)?$")


def derive_wildcard_for(bundle_id: str) -> str:
    """Return the wildcard pattern that covers a bundle id and any extensions.

    Examples:
        com.example.ios.app              -> com.example.*
        com.delta.app            -> com.delta.*
        com.delta.app.IntentExt  -> com.delta.*
    """
    m = _NAMESPACE_RE.match(bundle_id)
    if not m:
        # Last resort: drop the final dot-segment.
        head = bundle_id.rsplit(".", 1)[0]
        return f"{head}.*"
    return f"{m.group(1)}.*"


@dataclass
class ProvisionResult:
    profile_path: Path
    profile_id: str
    bundle_id_pattern: str  # the wildcard registered/used, e.g. "com.example.*"
    refreshed: bool  # True if we created a new profile this call
    # Why a cached profile was not reused, when that is what triggered the
    # refresh. None for a caller-forced refresh or a plain cache miss.
    refresh_reason: str | None = None
    # Configured UDIDs App Store Connect does not have enabled, and which this
    # profile therefore does not cover. Carried rather than raised: a phone the
    # user has replaced leaves its UDID in config.yaml, and refusing to build
    # for a device they no longer own would break a working setup over a stale
    # line. The build says so instead — see `signer.run_zsign`.
    unregistered_devices: tuple[str, ...] = ()


def certificate_team(cert: Certificate) -> str | None:
    """The team `cert` was issued to (its subject's OU), or None when App Store
    Connect sent no readable certificate.

    A certificate this key lists is the key's team's, so this is the key's
    team, known from a read alone.
    """
    if not cert.content_b64:
        return None
    from cryptography import x509
    from cryptography.x509.oid import NameOID

    try:
        parsed = x509.load_der_x509_certificate(base64.b64decode(cert.content_b64))
    except (TypeError, ValueError):
        return None
    units = parsed.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    return str(units[0].value) if units else None


def _profile_team(profile: Profile) -> str | None:
    """The team a profile App Store Connect sent belongs to, read in memory,
    or None when it can't be read or names none."""
    if not profile.profile_content_b64:
        return None
    try:
        plist = mobileprovision.decode_bytes(
            base64.b64decode(profile.profile_content_b64), source=f"profile {profile.id}"
        )
    except (GraftError, TypeError, ValueError):
        return None
    teams = plist.get("TeamIdentifier") or []
    return str(teams[0]) if teams else None


def _refuse_another_team(
    team_id: str | None,
    key_team: str | None,
    *,
    done: str = "Nothing was created or deleted at Apple.",
) -> None:
    """Stop when signing.team_id names a team other than the API key's.

    A key can only make profiles for its own team, and `cache_problem`'s team
    rule refuses every one of them for another `team_id`. Left to run, each
    build created a profile, refused it, and left it in the account: one more
    per build, and nothing ever built. Asked as soon as the key's team is
    known, so the refusal comes before any write.
    """
    if team_id is None or key_team is None or team_id.upper() == key_team.upper():
        return
    raise PortalError(
        f"signing.team_id {team_id} doesn't match the App Store Connect key's team {key_team}",
        hint=(
            f"the key and the .p12's certificate are team {key_team}'s, and every "
            f"profile it makes is {key_team}'s, which a build for {team_id} refuses. "
            f"If you moved to team {key_team}, set Team ID to {key_team} in Settings "
            f"(or `graft --set-settings --signing-team-id {key_team}`); if not, set "
            f"up team {team_id}'s App Store Connect key and .p12. {done}"
        ),
    )


def _cache_filename(pattern: str) -> str:
    # Make a filesystem-safe filename from a bundle id pattern.
    return pattern.replace("*", "x").replace(".", "_") + ".mobileprovision"


def profile_cache_path(cache_dir: Path, pattern: str) -> Path:
    """Where the profile for `pattern` is cached. The naming rule, shared.

    Public because `signer.plan_profile_for_app` needs the same answer to say
    which file a build will use, and was reaching into `_cache_filename` to get
    it. A second copy of the rule would let pre-flight validate one file while
    `ensure_profile_for_bundle` writes another.
    """
    return cache_dir / _cache_filename(pattern)


def _cache_if_usable(
    profile: Profile,
    cached: Path,
    device_udids: list[str],
    pattern: str,
    *,
    cert_serial: str | None = None,
    team_id: str | None = None,
) -> str | None:
    """Write `profile` to `cached` if it is usable, or say why it is not.

    Returns None once the file is in place, and the `cache_problem` sentence
    otherwise, leaving whatever was at `cached` untouched. Staged through a
    sibling so a rejected download never becomes the cache — the next build
    would treat it as a miss and re-provision, which is how the every-build
    round trip below starts.

    The point is that "when is a profile good?" keeps having exactly one
    answer. It had grown two more: the profile ASC hands back from
    `get_profile` on a cache miss was reused unchecked, and the profile
    `create_profile` returns was written back unchecked — so a profile created
    before the phone existed passed every pre-flight row and was refused by the
    phone, and a profile that could not resolve the problem it was created for
    was re-created on every single build.
    """
    if not profile.profile_content_b64:
        raise PortalError("profile fetched but ASC returned no profileContent")
    staged = cached.with_name(cached.name + ".new")
    staged.write_bytes(base64.b64decode(profile.profile_content_b64))
    staged.chmod(0o600)
    problem = mobileprovision.cache_problem(
        staged, device_udids=device_udids, application_identifier=pattern,
        cert_serial=cert_serial, team_id=team_id,
    )
    if problem is not None:
        staged.unlink(missing_ok=True)
        return problem
    staged.replace(cached)
    return None


def ensure_profile_for_bundle(
    client: ASCClient,
    *,
    bundle_id: str,
    cert_serial: str,
    device_udids: list[str],
    cache_dir: Path,
    force_refresh: bool = False,
    exact: bool = False,
    register_missing: bool = True,
    team_id: str | None = None,
) -> ProvisionResult:
    """Return a path to a .mobileprovision that covers `bundle_id`.

    Steps:
      1. Compute namespace wildcard (com.example.* etc), or use exact bundle_id
         if `exact=True` (needed for hosts like LiveContainer whose runtime
         features require the application-identifier entitlement to be exact
         rather than a wildcard).
      2. Resolve the cert, and refuse a `team_id` that is not its team (the
         key's) before anything is written to the account.
      3. Find the Bundle ID resource whose identifier *equals* that pattern,
         or create it — unless `register_missing=False`, which raises
         `AppIdNotRegistered` instead (see that class for why).
      4. Resolve the devices, refusing a UDID ASC does not know.
      5. Reuse or create a profile, and cache it only once it passes the rule.
         A name-matched remote profile is read and judged before it is
         replaced, never deleted unread.

    The cache heals itself. `force_refresh` is the caller's override, not the
    only way to get a fresh profile: an expired cached file, or one that does
    not list a device the build needs, is treated as a miss. It used to be
    returned unconditionally on the strength of a comment claiming "signer.py
    validates expiry upstream" — which signer.py has never done. What did check
    was app-aware pre-flight, which runs before stage 4 and aborted the build,
    so once this cache expired every build of that app failed with no in-product
    way to refresh it. Deciding here rather than at the call site is what keeps
    "when is a cached profile good?" from having two answers.

    It had grown two more anyway, both on the paths nothing asked the question
    about. `cache_problem` judged the local file and *only* the local file: the
    pre-existing remote profile reused on a cache miss was written straight
    through, and so was every profile created here — so a refresh that could
    not resolve the condition it was triggered by simply happened again on the
    next build, and the next. Nothing that lands in the cache now avoids the
    rule, and a profile that fails it stops the build with a reason instead of
    becoming the next build's problem.

    WHICH DEVICES THE RULE IS ASKED ABOUT is the other half of that, and it is
    what actually caused the loop. A UDID App Store Connect does not have
    cannot appear in any profile it issues, so judging the cache against the
    full configured list asks a question no refresh can answer: the file comes
    back still missing it, `cache_problem` says so again, and the account is
    deleted and recreated on every single build, forever. Everything from step
    3 down is therefore judged against configured ∩ registered — the devices a
    new profile could actually cover.

    The first check, before any of that, still uses the full list, because it
    runs offline: a cache good for every configured UDID is good for the subset
    too, and returning it there is what keeps a usable cache free of HTTP. Only
    when it reports a problem is the narrower question worth the round trip,
    and if the narrower answer is "no problem" the cached file is returned
    untouched. So the retired-phone case costs read-only calls and a warning
    rather than two writes to the user's developer account.

    Every one of those judgements, cached file and remote alike, also asks
    whether the profile lists the certificate `cert_serial` names and belongs
    to `team_id` (when given). A certificate revoked or replaced, or a move to
    another team, otherwise left a profile zsign can't use in the cache, and
    deleting the file only fetched the same remote profile back.
    """
    if exact:
        pattern = bundle_id
    else:
        pattern = derive_wildcard_for(bundle_id)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cached = profile_cache_path(cache_dir, pattern)
    # Why the cache was bypassed, carried out on the result so the build can say
    # it. A round trip to Apple that happens for no stated reason is the kind of
    # thing a user later has to reverse-engineer from a network log.
    refresh_reason: str | None = None
    # Whether the *caller* asked for a refresh, as opposed to the cache check
    # below deciding one is needed. Only the latter is worth re-asking once the
    # registered device list is known.
    caller_forced = force_refresh

    if cached.exists() and not force_refresh:
        problem = mobileprovision.cache_problem(
            cached, device_udids=device_udids, application_identifier=pattern,
            cert_serial=cert_serial, team_id=team_id,
        )
        if problem is None:
            return ProvisionResult(
                profile_path=cached,
                profile_id="(cached)",
                bundle_id_pattern=pattern,
                refreshed=False,
            )
        # Not the caller's override: the remote profile of the same name is
        # read and judged below before anything replaces it. For an expired
        # file it is usually the one the file came from, and is refused the
        # same way; for a file from another team, or one lacking this Mac's
        # certificate, it says nothing about the remote one, which another Mac
        # may have made and be using.
        refresh_reason = problem

    # Step 2: resolve our certificate id by serial number, before anything is
    # written. The certificate is the key's team's, so this is also where a
    # signing.team_id no profile from this key can satisfy is refused.
    # Apple's ASC API uses 'DEVELOPMENT' for newer accounts, 'IOS_DEVELOPMENT' for older.
    # Search both so we don't miss the cert.
    certs = client.list_certificates(certificate_type=["DEVELOPMENT", "IOS_DEVELOPMENT"])
    cert = _find_cert_by_serial(certs, cert_serial)
    if cert is None:
        raise PortalError(
            f"no IOS_DEVELOPMENT cert in ASC matching serial {cert_serial}",
            hint=(
                "verify the cert is registered: developer.apple.com -> Certificates. "
                "If it was created locally and never uploaded, regenerate it from Xcode "
                "while signed into the same Apple ID."
            ),
        )
    _refuse_another_team(team_id, certificate_team(cert))

    # Step 3: locate or create the Bundle ID resource.
    #
    # The identifier that *equals* the pattern, never the first one returned.
    # ASC's `filter[identifier]` is a prefix match: asked for
    # `com.kdt.livecontainer.ABCDE12345` it also returns `….LiveProcess`, and
    # on the account this was found on it returned that one first — so
    # `matches[0]` built the main app's profile against an extension's App ID.
    matches = client.list_bundle_ids(identifier=pattern)
    # Case-insensitively: Apple treats bundle ids that way, so a configured id
    # spelled with different case is the same App ID, and registering it again
    # would be a duplicate or a 409.
    bid: BundleId | None = next(
        (b for b in matches if b.identifier.lower() == pattern.lower()), None
    )
    if bid is None and not register_missing:
        raise AppIdNotRegistered(
            f"App ID {pattern} is not registered in this account",
            hint=(
                "register it at developer.apple.com -> Identifiers with the App "
                "Groups capability and assign its group there; App Group "
                "assignment cannot be done through the App Store Connect API"
            ),
        )
    if bid is None:
        # Apple's "name" field is human-shown and must be alphanumeric + spaces.
        # No dots, no asterisks. So rewrite "com.example.*" -> "graft com example".
        safe_name = "graft " + re.sub(r"[^a-zA-Z0-9]+", " ", pattern).strip()
        bid = client.create_bundle_id(pattern, name=safe_name)

    # Step 4: resolve device IDs, and narrow the question to the devices a
    # profile could actually cover.
    all_devices = client.list_devices(platform="IOS")
    udid_to_id = {d.udid.lower(): d.id for d in all_devices if d.status == "ENABLED"}
    provisionable = [u for u in device_udids if u.lower() in udid_to_id]
    unregistered = tuple(u for u in device_udids if u.lower() not in udid_to_id)
    device_ids = [udid_to_id[u.lower()] for u in provisionable]
    if not device_ids:
        raise PortalError(
            "no enabled iOS devices in ASC match the configured UDIDs",
            hint=f"configured: {device_udids} ; in ASC: {list(udid_to_id)}",
        )

    if unregistered and not caller_forced and cached.exists():
        # The loop, at its cause. An unregistered UDID cannot appear in any
        # profile ASC issues, so a `cache_problem` answer that names one is a
        # problem no refresh can resolve: the new profile came back missing it,
        # the next build asked the same question, got the same answer, and
        # deleted and recreated the account's profile again — every build,
        # forever, with a network failure between the two calls leaving the
        # account with no profile at all.
        #
        # Asked again against the devices that can actually be provisioned for.
        # If nothing is wrong with the cache once the impossible device is out
        # of the question, the file was fine all along and is returned
        # untouched. The alternative considered here was refusing to build at
        # all, and it is worse in the ordinary case: replacing a phone leaves
        # its UDID in config.yaml, and a build that stops working for a device
        # the user no longer owns is a bad trade for a warning they can act on.
        problem = mobileprovision.cache_problem(
            cached, device_udids=provisionable, application_identifier=pattern,
            cert_serial=cert_serial, team_id=team_id,
        )
        if problem is None:
            return ProvisionResult(
                profile_path=cached,
                profile_id="(cached)",
                bundle_id_pattern=pattern,
                refreshed=False,
                unregistered_devices=unregistered,
            )
        # More accurate than the answer the first check gave, which named a
        # device this refresh was never going to add.
        refresh_reason = problem

    # Step 5: find a usable existing profile, otherwise create a new one.
    # Apple rejects duplicate names so we either reuse or generate a unique name.
    #
    # Profiles made before the rename to Graft are called `tweak-build <id>
    # dev`. They are still ours, and recognizing only the new name would have
    # every existing install create a second profile per app on its first
    # build after upgrading, leaving the old one orphaned in the account. New
    # profiles get the new name; the old one is only ever matched.
    profile_name_prefix = f"graft {pattern} dev"
    ours = (profile_name_prefix, f"tweak-build {pattern} dev")
    existing = client.list_profiles(profile_type="IOS_APP_DEVELOPMENT")
    # The new name first when both survive (a refresh that could not delete
    # the old one, two machines on one account mid-upgrade): the order ASC
    # lists them in must not decide which one this build maintains. Stable,
    # so within each name ASC's order still holds.
    candidate = next(
        (
            p for p in sorted(existing, key=lambda p: not p.name.startswith(profile_name_prefix))
            if any(p.name == prefix or p.name.startswith(prefix + " ") for prefix in ours)
        ),
        None,
    )
    # Whether the candidate was read and refused: the only grounds, short of
    # the caller's override, for deleting it.
    candidate_refused = False
    if candidate is not None and not caller_forced:
        # The remote profile is a cache too, and it was the one place the
        # validity rule was never applied. This is the *only* path on a fresh
        # machine — or right after the user deletes the local file, which is
        # the remedy the old dead end trained them into — so a profile ASC
        # created before this phone existed came down here, passed every green
        # pre-flight row, and was refused by the phone. Only the next build,
        # judging the file it had written, healed it.
        #
        # Asked on a refresh too. It was skipped whenever the local file had a
        # problem, and then deleted unread once the replacement existed: a
        # valid profile in the new team after a team switch, or the one
        # another Mac had just refreshed, went with it.
        downloaded = client.get_profile(candidate.id, with_content=True)
        # A profile this key lists is the key's team's: the same refusal as
        # step 2's, for when App Store Connect sent the certificate bare.
        _refuse_another_team(team_id, _profile_team(downloaded))
        problem = _cache_if_usable(
            downloaded, cached, provisionable, pattern,
            cert_serial=cert_serial, team_id=team_id,
        )
        if problem is None:
            return ProvisionResult(
                profile_path=cached,
                profile_id=downloaded.id,
                bundle_id_pattern=pattern,
                refreshed=True,
                refresh_reason=refresh_reason,
                unregistered_devices=unregistered,
            )
        # The reason the refresh began, when the local file gave one: that is
        # the question the user has, and the remote one usually shares it.
        candidate_refused, refresh_reason = True, refresh_reason or problem

    unique_name = (
        profile_name_prefix
        if not any(p.name == profile_name_prefix for p in existing)
        else f"{profile_name_prefix} {int(__import__('time').time())}"
    )
    profile = client.create_profile(
        name=unique_name,
        bundle_id=bid,
        certificate_ids=[cert.id],
        device_ids=device_ids,
    )
    if not profile.profile_content_b64:
        profile = client.get_profile(profile.id, with_content=True)
    problem = _cache_if_usable(
        profile, cached, provisionable, pattern,
        cert_serial=cert_serial, team_id=team_id,
    )
    if problem is not None:
        # Removed again first: it is ours, minutes old, and refused, so all it
        # could do in the account is accumulate, one per build.
        try:
            client.delete_profile(profile.id)
        except PortalError:
            pass
        # The last place the key's team can show itself, when neither the
        # certificate nor a listed profile said.
        _refuse_another_team(
            team_id, _profile_team(profile),
            done="The profile it just made was deleted again.",
        )
        # The new profile does not resolve what the old one was replaced for.
        # Refreshing on the strength of `cache_problem` without ever asking it
        # about the result is what let an unresolvable condition re-provision
        # the account on every build. Step 3 removes the one cause that could
        # never be resolved — a device ASC does not have — so anything reaching
        # here is a condition a second attempt would not fix either: an expiry
        # already in the past, a profile ASC built for other devices entirely.
        # Stopping with the reason beats handing the next build the same file.
        raise PortalError(
            f"the profile App Store Connect just created is still unusable: {problem}",
            hint=(
                "this build would have re-provisioned the account again next "
                "time for the same reason. Check the certificate and devices "
                "on the profile at developer.apple.com -> Profiles."
            ),
        )

    # Deleted only now. Recreating meant deleting first, so a network failure
    # landing between the two calls left the account with no profile at all;
    # this way the worst case is a stale extra, which the check above rejects
    # and the next refresh clears. And only one that was read and refused, or
    # that the caller asked to replace.
    if candidate is not None and (caller_forced or candidate_refused):
        try:
            client.delete_profile(candidate.id)
        except PortalError:
            pass

    return ProvisionResult(
        profile_path=cached,
        profile_id=profile.id,
        bundle_id_pattern=pattern,
        refreshed=True,
        refresh_reason=refresh_reason,
        unregistered_devices=unregistered,
    )


def _find_cert_by_serial(certs: list[Certificate], serial: str) -> Certificate | None:
    """The certificate whose serial is `serial`, compared as numbers.

    ASC may print a serial as '00:11:22' or '001122', and `read_p12_cert_serial`
    as '1122': `format(n, 'X')` drops leading zero nibbles, which a byte-padded
    rendering keeps. Compared as strings those never matched, which would fail
    about one serial in sixteen; `mobileprovision.serial_value` makes every
    spelling of one number equal.
    """
    target = mobileprovision.serial_value(serial)
    if target is None:
        return None
    for c in certs:
        if c.serial_number and mobileprovision.serial_value(c.serial_number) == target:
            return c
    return None


# ---------- helper: read the .p12's serial number so the user doesn't have to ----------


def read_p12_cert_serial(p12_path: Path, password: str) -> str:
    """Return the X.509 serial number of the cert inside a .p12 (matches ASC).

    ASC's Certificate resource exposes `serialNumber` as the X.509 serial,
    formatted as a hex string. This function reads the .p12, extracts the cert,
    and returns the serial in the same hex form (no separators, uppercase).
    """
    from .p12 import load_key_and_certificates

    data = p12_path.read_bytes()
    try:
        _, cert, _ = load_key_and_certificates(data, password.encode("utf-8"))
    except ValueError as e:
        raise PortalError(
            f"could not read {p12_path}: {e}",
            hint="wrong password, or the .p12 was exported in an unsupported format",
        ) from e
    if cert is None:
        raise PortalError(f"{p12_path} contains no certificate")
    return format(cert.serial_number, "X")  # uppercase hex, no separators
