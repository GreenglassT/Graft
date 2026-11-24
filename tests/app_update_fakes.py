"""Stand-ins for the self-update tests: dummy Graft.app bundles, their zips,
a local server for the release JSON and the download, and the signature
tools.

Nothing here is signed or notarized, and nothing reaches past this Mac: the
"release" is served from 127.0.0.1, and codesign and spctl are answered by
`FakeTools`, which runs only ditto for real.
"""

from __future__ import annotations

import hashlib
import json
import plistlib
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from graft_ipa import app_update

TAG_PAGE = "https://github.com/GreenglassT/Graft/releases/tag/"
#: A made-up Developer ID team: the running Graft's, in these tests.
TEAM = "ABCDE12345"
#: Where the Graft.app this CLI runs from is, in these tests (`own_bundle`).
#: Nothing is there: only `FakeTools` is asked about it.
RUNNING_APP = Path("/private/var/empty/Running/Graft.app")


def dummy_app(where: Path, version: str, *, bundle_id: str = app_update.BUNDLE_ID,
              marker: str = "") -> Path:
    """A tiny unsigned Graft.app in `where`: an Info.plist, an "executable"
    that is only text, and a framework with the usual `Versions/Current`
    link. `marker` goes in a file of its own, to tell two copies apart."""
    app = where / app_update.APP_NAME
    contents = app / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "MacOS" / "Graft").write_text("#!/bin/sh\nexit 0\n")
    with (contents / "Info.plist").open("wb") as f:
        plistlib.dump({
            "CFBundleIdentifier": bundle_id,
            "CFBundleShortVersionString": version,
            "CFBundleExecutable": "Graft",
            "CFBundlePackageType": "APPL",
        }, f)
    versions = contents / "Frameworks" / "Example.framework" / "Versions"
    (versions / "A").mkdir(parents=True)
    (versions / "A" / "Example").write_text("not a binary")
    (versions / "Current").symlink_to("A")
    (contents / "Resources").mkdir()
    (contents / "Resources" / "marker.txt").write_text(marker)
    return app


def zip_app(app: Path, dest: Path) -> bytes:
    """`app` zipped the way the release is (`ditto -c -k --keepParent`);
    the zip's bytes."""
    subprocess.run(["/usr/bin/ditto", "-c", "-k", "--keepParent", str(app), str(dest)], check=True)
    return dest.read_bytes()


def release_json(base: str, tag: str, zip_bytes: bytes, **asset_fields: Any) -> dict[str, Any]:
    """GitHub's latest-release answer, trimmed, for a release whose
    Graft.zip is `zip_bytes`, served from `base`."""
    asset = {
        "name": app_update.ASSET_NAME,
        "state": "uploaded",
        "size": len(zip_bytes),
        "digest": "sha256:" + hashlib.sha256(zip_bytes).hexdigest(),
        "browser_download_url": f"{base}/download/{tag}/{app_update.ASSET_NAME}",
    }
    asset.update(asset_fields)
    return {
        "tag_name": tag, "html_url": TAG_PAGE + tag, "draft": False, "prerelease": False,
        "assets": [
            {"name": f"Graft-{tag.lstrip('v')}-sources.tar.gz", "size": 1,
             "browser_download_url": f"{base}/download/{tag}/sources.tar.gz"},
            asset,
        ],
    }


@dataclass
class Route:
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class FixtureServer:
    """A local HTTP server answering from `routes` (path → Route), and
    recording every request it got: (path, headers)."""

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self.requests: list[tuple[str, dict[str, str]]] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                server.requests.append((self.path, dict(self.headers)))
                route = server.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(route.status)
                for name, value in route.headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(route.body)))
                self.end_headers()
                self.wfile.write(route.body)

            def log_message(self, *args: Any) -> None:
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def serve_json(self, path: str, data: Any) -> None:
        self.routes[path] = Route(body=json.dumps(data).encode(),
                                  headers={"Content-Type": "application/json"})

    def paths(self) -> list[str]:
        return [path for path, _ in self.requests]

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class FakeTools:
    """`app_update.Run` for the checks: codesign and spctl answer as set
    here; ditto runs for real (on the test's own zip, into its temp dir).
    Every argv is recorded: about the download in `calls`, about the Graft
    the CLI runs from (`RUNNING_APP`, `own_team`) in `own_calls`."""

    def __init__(self) -> None:
        self.signature_ok = True
        self.requirement_ok = True
        self.stapled = True
        self.gatekeeper_ok = True
        #: What spctl names as the source when it accepts.
        self.source = "Notarized Developer ID"
        #: The team whose Developer ID signed the download.
        self.signed_by = TEAM
        #: The running Graft: what codesign names as its team ("not set" for
        #: ad hoc), whether its signature verifies, and whether it is a
        #: Developer ID one (not "Apple Development").
        self.own_team = TEAM
        self.own_signature_ok = True
        self.own_developer_id = True
        self.calls: list[list[str]] = []
        self.own_calls: list[list[str]] = []
        # Kept now: a test may then put this fake in `run_tool`'s place.
        self._real = app_update.run_tool

    def __call__(self, argv: list[str]) -> app_update.Ran:
        tool, args = argv[0], argv[1:]
        if tool == app_update.CODESIGN and args[-1:] == [str(RUNNING_APP)]:
            self.own_calls.append(argv)
            return self._own(args)
        self.calls.append(argv)
        if tool == app_update.DITTO:
            return self._real(argv)
        if tool == app_update.CODESIGN and args[:3] == ["--verify", "--deep", "--strict"]:
            return app_update.Ran(0 if self.signature_ok else 1, "" if self.signature_ok
                                  else f"{args[-1]}: code object is not signed at all\n")
        if tool == app_update.CODESIGN and "-R" in args:
            ok = self.requirement_ok and self._names(args, self.signed_by)
            return app_update.Ran(0 if ok else 3, "" if ok
                                  else "test-requirement: code failed to satisfy specified code requirement(s)\n")
        if tool == app_update.CODESIGN and args[0] == "-dvv":
            lines = [f"Executable={args[-1]}/Contents/MacOS/Graft", "Identifier=com.greenglasst.graft"]
            if self.stapled:
                lines.append("Notarization Ticket=stapled")
            return app_update.Ran(0, "\n".join(lines) + "\n")
        if tool == app_update.SPCTL:
            if not self.gatekeeper_ok:
                return app_update.Ran(3, f"{args[-1]}: rejected\n")
            return app_update.Ran(0, f"{args[-1]}: accepted\nsource={self.source}\n")
        raise AssertionError(f"no test here expects {argv}")

    @staticmethod
    def _names(args: list[str], team: str) -> bool:
        """The `-R` requirement in `args` is a Developer ID one for `team`."""
        wanted = args[args.index("-R") + 1]
        return wanted == "=" + app_update.requirement(team) if app_update.TEAM_RE.fullmatch(team) else False

    def _own(self, args: list[str]) -> app_update.Ran:
        if args[:3] == ["--verify", "--deep", "--strict"]:
            return app_update.Ran(0 if self.own_signature_ok else 1, "" if self.own_signature_ok
                                  else f"{args[-1]}: a sealed resource is missing or invalid\n")
        if args[:2] == ["-dv", "--verbose=2"]:
            return app_update.Ran(0, f"Executable={args[-1]}/Contents/MacOS/Graft\n"
                                     f"Identifier={app_update.BUNDLE_ID}\n"
                                     f"TeamIdentifier={self.own_team}\n")
        if "-R" in args:
            ok = self.own_developer_id and self._names(args, self.own_team)
            return app_update.Ran(0 if ok else 3, "" if ok
                                  else "test-requirement: code failed to satisfy specified code requirement(s)\n")
        raise AssertionError(f"no test here expects codesign {args}")

    def tools_run(self) -> list[str]:
        """The checks run, by name, in order."""
        named = []
        for argv in self.calls:
            if argv[0] == app_update.DITTO:
                named.append("ditto")
            elif argv[0] == app_update.SPCTL:
                named.append("spctl")
            elif "-R" in argv:
                named.append("requirement")
            elif argv[1] == "-dvv":
                named.append("stapled")
            else:
                named.append("signature")
        return named
