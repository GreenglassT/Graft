"""What `import graft_ipa.cli` is allowed to cost, and what still works after.

Two halves that only mean something together.

The first half is a startup guard: after `import graft_ipa.cli`, neither
`requests` nor `jwt` may be in `sys.modules` (nor `pymobiledevice3`, which was
already lazy and had no test saying so). Measured end to end by toggling one
module-scope import back on at a time and alternating A/B blocks, so both
states ran under the same machine load — min of each set:

                                            master  -requests  -jwt too
    `-X importtime -c "import graft_ipa.cli"` 128 ->    101 ->    83ms
    `graft --version`, which does no work 152 ->    124 ->   102ms
    `graft --devices --brief --json`      166 ->    137 ->   117ms

~50ms off every invocation of the CLI, including the `--devices --brief
--json` poll the GUI runs every five seconds, which never opens an HTTP
connection and never signs anything.

The two are one change in two halves, and the arithmetic says why: `requests`
costs 57ms to import alone but deferring it only bought ~28ms, because ~18ms
of its subtree (`urllib.request` -> `http.client` -> `ssl`, `email`) was still
reached by `jwt` through `jwt.jwks_client`. Deferring `jwt` collects that tail
rather than removing 41ms of PyJWT.

Five modules speak HTTP (`artwork`, `apple_portal`, `canister`, `itunes`,
`repo`) and `cli.py` imports every one of them at module scope, so this is a
property of the set rather than of any one file: a module-scope `import
requests` in any one of the five puts the whole cost back for all five.
A per-module test could not see that; asserting on the CLI's own `sys.modules`
can.

The second half is a functional test for each of the eight places an import
moved to. The startup guard on its own is satisfied by deleting `requests`
from this project entirely, which is why it cannot be the only test here: it
proves the import is absent, and these prove the code that needs it still
gets it. Every site with an `except requests.<E>` clause is exercised through
that clause specifically, because that is the failure this refactor could
plausibly introduce — an `except` names the module at the moment the exception
is *caught*, so a missing local import raises `NameError` on top of whatever
network trouble it was supposed to report, on the failure path only, where it
is least likely to be noticed.
"""

from __future__ import annotations

import base64
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from graft_ipa import apple_portal, artwork, canister, itunes, repo
from graft_ipa.apple_portal import ASCClient, PortalCredentials, PortalError
from graft_ipa.canister import CanisterError
from graft_ipa.errors import ITunesError, RepoError
from graft_ipa.itunes import ITunesApp

_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Top-level packages the CLI's own import must not pull in, each with the
#: rule that makes it a rule rather than an accident. Keyed by import name; a
#: submodule counts (`pymobiledevice3.lockdown` is the expensive one).
_MUST_STAY_LAZY = ("requests", "jwt", "pymobiledevice3")

_PROBE = (
    "import sys, graft_ipa.cli; "
    "print(graft_ipa.cli.__file__); "
    "[print(n, any(m == n or m.startswith(n + '.') for m in sys.modules)) "
    f"for n in {_MUST_STAY_LAZY!r}]"
)


def _startup_imports() -> dict[str, bool]:
    """Run `_PROBE` in a clean interpreter against *this* checkout.

    A subprocess is the only way to observe this: the import is a module-load
    side effect, and pytest's own process has already imported `requests`
    (this file does, at the top) long before any test could look.

    `cwd` is pinned to the repo root rather than inherited because this
    project is normally installed editable via pipx from a different checkout,
    and several worktrees of it exist at once. Without it, a green result here
    could be measuring somebody else's `cli.py`. The first line the probe
    prints is that file's path, and it is checked, so the test cannot pass by
    profiling the wrong source tree.
    """
    out = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert out.returncode == 0, f"the probe interpreter failed:\n{out.stderr}"
    cli_file, *rows = out.stdout.strip().splitlines()
    assert Path(cli_file).resolve() == (_REPO_ROOT / "graft_ipa" / "cli.py").resolve(), (
        f"the probe imported {cli_file} rather than this worktree's cli.py — the numbers "
        "and the verdict would both be about a different checkout"
    )
    imported = dict(row.split(" ") for row in rows)
    assert set(imported) == set(_MUST_STAY_LAZY), f"probe output does not parse: {rows}"
    return {name: value == "True" for name, value in imported.items()}


def test_importing_the_cli_does_not_import_requests() -> None:
    """An eager `requests` measured at ~28ms of the CLI's startup (see the
    module docstring for the A/B), and the GUI pays it every five seconds on a
    device poll that makes no HTTP request at all.

    A failure here does not mean a test is wrong; it means a module-scope
    `import requests` came back somewhere in the five modules `cli.py` imports
    (`artwork`, `apple_portal`, `canister`, `itunes`, `repo`),
    and every CLI invocation is now ~28ms slower than it needs to be.
    """
    assert _startup_imports()["requests"] is False, (
        "importing graft_ipa.cli pulled `requests` into sys.modules. That is "
        "~28ms added to every invocation of the CLI, including the "
        "`--devices --brief --json` poll the GUI runs every five seconds and "
        "which never speaks HTTP. Something in artwork/apple_portal/canister/"
        "itunes/repo imports `requests` at module scope again — move it back "
        "inside the function that calls it. All five have to stay lazy "
        "together; one of them undoes the saving for the other four."
    )


def test_importing_the_cli_does_not_import_jwt() -> None:
    """`jwt` is needed to sign, and only to sign.

    It is one `jwt.encode` call in `apple_portal.make_jwt` — the only line in
    the project that touches the library — but `apple_portal` is reached on
    startup through `cli.py` -> `init_flow` -> `preflight` -> `signer`, so an
    eager import there is paid by every invocation. Worth ~20ms of the CLI's
    startup on its own (see `make_jwt`'s comment for the A/B), most of it the
    stdlib tail `jwt.jwks_client` reaches through `urllib.request` ->
    `http.client` -> `ssl`, `email` — the same tail deferring `requests` left
    behind, which is why the two changes together are worth more than either
    alone.

    That is the rule, and it is one anybody can restate: the device poll must
    not pay for the library that signs.
    """
    assert _startup_imports()["jwt"] is False, (
        "importing graft_ipa.cli pulled `jwt` into sys.modules — ~20ms added "
        "to every invocation for a library only the signing path uses, the "
        "`--devices --brief --json` poll the GUI runs every five seconds "
        "included. `apple_portal.py` has `import jwt` at module scope again; "
        "move it back inside `make_jwt`, its only caller."
    )


def test_importing_the_cli_does_not_import_pymobiledevice3() -> None:
    """The third import this file guards, and the reason it is here rather
    than left to `test_artwork_sources.py`.

    `pymobiledevice3.lockdown` measured at 210ms to import (min of 5 runs of
    `python -X importtime`) — more than the whole rest of the CLI put
    together. `cli.py`'s `_do_device_info` already argues in prose that the
    import stays function-local and that `pymobiledevice3` is absent from
    `sys.modules` after `import graft_ipa.cli`; nothing tested that claim at
    the `cli.py` level. `test_artwork_sources.py` covers `artwork` alone, so a
    regression anywhere else in the graph — `device_apps`, `devices`,
    `preflight`, a new module — was invisible.

    This guards the *import* graph, and is not a claim that no verb pays for
    `pymobiledevice3` at run time. Several do, on purpose: `devices.py`,
    `device_apps.py` and `artwork.py`'s device rung all import it inside an
    `async def`, so any verb that actually talks to a phone pays for it then.
    What the laziness buys is that verbs which do not talk to a phone pay
    nothing. Measured per verb with `-X importtime -m graft_ipa.cli <verb>`,
    reading the cumulative column, with a phone attached:

        `--version`, `--list --json`, `--check --json`   nothing
        `--devices --brief --json`   pymobiledevice3.usbmux        ~10ms
        `--device-info --json`       pymobiledevice3.lockdown     ~158ms
        `--devices --json`           pymobiledevice3.lockdown     ~244ms

    The GUI's five-second poll is the `--brief` one, and it reaches `usbmux`
    only — never `lockdown`. That is the whole reason the poll costs what it
    does, and why ~50ms off its startup is a large fraction of it rather than
    a rounding error.

    Two things worth knowing rather than rediscovering, neither of them
    fixable by moving an import of ours. Inside that `lockdown` cost,
    `pymobiledevice3.utils` imports **IPython** (~106ms of the 244ms) —
    a REPL, pulled in to describe a USB device. And it imports `requests`
    (~44ms) as well, so on `--devices --json` and `--device-info --json`
    specifically, `requests` comes back regardless of what this project does:
    the saving these guards protect is real for `--version`, `--list`,
    `--check` and the five-second poll, and is swallowed on the two verbs
    that open a lockdown session. Only a narrower entry point into the
    library, or an upstream change, could recover it.

    Deliberately not extended past these three. `typer` (24ms) and `rich` are
    what the CLI *is*. `frida` (37ms) and `paramiko` happen to be absent too,
    but nothing in the code claims they must be, and a test that pins an
    accident reads to the next person as a rule nobody can explain. Guard what
    the code argues for, and add to this list when the code argues for more.
    """
    assert _startup_imports()["pymobiledevice3"] is False, (
        "importing graft_ipa.cli pulled `pymobiledevice3` into sys.modules — "
        "~210ms added to every invocation, `--version` included. Some module "
        "cli.py imports now has `import pymobiledevice3...` at module scope; "
        "move it inside the function that opens the lockdown session, as "
        "devices.py, device_apps.py and artwork.py all do."
    )


# --- the eight sites an import moved into ------------------------------------
#
# Ordered by module, and by the order the sites appear inside each module.
# `requests.get` is patched at the library rather than at `graft_ipa.<mod>.
# requests`, which is what these tests used to say: the modules no longer hold
# a `requests` attribute to reach through. The object being replaced is the
# same one either way — the old target resolved to this module too.


def _resp(
    status: int = 200, *, payload: object = None, body: bytes = b"", text: str = ""
) -> MagicMock:
    """A `requests.Response` stand-in that answers every access these call
    sites make: status, JSON, text, headers, streamed chunks, and the context
    manager `canister.fetch_deb` uses."""
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.content = body
    r.text = text
    r.headers = {"content-type": "application/octet-stream"}
    r.iter_content = MagicMock(return_value=iter([body] if body else []))
    r.raise_for_status = MagicMock()
    r.__enter__ = MagicMock(return_value=r)
    r.__exit__ = MagicMock(return_value=False)
    return r


# --- artwork._download_app_store_icon ---

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_artwork_app_store_rung_still_reaches_requests() -> None:
    """The one site with no `except requests.<E>` clause — and the one where a
    broken import would be quietest, because the whole body sits inside
    `except Exception: return None`. That is why this asserts the bytes come
    back rather than merely that nothing raised: "no icon" is what this rung
    returns when it is working correctly *and* when it is broken.
    """
    app = ITunesApp(
        track_id=1, bundle_id="com.bravo.app", name="Bravo", version="1",
        artwork_url="https://example.invalid/512.png",
    )
    with (
        patch.object(artwork, "lookup_by_bundle_id", return_value=app),
        patch("requests.get", return_value=_resp(body=_PNG)) as mock_get,
    ):
        assert artwork.icon_from_app_store("com.bravo.app") == _PNG
    assert mock_get.call_args.kwargs["stream"] is True


# --- itunes._query ---

_LOOKUP_OK = {
    "resultCount": 1,
    "results": [
        {
            "trackId": 100000001,
            "bundleId": "com.bravo.app",
            "trackName": "Bravo",
            "version": "2025.20.0",
        }
    ],
}


def test_itunes_query_still_reaches_requests() -> None:
    with patch("requests.get", return_value=_resp(payload=_LOOKUP_OK)):
        app = itunes.lookup(100000001)
    assert app.bundle_id == "com.bravo.app"


def test_itunes_query_still_catches_a_request_exception() -> None:
    """The `except` clause, not the call — see the module docstring."""
    with patch("requests.get", side_effect=requests.RequestException("timed out")):
        with pytest.raises(ITunesError, match="iTunes lookup request failed: timed out"):
            itunes.lookup(100000001)


# --- repo._http_get ---

_PACKAGES = "Package: com.example.alpha\nVersion: 1.0.0\nFilename: ./a.deb\n\n"


def test_repo_http_get_still_reaches_requests() -> None:
    with patch("requests.get", return_value=_resp(body=_PACKAGES.encode())):
        text = repo.fetch_packages("https://apt.example.invalid")
    assert "com.example.alpha" in text


def test_repo_http_get_still_catches_a_request_exception() -> None:
    with patch("requests.get", side_effect=requests.RequestException("no route to host")):
        with pytest.raises(RepoError, match="failed: no route to host"):
            repo.fetch_packages("https://apt.example.invalid")


# --- canister._get ---

_PACKAGE = {
    "package_id": "com.example.tweak",
    "name": "Tweak",
    "version": "1.0",
    "description": "d",
    "author": "a",
    "section": "Tweaks",
    "repository_id": "havoc",
    "package_filename": "pkg/com.example.tweak.deb",
    "package_size": 1,
    "sha256_hash": None,
    "price": "Free",
    "depiction": None,
}


def test_canister_get_still_reaches_requests() -> None:
    with patch("requests.get", return_value=_resp(payload={"data": [_PACKAGE]})):
        results = canister.search("tweak")
    assert [p.package_id for p in results] == ["com.example.tweak"]


def test_canister_get_still_catches_a_request_exception() -> None:
    with patch("requests.get", side_effect=requests.RequestException("connection reset")):
        with pytest.raises(CanisterError, match="canister request failed: connection reset"):
            canister.search("tweak")


# --- canister.fetch_deb ---
#
# Its own import, separate from `_get`'s: `fetch_deb` by URL never calls `_get`
# at all, so a broken import there is invisible to the two tests above.


def test_canister_fetch_deb_still_reaches_requests(tmp_path: Path) -> None:
    with patch("requests.get", return_value=_resp(body=b"deb-bytes")):
        dest = canister.fetch_deb("https://repo.example.invalid/x.deb", tmp_path)
    assert dest.read_bytes() == b"deb-bytes"


def test_canister_fetch_deb_still_catches_a_request_exception(tmp_path: Path) -> None:
    with patch("requests.get", side_effect=requests.RequestException("truncated")):
        with pytest.raises(CanisterError, match="download failed: truncated"):
            canister.fetch_deb("https://repo.example.invalid/x.deb", tmp_path)


# --- apple_portal.ASCClient.__init__ and ASCClient._request ---


def _creds(tmp_path: Path) -> PortalCredentials:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key_path = tmp_path / "k.p8"
    key_path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return PortalCredentials(key_id="A", issuer_id="B", key_path=key_path)


def test_asc_client_still_builds_its_own_requests_session(tmp_path: Path) -> None:
    """Every other test of this class injects a `MagicMock` session, so the
    real `requests.Session()` in `__init__` — the default the CLI actually
    uses — had no coverage at all before this."""
    client = ASCClient(_creds(tmp_path))
    assert isinstance(client.session, requests.Session)
    assert client.session.headers["User-Agent"] == apple_portal.USER_AGENT


def test_asc_client_still_catches_a_request_exception(tmp_path: Path) -> None:
    session = MagicMock()
    session.request.side_effect = requests.RequestException("TLS handshake failed")
    client = ASCClient(_creds(tmp_path), session=session)
    with pytest.raises(PortalError, match="request failed: TLS handshake failed"):
        client._request("GET", "/bundleIds")


# --- apple_portal.make_jwt ---


def test_make_jwt_still_reaches_jwt(tmp_path: Path) -> None:
    """`tests/test_apple_portal.py::test_make_jwt` covers this site too, and
    covers the token's shape in more detail. This one is here so that every
    import this file's guards moved has its counterpart in the same file —
    the pairing is the point, and a reader checking whether the guards are
    load-bearing should not have to go looking in another file for one of
    eight."""
    token = apple_portal.make_jwt(_creds(tmp_path))
    header_b64, payload_b64, _sig = token.split(".")

    def _unpad(seg: str) -> bytes:
        return base64.urlsafe_b64decode(seg + "=" * ((4 - len(seg) % 4) % 4))

    assert json.loads(_unpad(header_b64))["alg"] == "ES256"
    assert json.loads(_unpad(payload_b64))["aud"] == apple_portal.JWT_AUDIENCE
