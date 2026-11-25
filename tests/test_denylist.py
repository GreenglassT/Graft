"""scripts/denylist.py: the release's scan for names on the private deny-list.

Every term here is a made-up placeholder. The real list lives outside the
repo, and nothing in this file may name an entry of it.
"""

from __future__ import annotations

import gzip
import importlib.util
import io
import os
import subprocess
import sys
import tarfile
import zipfile
import zlib
from pathlib import Path

import pytest

from . import macho_builder as mb
from .toolchain import needs_tools

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("denylist", REPO / "scripts" / "denylist.py")
dl_mod = importlib.util.module_from_spec(_spec)
sys.modules["denylist"] = dl_mod
_spec.loader.exec_module(dl_mod)

#: One placeholder of each shape the real list holds: a name of two words,
#: a bundle id, a name with a symbol, and one the release is allowed to carry.
LIST = """\
# A placeholder scrub list.

Example Game
com.example.thing
Zed+
#release-allow: Allowed  Name
Allowed Name
"""
GAME, BUNDLE, ZED, ALLOWED = 3, 4, 5, 7


@pytest.fixture
def listed(tmp_path, monkeypatch):
    path = tmp_path / "private" / "scrub-denylist.txt"
    path.parent.mkdir()
    path.write_text(LIST)
    monkeypatch.setenv(dl_mod.PATH_ENV, str(path))
    monkeypatch.delenv(dl_mod.SKIP_ENV, raising=False)
    return dl_mod.load()


def test_the_list_is_the_wiki_builds_own_private_file_and_never_in_the_repo(monkeypatch):
    """One list, not two: the wiki build's, by its own path and variable."""
    monkeypatch.delenv(dl_mod.PATH_ENV, raising=False)
    assert dl_mod.list_path() == Path.home() / "OpenSource" / "release-notes" / "scrub-denylist.txt"
    assert not dl_mod.list_path().is_relative_to(REPO)
    assert dl_mod.PATH_ENV == "GRAFT_SCRUB_DENYLIST"
    monkeypatch.setenv(dl_mod.PATH_ENV, "~/elsewhere.txt")
    assert dl_mod.list_path() == Path.home() / "elsewhere.txt"


@needs_tools("git")
def test_no_deny_list_is_tracked():
    """A deny-list in the repository gives away every term in it."""
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files"], capture_output=True, text=True,
                             check=True).stdout.split("\n")
    assert "README.md" in tracked, "the listing is real"
    assert [f for f in tracked if not f.endswith(".py") and ("denylist" in f.lower() or "deny-list" in f.lower())] == []


def test_the_list_reads_as_the_wiki_build_reads_it(listed):
    """Blank lines and `#` lines are comments; `#release-allow:` exempts a
    term from the release's scans only (it is a comment to the wiki build)."""
    assert [n for n, _ in listed.entries] == [GAME, BUNDLE, ZED]
    assert listed.allowed == (ALLOWED,)


@pytest.mark.parametrize("text, lines", [
    (b"play EXAMPLE GAME now", [GAME]),
    (b"Example \t\n  Game", [GAME]),           # any run of whitespace between words
    (b"ExampleGame", []),                      # the words, not their letters run together
    (b"ExampleGameKit and Example Gamer", []),  # no letter or digit right before or after
    (b"(example game)", [GAME]),
    (b"2Example Game", []),
    (b"_Example Game_", [GAME]),               # an underscore is a boundary, as in the wiki build
    (b"id com.example.thing;", [BUNDLE]),
    (b"com.example.things", []),
    (b"comXexampleXthing", []),                # a dot is a dot, not a pattern
    (b"zed+ and Zed+", [ZED]),
    (b"zed plus", []),
    (b"an allowed name", []),
    (b"", []),
])
def test_terms_match_literally_case_insensitively_and_between_word_boundaries(listed, text, lines):
    assert listed.lines_in(text) == lines


def test_a_term_inside_a_zlib_stream_is_found_as_the_path_scan_finds_paths(listed):
    """1.0 and 1.1 leaked build paths only inside compressed data."""
    hidden = b"\0\x01junk" + zlib.compress(b"a table naming com.example.thing\0" * 4) + b"tail"
    assert b"example" not in hidden
    assert listed.lines_in(hidden) == [BUNDLE]


def _cms_signed(cms: bytes, directory: bytes = b"\0" * 40) -> bytes:
    return mb.signed([], mb.superblob([
        (mb.CSSLOT_CODEDIRECTORY, mb.blob(mb.CSMAGIC_CODEDIRECTORY, directory)),
        (0x10000, mb.blob(0xFADE0B01, cms)),
    ]))


def test_a_mach_os_code_signature_is_not_scanned_and_nothing_else_is_left_out(listed):
    """A personal Developer ID's certificate names its owner, and the code
    directory carries its Team ID, both of which the list holds, in every
    signed Mach-O. The embedded signature is left out whole; the rest of the
    file is still scanned."""
    cert = b"0\x82 Developer ID Application: Example Game (ZZZZZZZZZZ)"
    thin = _cms_signed(cert)
    assert b"Example Game" in thin
    assert listed.lines_in(thin) == []
    assert listed.lines_in(_cms_signed(cert, directory=b"ident com.example.thing\0" + b"\0" * 20)) == []
    assert listed.lines_in(thin + b" and Example Game after it") == [GAME]
    universal = mb.fat([(mb.CPU_ARM64, thin), (mb.CPU_X86_64, _cms_signed(cert))])
    assert universal.count(b"Example Game") == 2 and listed.lines_in(universal) == []


def test_only_a_real_signature_is_blanked(listed):
    """Blanked only where LC_CODE_SIGNATURE points at an embedded signature:
    not a Java class file (cafebabe too)."""
    java = b"\xca\xfe\xba\xbe\x00\x00\x00\x37 Example Game"
    assert dl_mod.signature_cms_ranges(java) == [] and listed.lines_in(java) == [GAME]
    inside = mb.signed([], mb.superblob([(0x10000, mb.blob(0xFADE0C02, b"Example Game"))]))
    assert listed.lines_in(inside) == []      # inside an embedded signature: codesign's, not Graft's
    assert listed.lines_in(inside + b" Example Game") == [GAME]
    truncated = _cms_signed(b"Example Game")[:-20]
    assert listed.lines_in(truncated) in ([], [GAME])  # never an exception


def test_missing_unreadable_or_empty_lists_are_errors_that_name_no_term(tmp_path):
    with pytest.raises(dl_mod.DenylistError, match="no deny-list at"):
        dl_mod.load(tmp_path / "absent.txt")
    (tmp_path / "comments.txt").write_text("# only a comment\n\n#release-allow: Example Game\n")
    with pytest.raises(dl_mod.DenylistError, match="holds no term"):
        dl_mod.load(tmp_path / "comments.txt")
    (tmp_path / "latin1.txt").write_bytes(b"Example Gam\xe9\n")
    with pytest.raises(dl_mod.DenylistError, match="isn't UTF-8") as e:
        dl_mod.load(tmp_path / "latin1.txt")
    assert "Example" not in str(e.value)


def test_a_byte_order_mark_is_not_part_of_the_first_term(tmp_path):
    (tmp_path / "bom.txt").write_bytes("﻿Example Game\n".encode())
    assert dl_mod.load(tmp_path / "bom.txt").lines_in(b"an example game") == [1]


def _tar_gz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_a_folder_is_scanned_by_paths_and_contents_and_into_its_archives(listed, tmp_path):
    root = tmp_path / "Graft.app"
    (root / "Contents" / "Resources").mkdir(parents=True)
    (root / "Contents" / "Resources" / "clean.txt").write_text("nothing to see")
    (root / "Contents" / "Resources" / "example-game.png").write_bytes(b"\x89PNG")
    (root / "Contents" / "Resources" / "Example Game.png").write_bytes(b"\x89PNG")
    (root / "Contents" / "Resources" / "notes.txt").write_text("for com.example.thing")
    inner = _tar_gz({"pkg/README": b"Zed+ inside"})
    (root / "Contents" / "Resources" / "bundle.zip").write_bytes(_zip({"deep/inner.tar.gz": inner, "ok": b"fine"}))
    (root / "Contents" / "Resources" / "lone.gz").write_bytes(gzip.compress(b"one example game"))
    (root / "Contents" / "link").symlink_to("/nonexistent/Example Game")
    hits = {h.where: h.lines for h in dl_mod.scan_path(root, listed)}
    assert hits == {
        "Graft.app/Contents/Resources/Example Game.png": (GAME,),
        "Graft.app/Contents/Resources/notes.txt": (BUNDLE,),
        "Graft.app/Contents/Resources/bundle.zip!deep/inner.tar.gz!pkg/README": (ZED,),
        "Graft.app/Contents/Resources/lone.gz": (GAME,),
    }, "a symlink's target isn't followed or read; `example-game` isn't the term"


def test_a_tarball_is_scanned_member_by_member(listed, tmp_path):
    sources = tmp_path / "Graft-9.9-sources.tar.gz"
    sources.write_bytes(_tar_gz({"Graft-9.9-sources/graft/README.md": b"# Graft",
                                 "Graft-9.9-sources/graft/docs/com.example.thing.md": b"",
                                 "Graft-9.9-sources/sdists/x-1.0.tar.gz": _tar_gz({"x-1.0/x.py": b"# zed+"})}))
    hits = [str(h) for h in dl_mod.scan_path(sources, listed)]
    assert hits == [f"Graft-9.9-sources.tar.gz!Graft-9.9-sources/graft/docs/com.example.thing.md: the deny-list's "
                    f"line {BUNDLE}",
                    f"Graft-9.9-sources.tar.gz!Graft-9.9-sources/sdists/x-1.0.tar.gz!x-1.0/x.py: the deny-list's "
                    f"line {ZED}"]


@needs_tools("git")
def test_the_tracked_scan_sees_what_a_public_export_carries_and_nothing_kept_out_of_it(listed, tmp_path):
    """docs/superpowers and tweaks-src never leave the private repository
    (graft_bundle.SOURCE_EXCLUDES); everything else does, paths included."""
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    files = {"README.md": "Graft", "docs/guide.md": "works with com.example.thing",
             "Graft/Example Game.swift": "", "docs/superpowers/plans/example-game.md": "Example Game, Zed+",
             "tweaks-src/Example Game/Tweak.x": "Zed+"}
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    hits = {h.where: h.lines for h in dl_mod.scan_tracked(repo, head, listed)}
    assert hits == {"docs/guide.md": (BUNDLE,), "Graft/Example Game.swift": (GAME,)}


def _cli(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-I", str(REPO / "scripts" / "denylist.py"), *args],
                          capture_output=True, text=True, env={**os.environ, **(env or {})}, timeout=120)


def test_the_command_fails_loudly_without_the_list_and_skips_only_when_told(tmp_path, monkeypatch):
    monkeypatch.delenv(dl_mod.SKIP_ENV, raising=False)
    absent = {dl_mod.PATH_ENV: str(tmp_path / "absent.txt")}
    (tmp_path / "app").mkdir()
    for args in (["check"], ["scan", str(tmp_path / "app")]):
        r = _cli(*args, env=absent)
        assert r.returncode == 1 and "no deny-list at" in r.stderr and "GRAFT_SKIP_DENYLIST=1" in r.stderr, args
        r = _cli(*args, env={**absent, dl_mod.SKIP_ENV: "1"})
        assert r.returncode == 0 and "SKIPPED" in r.stderr, args
        r = _cli(*args, env={**absent, dl_mod.SKIP_ENV: "yes"})
        assert r.returncode == 1, "only 1 skips"
    assert _cli().returncode == 2 and _cli("tracked", "x").returncode == 2


def test_the_command_names_where_a_term_is_and_never_the_term(listed, tmp_path):
    app = tmp_path / "Graft.app"
    app.mkdir()
    (app / "a.txt").write_text("all clear")
    r = _cli("check")
    assert r.returncode == 0 and "3 terms" not in r.stdout and "4 terms" in r.stdout and "1 of them allowed" in r.stdout
    r = _cli("scan", str(app))
    assert r.returncode == 0 and "no deny-listed name" in r.stdout
    (app / "b.txt").write_text("made for com.example.thing and Example Game")
    r = _cli("scan", str(app))
    assert r.returncode == 1
    assert f"Graft.app/b.txt: the deny-list's line {GAME}, {BUNDLE}" in r.stderr
    for term in ("example", "thing", "game"):
        assert term not in (r.stdout + r.stderr).lower().replace("graft.app", ""), term


@pytest.mark.parametrize("rel, scanned", [
    ("Contents/MacOS/Graft", True),
    ("Contents/Info.plist", True),
    ("Contents/Helpers/graft", True),
    ("Contents/Helpers/bin/cyan", True),          # Graft's own launcher
    ("Contents/Helpers/bin/ipatool", False),
    ("Contents/Frameworks/libssl.3.dylib", False),
    ("Contents/Resources/GraftPython/lib/python3.13/os.py", False),
    ("Contents/Resources/GraftPython/lib/python3.13/site-packages/cyan/main.py", False),
    ("Contents/Resources/GraftPython/lib/python3.13/site-packages/graft_ipa/cli.py", True),
    ("Contents/Resources/GraftPython/lib/python3.13/site-packages/graft_ipa-2.0.1.dist-info/RECORD", True),
    ("Contents/Resources/Licenses/zsign-LICENSE", False),
    ("Contents/Resources/Licenses/components.json", False),   # others' module names
    ("Contents/Resources/Licenses/INDEX.txt", False),
    ("Contents/_CodeSignature/CodeResources", False),        # every file's path again
])
def test_in_the_app_graft_s_own_files_are_scanned_whole_and_others_by_name(rel, scanned):
    assert dl_mod.app_contents_scanned(rel) is scanned


@pytest.mark.parametrize("rel, named", [
    ("Contents/MacOS/Graft", True),
    ("Contents/_CodeSignature/CodeResources", True),
    ("Contents/Resources/GraftPython/lib/python3.13/site-packages/graft_ipa/cli.py", True),
    ("Contents/Resources/GraftPython/lib/python3.13/site-packages/x/lexers/examplegame.py", False),
    ("Contents/Resources/Licenses/ipatool-modules/github.com/example-thing/mod@v1.0/LICENSE", False),
    ("Contents/Helpers/bin/ipatool", False),
])
def test_in_the_app_others_paths_keep_upstream_s_names_and_are_not_scanned(rel, named):
    assert dl_mod.app_names_scanned(rel) is named


def test_the_app_scan_skips_others_names_and_still_finds_graft_s_own(listed, tmp_path):
    app = tmp_path / "Graft.app"
    others = app / "Contents/Resources/Licenses/ipatool-modules/com.example.thing"   # a term, as a folder name
    others.mkdir(parents=True)
    (others / "LICENSE").write_text("Example Game")
    (app / "Contents/_CodeSignature").mkdir(parents=True)
    (app / "Contents/_CodeSignature/CodeResources").write_text("Resources/Licenses/ipatool-modules/com.example.thing Example Game")
    own = app / "Contents/Resources/GraftPython/lib/python3.13/site-packages/graft_ipa"
    own.mkdir(parents=True)
    (own / "cli.py").write_text("# for com.example.thing")
    hits = [str(h) for h in dl_mod.scan_path(app, listed, contents=dl_mod.app_contents_scanned,
                                             names=dl_mod.app_names_scanned)]
    assert hits == [f"Graft.app/Contents/Resources/GraftPython/lib/python3.13/site-packages/graft_ipa/cli.py: "
                    f"the deny-list's line {BUNDLE}"]


def test_in_the_source_archive_the_pinned_sources_and_sdists_are_scanned_by_name(listed, tmp_path):
    sources = tmp_path / "Graft-9.9-sources.tar.gz"
    sources.write_bytes(_tar_gz({"Graft-9.9-sources/SOURCES.md": b"built with care",
                                 "Graft-9.9-sources/graft/README.md": b"for com.example.thing",
                                 "Graft-9.9-sources/sdists/x-1.0.tar.gz": _tar_gz({"x/x.py": b"# Example Game"}),
                                 "Graft-9.9-sources/pinned/y/y.tar.gz": b"Example Game",
                                 "Graft-9.9-sources/pinned/zed+/y.tar.gz": b""}))
    hits = [str(h) for h in dl_mod.scan_path(sources, listed, contents=dl_mod.sources_contents_scanned)]
    assert hits == [f"Graft-9.9-sources.tar.gz!Graft-9.9-sources/graft/README.md: the deny-list's line {BUNDLE}",
                    f"Graft-9.9-sources.tar.gz!Graft-9.9-sources/pinned/zed+/y.tar.gz: the deny-list's line {ZED}"]
    r = _cli("sources", str(sources))
    assert r.returncode == 1 and "sdists" not in r.stderr and "graft/README.md" in r.stderr


# --- the owner's device UDIDs (2.0.2) -----------------------------------------------------------
#
# Every UDID here is made up and zero-padded, one of each shape a profile
# holds; none is a real device's. Nothing reads the real config or profiles:
# conftest gives each test its own HOME and XDG roots, and the device check
# reads config.yaml under XDG_CONFIG_HOME.

PHONE = "00008030-000000000000001A"
MAC = "00000000-0000-0000-0000-0000000000B2"
OLD = "000000000000000000000000000000000000c0d3"
LISTED = "00008110-00000000000000E4"


def _der(tag: int, body: bytes) -> bytes:
    n = len(body)
    width = (n.bit_length() + 7) // 8
    return bytes([tag]) + (bytes([n]) if n < 0x80 else bytes([0x80 | width]) + n.to_bytes(width, "big")) + body


def _cms(content: bytes) -> bytes:
    """A test CMS: a ContentInfo / SignedData whose eContent is `content`,
    split into octet-string chunks as a streaming signer writes it, so it
    can only be read through the CMS structure, never by cutting the XML out.
    No signature: nothing here verifies one."""
    chunks = b"".join(_der(0x04, content[i:i + 100]) for i in range(0, len(content), 100))
    encap = _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010701")) + _der(0xA0, _der(0x24, chunks)))
    signed = _der(0x30, _der(0x02, b"\x01") + _der(0x31, b"") + encap + _der(0x31, b""))
    return _der(0x30, _der(0x06, bytes.fromhex("2a864886f70d010702")) + _der(0xA0, signed))


def _profile(path: Path, devices: list, *, cms: bool = False) -> Path:
    import plistlib

    plist = plistlib.dumps({"Name": "Placeholder Profile", "TeamIdentifier": ["ABCDE12345"],
                            "ProvisionedDevices": devices}, fmt=plistlib.FMT_XML)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_cms(plist) if cms else plist)
    return path


@pytest.fixture
def owner(tmp_path, monkeypatch):
    """A Graft config naming three profiles (signing, one app's, and a
    portal cache holding one), and a UDID the config lists itself."""
    monkeypatch.delenv(dl_mod.PROFILES_ENV, raising=False)
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "graft"
    _profile(config / "wildcard.mobileprovision", [PHONE, "not-a-udid", 7])
    _profile(tmp_path / "elsewhere" / "Example Game.mobileprovision", [MAC.lower()], cms=True)
    _profile(config / "profiles" / "com.example.thing.mobileprovision", [OLD, PHONE.lower()])
    (config / "config.yaml").write_text(
        "signing:\n  cert: cert.p12\n  cert_password_env: X\n  profile: wildcard.mobileprovision\n  team_id: ABCDE12345\n"
        f"apps:\n  Example Game:\n    bundle_id: com.example.thing\n    profile: {tmp_path / 'elsewhere' / 'Example Game.mobileprovision'}\n"
        "  Other:\n    bundle_id: com.example.other\n"
        f"app_store_connect:\n  key_id: K\n  issuer_id: I\n  key_file: k.p8\n  device_udids: [{LISTED}]\n")
    return config


def test_the_udids_come_from_every_profile_the_config_names_and_its_own_list(owner):
    devices = dl_mod.load_devices()
    assert set(devices.udids) == {u.lower() for u in (PHONE, MAC, OLD, LISTED)}
    assert devices.profiles == 3 and devices.skipped == 2, "'not-a-udid' and 7 aren't UDIDs"
    assert set(devices.sources) == {"signing.profile", "apps entry 1's profile",
                                    "app_store_connect.profile_cache_dir, profile 1", "app_store_connect.device_udids"}
    source = dict(zip(devices.udids, devices.sources))
    assert source[PHONE.lower()] == "signing.profile", "where it was first read: the cache's copy adds nothing"
    assert source[LISTED.lower()] == "app_store_connect.device_udids"


def test_a_profile_is_decoded_in_python_and_security_never_runs(owner, monkeypatch):
    """`security cms -D` can touch the login keychain; the plist is read out
    of the CMS bytes instead (graft_ipa.mobileprovision), the test CMS's
    chunked content included."""
    import subprocess as sp

    monkeypatch.setattr(sp, "run", lambda *a, **k: pytest.fail(f"ran {a}"))
    monkeypatch.setattr(sp, "Popen", lambda *a, **k: pytest.fail(f"ran {a}"))
    assert MAC.lower() in dl_mod.load_devices().udids


def test_with_no_config_or_no_profile_it_says_so_and_skips(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(dl_mod.PROFILES_ENV, raising=False)
    assert dl_mod.devices_main(["check"]) == dl_mod.NO_PROFILES == 3
    assert "there is no Graft config at" in capsys.readouterr().out
    config = Path(os.environ["XDG_CONFIG_HOME"]) / "graft"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("apps:\n  Example Game:\n    bundle_id: com.example.thing\n")
    assert dl_mod.devices_main(["tracked", str(tmp_path), "HEAD"]) == 3
    out = capsys.readouterr().out
    assert "names no provisioning profile" in out and "skipped" in out and "Example Game" not in out
    _profile(config / "empty.mobileprovision", [])
    (config / "config.yaml").write_text("signing:\n  profile: empty.mobileprovision\n")
    assert dl_mod.devices_main(["check"]) == 3
    assert "provision no device" in capsys.readouterr().out


def test_the_variable_names_the_profiles_instead_and_empty_names_none(owner, tmp_path, monkeypatch):
    folder = tmp_path / "chosen"
    _profile(folder / "a" / "one.mobileprovision", ["00008030-00000000000000F1"], cms=True)
    other = _profile(tmp_path / "two.mobileprovision", ["00008030-00000000000000F2"])
    monkeypatch.setenv(dl_mod.PROFILES_ENV, f"{folder}:{other}")
    devices = dl_mod.load_devices()
    assert devices.udids == ("00008030-00000000000000f1", "00008030-00000000000000f2"), "the config is not read"
    assert devices.sources == (f"{dl_mod.PROFILES_ENV} entry 1, profile 1", f"{dl_mod.PROFILES_ENV} entry 2")
    monkeypatch.setenv(dl_mod.PROFILES_ENV, "")
    assert dl_mod.devices_main(["check"]) == 3


def test_a_profile_that_cant_be_read_fails_naming_its_key_never_its_path(owner, tmp_path, capsys):
    (tmp_path / "elsewhere" / "Example Game.mobileprovision").unlink()
    assert dl_mod.devices_main(["check"]) == 1
    err = capsys.readouterr().err
    assert "can't read apps entry 1's profile" in err and dl_mod.PROFILES_ENV in err
    assert "Example Game" not in err and str(tmp_path) not in err
    (tmp_path / "elsewhere" / "Example Game.mobileprovision").write_bytes(b"not a profile")
    assert dl_mod.devices_main(["check"]) == 1
    err = capsys.readouterr().err
    assert "failed to decode apps entry 1's profile" in err and "Example Game" not in err


def test_check_says_how_many_and_never_which(owner, capsys):
    assert dl_mod.devices_main(["check"]) == 0
    out = capsys.readouterr().out
    assert "device UDIDs: 4 from 3 provisioning profiles and the config's own list; 2 entries" in out
    for udid in (PHONE, MAC, OLD, LISTED):
        assert udid.lower() not in out.lower() and udid.replace("-", "").lower() not in out.lower()


def test_a_udid_is_found_in_any_case_with_or_without_hyphens_and_never_printed(owner, tmp_path):
    app = tmp_path / "Graft.app"
    files = {"Contents/Resources/upper.txt": f"device {PHONE.upper()} here".encode(),
             "Contents/Resources/bare.json": f'{{"udid": "{PHONE.replace("-", "").lower()}"}}'.encode(),
             "Contents/MacOS/Graft": b"\0" + zlib.compress(f"mac {MAC}".encode()) + b"\0",
             "Contents/Resources/near.txt": f"{PHONE[:-1]}B, {OLD}0, 1{LISTED}".encode(),
             "Contents/Resources/clear.txt": b"00008030-000000000000000A, nothing of yours",
             "Contents/Resources/Licenses/zsign-LICENSE": OLD.encode(),
             f"Contents/Resources/Licenses/go-modules/example.com/{OLD}@v1.0.0/LICENSE": b"MIT",
             "Contents/_CodeSignature/CodeResources": OLD.encode(),
             f"Contents/Resources/named-{MAC.lower()}.txt": b"clear"}
    for rel, data in files.items():
        (app / rel).parent.mkdir(parents=True, exist_ok=True)
        (app / rel).write_bytes(data)
    r = _cli("devices", "app", str(app))
    assert r.returncode == 1, r.stdout + r.stderr
    assert "Graft.app/Contents/Resources/upper.txt: device" in r.stderr
    assert "Graft.app/Contents/Resources/bare.json: device" in r.stderr
    assert "Graft.app/Contents/MacOS/Graft: device" in r.stderr, "inside a zlib stream too"
    assert "near.txt" not in r.stderr and "clear.txt" not in r.stderr, "a longer hex run isn't the UDID"
    assert "zsign-LICENSE" not in r.stderr and "go-modules" not in r.stderr and "CodeResources" not in r.stderr, (
        "the deny-list's app scopes: others' files skipped, names too, and what codesign writes")
    assert "Graft.app/Contents/Resources/named-[device 3].txt: device 3" in r.stderr, (
        "a name of Graft's own is scanned, and the UDID in it is masked where the location is printed")
    shown = (r.stdout + r.stderr).lower()
    for udid in (PHONE, MAC, OLD, LISTED):
        assert udid.lower() not in shown and udid.replace("-", "").lower() not in shown
    assert str(owner) not in r.stderr and "Example Game" not in r.stderr


@needs_tools("git")
def test_the_tracked_files_and_the_source_archive_are_scanned_for_them(owner, tmp_path):
    repo = tmp_path / "repo"
    env = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1", "GIT_AUTHOR_NAME": "t",
           "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    for name, text in {"README.md": "Graft", "tests/fixture.py": f'UDID = "{LISTED.lower()}"',
                       "docs/superpowers/notes.md": PHONE}.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "x"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, env=env, capture_output=True)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()
    r = _cli("devices", "tracked", str(repo), head)
    assert r.returncode == 1 and "tests/fixture.py: device" in r.stderr and "app_store_connect.device_udids" in r.stderr
    assert "notes.md" not in r.stderr, "kept out of every export"
    sources = tmp_path / "Graft-9.9-sources.tar.gz"
    sources.write_bytes(_tar_gz({"Graft-9.9-sources/graft/README.md": b"clear",
                                 "Graft-9.9-sources/pinned/y/y.tar.gz": OLD.encode()}))
    r = _cli("devices", "sources", str(sources))
    assert r.returncode == 0 and "no device UDID from your provisioning profiles" in r.stdout
    sources.write_bytes(_tar_gz({"Graft-9.9-sources/graft/SOURCES.md": MAC.encode()}))
    r = _cli("devices", "sources", str(sources))
    assert r.returncode == 1 and "graft/SOURCES.md: device" in r.stderr


def test_the_devices_command_takes_only_its_own_forms():
    for args in (["devices"], ["devices", "scan", "x"], ["devices", "tracked", "x"], ["devices", "app"]):
        assert _cli(*args).returncode == 2, args


# ------------------------------------------------------- #public-allow phrases

PHRASED = LIST + "#public-allow: Zed+  Chat\n#public-allow: zed+.example/AbC123\n"


@pytest.fixture
def phrased(tmp_path):
    path = tmp_path / "scrub-denylist.txt"
    path.write_text(PHRASED)
    return dl_mod.load(path)


@pytest.mark.parametrize("phrase", ["Zed+", "  zed+  ", "ZED+!", "zed+ zed+", "Example   game", "(zed+)"])
def test_a_public_allow_phrase_that_is_only_a_listed_term_is_refused_by_its_line(tmp_path, phrase):
    """`#public-allow: Zed+` would let the bare term through everywhere: refused
    loudly, naming the line and never the phrase or the term."""
    path = tmp_path / "scrub-denylist.txt"
    path.write_text(LIST + f"#public-allow: {phrase}\n")
    line = len(LIST.splitlines()) + 1
    with pytest.raises(dl_mod.DenylistError) as e:
        dl_mod.load(path)
    assert f"line {line}" in str(e.value)
    for word in ("Zed", "zed", "Example", "game"):
        assert word not in str(e.value).split(", line")[1]


def test_a_public_allow_phrase_with_more_than_a_term_still_loads(phrased):
    assert len(phrased.phrases) == 2


@pytest.mark.parametrize("text, lines", [
    (b"join the Zed+ Chat today", []),                   # the term only inside an allowed phrase
    (b"join the zed+   chat today", []),                 # any case, any run of whitespace
    (b"see https://zed+.example/abc123 now", []),         # an exact link, any case
    (b"join the Zed+ Chat, or use Zed+ itself", [ZED]),   # the same term elsewhere still hits
    (b"zed+.example/abc124", [ZED]),                     # one character off is not the phrase
    (b"the Zed+ app", [ZED]),
    (b"Example Game in the Zed+ Chat", [GAME]),          # other terms are untouched
])
def test_a_public_allow_phrase_is_blanked_and_the_term_elsewhere_still_hits(phrased, text, lines):
    assert phrased.lines_in(text) == lines


def test_a_public_allow_phrase_is_blanked_inside_a_zlib_stream_too(phrased):
    assert phrased.lines_in(zlib.compress(b"the Zed+ Chat")) == []
    assert phrased.lines_in(zlib.compress(b"the Zed+ Chat and Zed+")) == [ZED]


def test_a_public_allow_line_is_a_comment_not_a_term_and_allows_nothing_by_itself(phrased, listed):
    assert [n for n, _ in phrased.entries] == [n for n, _ in listed.entries]
    assert len(phrased.phrases) == 2 and listed.phrases == ()
