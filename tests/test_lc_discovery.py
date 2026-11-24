"""Finding LiveContainer on a device without being told its team id.

Every installer (AltStore/SideStore, iloader, Impactor) installs it as
`com.kdt.livecontainer.<TEAMID>`, and extra instances as
`com.kdt.LiveContainer2.<TEAMID>` / `…3.<TEAMID>`. A free Apple ID's team id is
in no config file, so the ids are matched by pattern; a paid config's team id
only breaks a tie. These are the rules `--lc-status`, the push and `--lc-clean`
all use.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from pymobiledevice3.exceptions import AfcException, ConnectionTerminatedError

from graft_ipa import lc_discovery, lc_manage
from graft_ipa.device_install import this_mac
from graft_ipa.lc_runner import LiveContainerError

TEAM = "ABCDE12345"
OTHER = "FGHIJ67890"
PRIMARY = f"com.kdt.livecontainer.{TEAM}"
LC2 = f"com.kdt.LiveContainer2.{TEAM}"
LC3 = f"com.kdt.LiveContainer3.{TEAM}"


# --- roles ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("bundle_id", "role"),
    [
        (PRIMARY, "primary"),
        (f"com.kdt.livecontainer.{OTHER}", "primary"),
        ("com.kdt.livecontainer.abcde12345", "primary"),
        # Case-insensitive, as the secondaries always were.
        (f"com.kdt.LiveContainer.{TEAM}", "primary"),
        (f"COM.KDT.LIVECONTAINER.{TEAM}", "primary"),
        (LC2, "2"),
        (LC3, "3"),
        # Case varies by installer; the secondaries match either way.
        (f"com.kdt.livecontainer2.{TEAM}", "2"),
        (f"COM.KDT.LIVECONTAINER3.{TEAM}", "3"),
        # Not ids an installer makes: legacy. Used when one is the only
        # LiveContainer on the device, never chosen over a real one.
        ("com.kdt.livecontainer.LiveProcess", "legacy"),
        ("com.kdt.livecontainer.ABCDE1234", "legacy"),       # nine characters
        ("com.kdt.livecontainer.ABCDE123456", "legacy"),     # eleven
        ("com.kdt.livecontainer.ABCDE-1234", "legacy"),
        ("com.kdt.LiveContainer4.ABCDE12345", "legacy"),
        ("com.kdt.livecontainer", "legacy"),
        ("com.kdt.livecontainerfan", "legacy"),
        ("com.example.app", None),
        ("com.example.livecontainer.ABCDE12345", None),
    ],
)
def test_roles_without_a_team(bundle_id: str, role: str | None) -> None:
    assert lc_discovery.role_of(bundle_id, None) == role


def test_a_configured_team_still_recognises_its_own_ids_whatever_their_length() -> None:
    """`--lc-install` names instances after the configured team. A team id that
    is not ten characters matches no pattern, and must still be found."""
    assert lc_discovery.role_of("com.kdt.livecontainer.T1", "T1") == "primary"
    assert lc_discovery.role_of("com.kdt.LiveContainer2.T1", "T1") == "2"
    assert lc_discovery.role_of("com.kdt.livecontainer.T1", None) == "legacy"


def test_lc_status_uses_the_same_roles() -> None:
    ids = (PRIMARY, LC2, f"com.kdt.LiveContainer.{OTHER}", "com.kdt.livecontainer.LiveProcess",
           "com.example.app")
    status = lc_manage.status_payload(
        udid="u1", apps={b: {} for b in ids}, team=None, ours=set(), latest=None
    )

    assert {i["bundle_id"]: i["role"] for i in status["instances"]} == {
        b: lc_discovery.role_of(b, None) for b in ids if lc_discovery.role_of(b, None)
    }


# --- choosing, with no team id (a free Apple ID) -------------------------------


def test_a_primary_from_an_unknown_team_is_found() -> None:
    chosen, others = lc_discovery.choose([f"com.kdt.livecontainer.{OTHER}", "com.example.app"])

    assert chosen == f"com.kdt.livecontainer.{OTHER}"
    assert others == []


def test_the_primary_is_chosen_over_its_secondaries_which_are_reported() -> None:
    # The exact scenario that once broke a build: LiveContainer 2/3 installed too.
    chosen, others = lc_discovery.choose([LC3, PRIMARY, LC2, "com.example.app"])

    assert chosen == PRIMARY
    assert others == [LC2, LC3]


def test_two_primaries_need_lc_bundle_id_and_the_error_lists_them() -> None:
    """Two free Apple IDs, or SideStore and iloader each installing one."""
    a, b = PRIMARY, f"com.kdt.livecontainer.{OTHER}"

    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([a, b, LC2])

    assert a in exc.value.message and b in exc.value.message
    assert LC2 not in exc.value.message, "only the ambiguous ones"
    assert "--lc-bundle-id" in exc.value.hint


def test_two_secondaries_and_no_primary_are_ambiguous() -> None:
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([LC2, LC3])

    assert LC2 in exc.value.message and LC3 in exc.value.message
    assert "--lc-bundle-id" in exc.value.hint


def test_a_lone_secondary_is_used() -> None:
    assert lc_discovery.choose([LC2.lower()]) == (LC2.lower(), [])


def test_none_found_points_a_free_account_at_the_free_setup() -> None:
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose(["com.example.app"], free_account=True)

    assert exc.value.message == "no LiveContainer found on the device"
    assert "SideStore" in exc.value.hint and "iloader" in exc.value.hint
    assert "--lc-install" not in exc.value.hint


def test_none_found_points_a_paid_account_at_lc_install() -> None:
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose(["com.example.app"], free_account=False)

    assert "--lc-install" in exc.value.hint


@pytest.mark.parametrize(
    "only",
    [
        "com.kdt.livecontainer",                        # bare upstream: a jailbreak, TrollStore
        PRIMARY,
        f"com.kdt.LiveContainer.{TEAM}",                # a capitalized primary
        LC2,
        LC3.upper(),
        "com.kdt.livecontainer.ABCDE1234",              # a team id that is not ten characters
        f"com.kdt.livecontainer.{TEAM}.resigned",       # an on-device re-signer's suffix
        "com.kdt.livecontainer.LiveProcess",
    ],
)
def test_the_only_livecontainer_on_the_device_is_used_whatever_its_id(only: str) -> None:
    """The rule from before discovery, kept: with one LiveContainer of any kind
    installed there is nothing to choose between, and refusing it would stop
    a build that has exactly one place to go."""
    assert lc_discovery.choose([only, "com.example.app"]) == (only, [])
    assert lc_discovery.choose([only], free_account=True) == (only, [])


@pytest.mark.parametrize(
    "odd", ["com.kdt.livecontainer.LiveProcess", "com.kdt.livecontainer.ABCDE1234"]
)
def test_an_odd_id_beside_a_primary_does_not_make_it_ambiguous(odd: str) -> None:
    """Several candidates: the single primary wins, and the odd one is still
    reported, since it is a LiveContainer the user may be looking in."""
    assert lc_discovery.choose([odd, PRIMARY]) == (PRIMARY, [odd])


def test_an_odd_id_beside_a_secondary_and_no_primary_is_ambiguous() -> None:
    odd = "com.kdt.livecontainer.LiveProcess"

    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([odd, LC2])

    assert odd in exc.value.message and LC2 in exc.value.message


def test_a_capitalised_primary_is_chosen_over_its_secondaries() -> None:
    capitalised = f"com.kdt.LiveContainer.{TEAM}"

    assert lc_discovery.choose([LC2, capitalised, LC3]) == (capitalised, [LC2, LC3])


def test_the_bare_upstream_id_is_still_a_target() -> None:
    """What a jailbroken install leaves, and the push target before discovery."""
    assert lc_discovery.choose(["com.kdt.livecontainer"]) == ("com.kdt.livecontainer", [])
    assert lc_discovery.choose(["com.kdt.livecontainer", LC2]) == (
        "com.kdt.livecontainer", [LC2],
    )


def test_bare_and_a_suffixed_primary_are_ambiguous_with_a_pin_to_copy() -> None:
    """Two primaries, one a jailbreak's and one an installer's: nothing says
    which the user imports from, so Graft asks rather than guesses, and the
    hint is a line they can paste."""
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose(["com.kdt.livecontainer", PRIMARY, LC2])

    assert exc.value.message == (
        "2 LiveContainers are installed and Graft will not guess which to use: "
        f"com.kdt.livecontainer, {PRIMARY}"
    )
    # The paste-able line is the CLI's own verb, and the app's picker is named
    # too: a zero-Terminal user never edits config.yaml by hand.
    assert "graft --set-app-options --app <name> --lc-bundle-id com.kdt.livecontainer" in exc.value.hint
    assert PRIMARY in exc.value.hint
    assert "Edit App › LiveContainer" in exc.value.hint
    assert "config.yaml" not in exc.value.hint


def test_a_bad_pin_names_the_apps_picker_too() -> None:
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([PRIMARY], pinned="com.kdt.livecontainer.NOTHERE")
    assert "--set-app-options --app <name> --lc-bundle-id" in exc.value.hint
    assert "Edit App › LiveContainer" in exc.value.hint


def test_the_configured_team_settles_bare_against_its_own_primary() -> None:
    assert lc_discovery.choose(["com.kdt.livecontainer", PRIMARY], team=TEAM) == (
        PRIMARY, ["com.kdt.livecontainer"],
    )


# --- choosing, with a pin or a team ---------------------------------------------


def test_an_exact_pin_targets_that_livecontainer() -> None:
    chosen, others = lc_discovery.choose([PRIMARY, LC3], pinned=LC3)

    assert chosen == LC3
    assert others == [PRIMARY]


def test_a_pin_may_name_the_id_without_its_team_suffix() -> None:
    assert lc_discovery.choose([PRIMARY, LC2], pinned="com.kdt.LiveContainer2")[0] == LC2


def test_a_pin_that_matches_nothing_among_several_is_an_error() -> None:
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([PRIMARY, LC2], pinned=LC3)

    assert LC3 in exc.value.message
    assert PRIMARY in exc.value.message and LC2 in exc.value.message


def test_a_pin_differing_only_in_case_targets_that_livecontainer() -> None:
    """Installers disagree on case (`LiveContainer2` against `livecontainer2`),
    and so do the README and `--lc-status`: the pin a user types from either
    must find the container, as the patterns themselves always did."""
    installed = [PRIMARY, LC2]

    assert lc_discovery.choose(installed, pinned=LC2.lower()) == (LC2, [PRIMARY])
    assert lc_discovery.choose(installed, pinned=PRIMARY.upper())[0] == PRIMARY


def test_a_pin_whose_team_suffix_differs_in_case_targets_that_livecontainer() -> None:
    other = f"com.kdt.livecontainer.{OTHER}"

    assert lc_discovery.choose([PRIMARY, other], pinned=other.lower())[0] == other


def test_a_stale_pin_is_an_error_naming_it_even_with_one_livecontainer_there() -> None:
    """The pin names a LiveContainer that is gone, and the one installed is a
    different one. Falling back to it silently put the IPA where the pin said
    it must not go, with nothing anywhere saying the pin was ignored."""
    only = f"com.kdt.livecontainer.{OTHER}"

    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([only, "com.example.app"], pinned=LC2)

    assert LC2 in exc.value.message
    assert "not installed" in exc.value.message
    assert only in exc.value.message
    assert "--no-lc-bundle-id" in exc.value.hint


@pytest.mark.parametrize("pin", ["com.example.app", "com.example.livecontainer.ABCDE12345"])
def test_a_pin_to_an_app_that_is_not_livecontainer_is_refused(pin: str) -> None:
    """Installed or not, a pin to some other app would push an IPA into that
    app's Documents, where LiveContainer never looks."""
    with pytest.raises(LiveContainerError) as exc:
        lc_discovery.choose([PRIMARY, pin], pinned=pin)

    assert pin in exc.value.message
    assert "not a LiveContainer" in exc.value.message


@pytest.mark.parametrize(
    ("pin", "ok"),
    [
        (PRIMARY, True),
        (LC2.lower(), True),
        ("com.kdt.LiveContainer2", True),     # the prefix form `choose` accepts
        ("com.kdt.livecontainer", True),      # the bare upstream id
        ("COM.KDT.LIVECONTAINER3.ABCDE12345", True),
        ("com.example.app", False),
        ("", False),
        ("com.example.livecontainer.ABCDE12345", False),
    ],
)
def test_is_livecontainer_id(pin: str, ok: bool) -> None:
    assert lc_discovery.is_livecontainer_id(pin) is ok


def test_the_configured_team_breaks_a_tie_between_primaries() -> None:
    other = f"com.kdt.livecontainer.{OTHER}"

    assert lc_discovery.choose([other, PRIMARY], team=TEAM) == (PRIMARY, [other])


def test_a_configured_team_does_not_hide_a_primary_from_another_team() -> None:
    """A paid user whose LiveContainer came from SideStore under a free Apple
    ID: the team id in config.yaml is not the one on the device."""
    other = f"com.kdt.livecontainer.{OTHER}"

    assert lc_discovery.choose([other, f"com.kdt.LiveContainer2.{OTHER}"], team=TEAM)[0] == other


# --- through the push, against fake device services ------------------------------


class _Lockdown:
    #: A phone that already trusts this Mac: `open_lockdown` asks nothing.
    paired = True

    async def close(self) -> None:
        pass


class _HouseArrest:
    """A LiveContainer's sandbox. `written` is every file the push left, by
    (bundle id, path); `docs` the names that were there before."""

    #: The phone goes away (a cable pulled) once this many bytes are written.
    gone_after: ClassVar[int | None] = None
    #: A write fails with the phone still there (a full disk).
    write_fails: ClassVar[bool] = False
    #: Every chunk written, in order, across sandboxes.
    chunks: ClassVar[list[bytes]] = []
    #: Every byte read off the phone, across sandboxes, however it was read:
    #: a whole file read shows here as all of it.
    bytes_read: ClassVar[int] = 0

    def __init__(self, bundle_id: str, written: dict, docs: dict,
                 contents: dict | None = None) -> None:
        self.bundle_id, self.written, self.docs = bundle_id, written, docs
        self.contents = contents or {}
        self.read: list[str] = []
        self.gone = False
        self._open: str | None = None
        #: The file open for reading, and where in it the next read starts.
        self._reading: list | None = None

    def _check(self) -> None:
        if self.gone:
            raise ConnectionTerminatedError()

    def _forget(self, path: str) -> None:
        self.written.pop((self.bundle_id, path), None)
        name = path.removeprefix("Documents/")
        if name in self.docs.get(self.bundle_id, ()):
            self.docs[self.bundle_id] = [n for n in self.docs[self.bundle_id] if n != name]

    async def fopen(self, path: str, mode: str = "r") -> int:
        self._check()
        if mode == "r":
            if not await self.exists(path):
                raise AfcException(f"no such file: {path}", 8)
            self._reading = [path, 0]
            return 2
        assert mode == "w"
        self._forget(path)
        self.written[(self.bundle_id, path)] = b""
        self._open = path
        return 1

    async def fseek(self, handle: int, offset: int, whence: int = 0) -> None:
        import os

        self._check()
        assert handle == 2 and self._reading is not None
        base = {os.SEEK_SET: 0, os.SEEK_END: len(self._bytes(self._reading[0]))}[whence]
        self._reading[1] = base + offset

    async def fread(self, handle: int, size: int) -> bytes:
        self._check()
        assert handle == 2 and self._reading is not None
        path, at = self._reading
        data = self._bytes(path)[at:at + size]
        self._reading[1] = at + len(data)
        _HouseArrest.bytes_read += len(data)
        return data

    async def listdir(self, folder: str) -> list[str]:
        self._check()
        names = set(self.docs.get(self.bundle_id, ()))
        names |= {p.removeprefix(f"{folder}/") for (b, p) in self.written
                  if b == self.bundle_id and p.startswith(f"{folder}/")}
        return sorted(names)

    async def fwrite(self, handle: int, data: bytes, chunk_size: int = 0) -> None:
        self._check()
        key = (self.bundle_id, self._open)
        have = self.written[key]
        if _HouseArrest.write_fails:
            raise AfcException("Failed to write to the file.", 1)
        limit = _HouseArrest.gone_after
        if limit is not None and len(have) + len(data) > limit:
            self.written[key] = have + data[:limit - len(have)]
            self.gone = True
            raise ConnectionTerminatedError()
        _HouseArrest.chunks.append(bytes(data))
        self.written[key] = have + bytes(data)

    async def fclose(self, handle: int) -> None:
        self._check()
        if handle == 2:
            self._reading = None
        else:
            self._open = None

    async def rm_single(self, path: str, force: bool = False) -> bool:
        self._check()
        self._forget(path)
        return True

    async def rename(self, source: str, target: str) -> None:
        self._check()
        if not await self.exists(source):
            raise AfcException(f"no such file: {source}", 8)
        if (self.bundle_id, source) in self.written:
            moved = self.written.pop((self.bundle_id, source))
        else:  # one that was there before: its bytes go with it
            moved = self._bytes(source)
            self._forget(source)
        self._forget(target)
        self.written[(self.bundle_id, target)] = moved

    async def exists(self, path: str) -> bool:
        name = path.removeprefix("Documents/")
        return (self.bundle_id, path) in self.written or name in self.docs.get(self.bundle_id, ())

    def _bytes(self, path: str) -> bytes:
        name = path.removeprefix("Documents/")
        # A file in `docs` with no content given is somebody's three bytes.
        return self.contents.get((self.bundle_id, name), b"???")

    async def stat(self, path: str) -> dict:
        return {"st_size": len(self._bytes(path))}

    async def get_file_contents(self, path: str) -> bytes:
        self.read.append(path)
        data = self._bytes(path)
        _HouseArrest.bytes_read += len(data)
        return data

    async def set_file_contents(self, path: str, data: bytes) -> None:
        # What pymobiledevice3's does: open the real name, write, close.
        handle = await self.fopen(path, "w")
        await self.fwrite(handle, data)
        await self.fclose(handle)

    async def close(self) -> None:
        pass


def fake_device(
    monkeypatch: pytest.MonkeyPatch,
    installed: list[str],
    *,
    system: list[str] = (),
    lookups: list[dict] | None = None,
    docs: dict[str, list[str]] | None = None,
    contents: dict[tuple[str, str], bytes] | None = None,
) -> dict:
    """pymobiledevice3's three entry points the push uses, over `installed`
    (User apps) and `system` (System apps: what TrollStore or a rootful
    jailbreak registers). Answers by `ApplicationType` the way the device
    does, and `ReturnAttributes` too. Every Lookup's options are appended to
    `lookups`. `docs` is what each sandbox's Documents already holds, and
    `contents` the bytes of any of those files, by (bundle id, name).
    Returns what was written, keyed by (sandbox bundle id, path)."""
    import pymobiledevice3.lockdown as lockdown_mod
    import pymobiledevice3.services.house_arrest as house_arrest_mod
    import pymobiledevice3.services.installation_proxy as ipx_mod

    written: dict = {}
    by_type = {
        "User": {b: {"CFBundleIdentifier": b, "ApplicationType": "User",
                     "CFBundleShortVersionString": "1.0"} for b in installed},
        "System": {b: {"CFBundleIdentifier": b, "ApplicationType": "System",
                       "CFBundleShortVersionString": "1.0"} for b in system},
    }
    by_type["Any"] = {**by_type["User"], **by_type["System"]}
    _HouseArrest.gone_after = None
    _HouseArrest.write_fails = False
    _HouseArrest.chunks = []
    _HouseArrest.bytes_read = 0

    async def create_using_usbmux(*, serial: str, autopair: bool):
        return _Lockdown()

    class InstallationProxy:
        def __init__(self, *, lockdown) -> None:
            pass

        async def lookup(self, options=None):
            options = dict(options or {})
            if lookups is not None:
                lookups.append(options)
            apps = by_type[options.get("ApplicationType", "Any")]
            if "BundleIDs" in options:
                apps = {b: m for b, m in apps.items() if b in options["BundleIDs"]}
            attrs = options.get("ReturnAttributes")
            return {
                b: ({k: v for k, v in meta.items() if k in attrs} if attrs else dict(meta))
                for b, meta in apps.items()
            }

        async def get_apps(self, application_type: str = "Any", **_kw):
            return await self.lookup({"ApplicationType": application_type})

        async def close(self) -> None:
            pass

    async def create_house_arrest(*, lockdown, bundle_id: str):
        if bundle_id not in by_type["Any"]:
            raise RuntimeError(f"no such bundle id {bundle_id}")
        return _HouseArrest(bundle_id, written, docs or {}, contents)

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", create_using_usbmux)
    monkeypatch.setattr(ipx_mod, "InstallationProxyService", InstallationProxy)
    monkeypatch.setattr(house_arrest_mod.HouseArrestService, "create", create_house_arrest)
    return written


def test_a_push_with_no_team_finds_the_primary_on_the_device(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [f"com.kdt.livecontainer.{OTHER}", "com.example.app"])
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"ipa-bytes")

    result = push_ipa_to_livecontainer(
        ipa, "UDID1", inner_filename="example.ipa", team=None, free_account=True
    )

    assert result.bundle_id == f"com.kdt.livecontainer.{OTHER}"
    assert written == {(f"com.kdt.livecontainer.{OTHER}", "Documents/example.ipa"): b"ipa-bytes"}


def test_a_push_with_no_livecontainer_says_how_to_get_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    fake_device(monkeypatch, ["com.example.app"])
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x")

    with pytest.raises(LiveContainerError) as exc:
        push_ipa_to_livecontainer(ipa, "UDID1", free_account=True)

    assert "no LiveContainer found" in exc.value.message
    assert "SideStore" in exc.value.hint


# --- a push never overwrites a file Graft did not put there ----------------------
#
# `set_file_contents` replaces whatever is at the path. A same-named file in
# SideStore's LiveContainer is the user's own copy unless a delivery to this
# device and this container recorded it.


def _example_ipa(tmp_path):
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"graft-build")
    return ipa


def test_a_push_does_not_overwrite_a_file_graft_never_pushed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]})

    with pytest.raises(LiveContainerError) as exc:
        push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                                  ours={})

    assert exc.value.message == (
        f"{PRIMARY} on UDID1 already has a Documents/example.ipa that Graft did not push; "
        "not overwriting it"
    )
    assert "rename or remove example.ipa" in exc.value.hint
    assert written == {}


def test_a_push_with_no_records_given_overwrites_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]})

    with pytest.raises(LiveContainerError, match="Graft did not push"):
        push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa")
    assert written == {}


def test_a_push_overwrites_its_own_recorded_delivery(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["example.ipa"]})

    push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                              ours={PRIMARY: {"example.ipa"}})

    assert written == {(PRIMARY, "Documents/example.ipa"): b"graft-build"}


def test_a_record_in_another_livecontainer_does_not_cover_this_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY, LC2], docs={PRIMARY: ["example.ipa"]})

    with pytest.raises(LiveContainerError, match="Graft did not push"):
        push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                                  ours={LC2: {"example.ipa"}})
    assert written == {}


def test_a_push_to_a_free_name_needs_no_record(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: ["mine.ipa"]})

    push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa")

    assert list(written) == [(PRIMARY, "Documents/example.ipa")]


# --- a dry run names where the push would go ----------------------------------------
#
# It used to report `lc_bundle_id or "com.kdt.livecontainer"`: the bare id, which
# no installer uses, on a dry run whose device had a suffixed one.


def test_a_dry_run_names_the_livecontainer_the_push_would_choose(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    other = f"com.kdt.livecontainer.{OTHER}"
    written = fake_device(monkeypatch, [other, LC2, "com.example.app"])
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"12345")

    result = push_ipa_to_livecontainer(ipa, "UDID1", inner_filename="example.ipa", dry_run=True)

    assert (result.bundle_id, result.others) == (other, [LC2])
    assert (result.documents_path, result.size_bytes) == ("Documents/example.ipa", 5)
    assert written == {}, "a dry run writes nothing"


def test_a_dry_run_with_a_device_that_does_not_answer_names_no_bundle_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    import pymobiledevice3.lockdown as lockdown_mod

    from graft_ipa import lc_runner

    async def unreachable(*, serial: str, autopair: bool):
        raise ConnectionError("no such device")

    monkeypatch.setattr(lockdown_mod, "create_using_usbmux", unreachable)
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x")

    unpinned = lc_runner.push_ipa_to_livecontainer(ipa, "UDID1", dry_run=True)
    pinned = lc_runner.push_ipa_to_livecontainer(ipa, "UDID1", lc_bundle_id=LC2, dry_run=True)

    assert unpinned.bundle_id == "the LiveContainer found on the device"
    assert pinned.bundle_id == LC2


def test_a_dry_run_says_now_what_the_push_would_refuse(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    fake_device(monkeypatch, [PRIMARY, f"com.kdt.livecontainer.{OTHER}"])
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x")

    with pytest.raises(LiveContainerError, match="Graft will not guess"):
        push_ipa_to_livecontainer(ipa, "UDID1", dry_run=True)


# --- System apps: TrollStore and rootful jailbreaks -------------------------------
#
# Both register what they install as a System app, the bare upstream id
# included. A listing of User apps alone never sees it, and the push then
# reported "no LiveContainer found" on a device that had one.

BARE = "com.kdt.livecontainer"


def test_a_push_finds_a_livecontainer_registered_as_a_system_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, ["com.example.app"], system=[BARE])
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"ipa-bytes")

    result = push_ipa_to_livecontainer(ipa, "UDID1", inner_filename="example.ipa")

    assert result.bundle_id == BARE
    assert written == {(BARE, "Documents/example.ipa"): b"ipa-bytes"}


def test_the_push_lists_bundle_ids_only(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The push needs nothing but the ids, and a System listing with every
    attribute is the size of the whole OS's app catalog."""
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    lookups: list[dict] = []
    fake_device(monkeypatch, [PRIMARY], lookups=lookups)
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"x")

    push_ipa_to_livecontainer(ipa, "UDID1")

    assert lookups == [{"ApplicationType": "Any", "ReturnAttributes": ["CFBundleIdentifier"]}]


def test_lc_status_sees_a_livecontainer_registered_as_a_system_app(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`UsbmuxDevice.installed_apps` is what --lc-status, --lc-clean and
    --lc-install list with; their version and role need its metadata."""
    fake_device(monkeypatch, [LC2], system=[BARE])

    apps = lc_manage.UsbmuxDevice().installed_apps("UDID1")

    assert set(apps) == {LC2, BARE}
    assert apps[BARE]["CFBundleShortVersionString"] == "1.0"


def test_lc_status_asks_for_full_metadata_only_for_livecontainers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A System listing with every attribute is the whole OS's app catalog —
    entitlements and all — for a question about two or three apps. Every app's
    bundle id and type (AltStore is found by id, and `--lc-install` needs to
    see a User app), then everything about the LiveContainers alone."""
    lookups: list[dict] = []
    fake_device(monkeypatch, [LC2, "com.example.app"], system=[BARE, "com.apple.example"],
                lookups=lookups)

    apps = lc_manage.UsbmuxDevice().installed_apps("UDID1")

    assert lookups == [
        {"ApplicationType": "Any", "ReturnAttributes": ["CFBundleIdentifier", "ApplicationType"]},
        {"ApplicationType": "Any", "BundleIDs": sorted([LC2, BARE])},
    ]
    assert set(apps) == {LC2, BARE, "com.example.app", "com.apple.example"}
    assert apps[LC2]["CFBundleShortVersionString"] == "1.0"
    assert apps["com.example.app"] == {"CFBundleIdentifier": "com.example.app",
                                       "ApplicationType": "User"}


def test_lc_status_with_no_livecontainer_makes_one_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[dict] = []
    fake_device(monkeypatch, ["com.example.app"], lookups=lookups)

    assert lc_manage.UsbmuxDevice().installed_apps("UDID1") == {
        "com.example.app": {"CFBundleIdentifier": "com.example.app", "ApplicationType": "User"}
    }
    assert len(lookups) == 1


def test_the_check_finds_a_system_livecontainer_listing_each_device_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """--check on a free Apple ID, through the real device seam down to the
    InstallationProxy: one bundle-id-only listing per device, System apps in."""
    import yaml

    from graft_ipa import preflight
    from graft_ipa.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"apps": {
        "example": {"bundle_id": "com.example.app"},
        "other": {"bundle_id": "com.example.other", "lc_bundle_id": BARE},
    }}))
    from graft_ipa import tools
    from tests.tool_fakes import fake_path

    monkeypatch.setattr(tools, "which", fake_path(tmp_path / "path-bin"))
    monkeypatch.setattr(preflight, "list_connected_devices", lambda: ["UDID1", "UDID2"])
    lookups: list[dict] = []
    fake_device(monkeypatch, ["com.example.app"], system=[BARE], lookups=lookups)

    rows = [r for r in preflight.run_free_account_checks(load_config(path))
            if r.name == preflight.LIVECONTAINER_CHECK_NAME]

    assert [(r.ok, r.detail) for r in rows] == [(True, f"UDID1: {BARE}"), (True, f"UDID2: {BARE}")]
    assert lookups == [{"ApplicationType": "Any", "ReturnAttributes": ["CFBundleIdentifier"]}] * 2


def test_the_check_accepts_a_pin_typed_in_another_case(tmp_path) -> None:
    """The `--check` row asks `choose`, so it reads a pin the way the push
    does: one in another case is the LiveContainer it names, not "missing"."""
    import yaml

    from graft_ipa import preflight
    from graft_ipa.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"default_device_udid": "UDID1", "apps": {
        "example": {"bundle_id": "com.example.app", "lc_bundle_id": LC2.lower()},
    }}))
    rows = preflight.check_livecontainer(
        load_config(path), ["UDID1"], installed_apps=lambda u: [PRIMARY, LC2],
    )

    assert [(r.ok, r.detail) for r in rows] == [(True, f"UDID1: {LC2} (also installed: {PRIMARY})")]


# --- --lc-status and --lc-clean on a free Apple ID --------------------------------


def _free_cfg(tmp_path):
    import yaml

    from graft_ipa.config import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "state_dir": str(tmp_path / "state"),
        "default_device_udid": "u1",
        "apps": {"example": {"bundle_id": "com.example.app"}},
    }))
    cfg = load_config(path)
    assert cfg.free_account and cfg.team_id is None
    return cfg


def test_lc_status_works_on_a_free_account_and_changes_nothing(tmp_path) -> None:
    from .test_lc_install import FakeDevice, Recorder

    other_lc2 = f"com.kdt.LiveContainer2.{OTHER}"
    device = FakeDevice({
        f"com.kdt.livecontainer.{OTHER}": {"CFBundleShortVersionString": "3.8.14"},
        other_lc2: {"CFBundleShortVersionString": "3.8.14"},
        "com.example.app": {},
    })
    rec = Recorder()

    def offline(url: str):
        raise LiveContainerError("offline")

    [status] = lc_manage.run_status(
        _free_cfg(tmp_path), udid="u1", channel=None, events=rec.events,
        device_ops=device, get_json=offline,
    )

    assert [(i["bundle_id"], i["role"]) for i in status["instances"]] == [
        (f"com.kdt.livecontainer.{OTHER}", "primary"), (other_lc2, "2"),
    ]
    assert status["reachable"] is True
    assert device.pushed == [] and device.removed == []


def test_lc_clean_on_a_free_account_removes_its_recorded_delivery_and_no_more(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The primary is SideStore's LiveContainer: the user's files live there."""
    from graft_ipa.build_log import BuildLog

    from .test_lc_install import FakeDevice, Recorder

    cfg = _free_cfg(tmp_path)
    primary = f"com.kdt.livecontainer.{OTHER}"
    BuildLog(cfg.state_dir / "log.sqlite").record_delivery(
        app="example", bundle_id="com.example.app", inputs_digest=None, digest_version=None,
        cyan=None, tweaks=[], source_version="1", ipa_sha256=None, delivery="livecontainer",
        lc_bundle_id=primary, remote_name="example.ipa", udid="u1",
    )
    device = FakeDevice(
        {primary: {}}, docs={primary: ["example.ipa", "mine.ipa", "AppleDevelopment.p12"]}
    )
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)
    rec = Recorder()

    lc_manage.run_clean(cfg, udid=None, events=rec.events)

    assert device.removed == [(primary, "example.ipa")]
    assert len(rec.warnings) == 1 and "AppleDevelopment.p12" in rec.warnings[0]


def test_lc_clean_with_nothing_recorded_needs_no_livecontainer(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from .test_lc_install import FakeDevice, Recorder

    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: FakeDevice({"com.example.app": {}}))
    rec = Recorder()

    assert lc_manage.run_clean(_free_cfg(tmp_path), udid=None, events=rec.events) == []
    assert any("nothing removed" in e["line"] for e in rec.of("log"))


# --- a push is streamed, and lands whole or not at all (review #13, #14) ----------


def test_a_push_is_streamed_a_chunk_at_a_time(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Not `set_file_contents(ipa.read_bytes())`, which held the whole IPA in
    memory: a game is gigabytes."""
    from graft_ipa import device_install
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY])
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 4)
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"0123456789")
    push_ipa_to_livecontainer(ipa, "UDID1", inner_filename="example.ipa", team=TEAM)
    assert _HouseArrest.chunks == [b"0123", b"4567", b"89"]
    assert written == {(PRIMARY, "Documents/example.ipa"): b"0123456789"}


def test_a_push_cut_off_half_way_leaves_nothing_at_the_real_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A truncated `<app>.ipa` at the real name had no delivery record and
    matched no archive, so every later push refused it as the user's."""
    from graft_ipa import device_install
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY])
    monkeypatch.setattr(device_install, "UPLOAD_CHUNK_BYTES", 4)
    _HouseArrest.gone_after = 6  # the cable comes out mid-upload
    ipa = tmp_path / "example.ipa"
    ipa.write_bytes(b"0123456789")
    with pytest.raises(LiveContainerError):
        push_ipa_to_livecontainer(ipa, "UDID1", inner_filename="example.ipa", team=TEAM)
    assert (PRIMARY, "Documents/example.ipa") not in written

    # Plugged back in, the next push is not refused, and leaves no partial.
    _HouseArrest.gone_after = None
    push_ipa_to_livecontainer(ipa, "UDID1", inner_filename="example.ipa", team=TEAM, ours={})
    assert written == {(PRIMARY, "Documents/example.ipa"): b"0123456789"}


def test_a_failed_push_removes_its_partial_file_when_the_phone_is_still_there(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY])
    _HouseArrest.write_fails = True
    with pytest.raises(LiveContainerError):
        push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                                  team=TEAM)
    assert written == {}


def test_the_partial_name_is_hidden_and_not_an_ipa() -> None:
    from graft_ipa.lc_runner import partial_path

    partial = partial_path("Documents/example.ipa")
    folder, _, name = partial.rpartition("/")
    assert folder == "Documents" and name.startswith(".") and not name.endswith(".ipa")


# --- the partials a dropped push left are swept (re-check D4) ---------------------
#
# A push whose connection drops cannot remove its partial, and nothing else did:
# hidden, it is not in the Files app, and `--lc-clean` removed only recorded
# pushes. Up to a whole IPA of the phone's storage, for good if the app was
# never pushed under that name again.

#: This Mac, as graft names it in what it leaves on a phone.
HERE = this_mac()
#: Another Mac paired with the same phone.
ELSEWHERE = "0123456789ab" if HERE != "0123456789ab" else "ba9876543210"
#: Above macOS's largest pid (99998): no process here ever has it.
GONE_PID = 999_999

#: Left by a push from this Mac whose graft has exited.
ABANDONED = [
    f".example.ipa.{HERE}-{GONE_PID}.graft-partial",
    f".other app.ipa.{HERE}-{GONE_PID}.graft-partial",  # another app's
]
#: Everything else, which nothing may remove.
NOT_ABANDONED = [
    # A graft still running may be mid-push with it: another run or the
    # app's (pid 1 stands in for one that can't be signaled).
    f".example.ipa.{HERE}-1.graft-partial",
    # Another Mac's, mid-push from there: its pid means nothing here.
    f".example.ipa.{ELSEWHERE}-{GONE_PID}.graft-partial",
    # The name before it named the Mac: whose, and whether live, unknown.
    ".example.ipa.graft-partial",
    # Not graft's names.
    f"example.ipa.{HERE}-{GONE_PID}.graft-partial",
    f".example.ipa.{HERE}-{GONE_PID}.graft-partial.bak",
    f".{HERE}-{GONE_PID}.graft-partial",
    "example.ipa",
    "AppleDevelopment.p12",
]


def test_the_partial_is_named_for_this_mac_and_this_process() -> None:
    import os
    import re

    from graft_ipa.lc_runner import is_abandoned_partial, partial_path

    name = partial_path("Documents/example.ipa").rpartition("/")[2]
    assert re.fullmatch(rf"\.example\.ipa\.{HERE}-{os.getpid()}\.graft-partial", name)
    assert not is_abandoned_partial(name), "this push's own partial, as it writes it"


@pytest.mark.parametrize("name", ABANDONED)
def test_a_partial_this_mac_left_whose_graft_is_gone_is_abandoned(name: str) -> None:
    from graft_ipa.lc_runner import is_abandoned_partial

    assert is_abandoned_partial(name)


@pytest.mark.parametrize("name", NOT_ABANDONED)
def test_anything_else_is_not_abandoned(name: str) -> None:
    from graft_ipa.lc_runner import is_abandoned_partial

    assert not is_abandoned_partial(name)


def test_a_push_first_removes_the_partials_pushes_from_this_mac_left(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    keep = [n for n in NOT_ABANDONED if n != "example.ipa"]  # the push replaces that one
    docs = {PRIMARY: [*ABANDONED, *keep]}
    written = fake_device(monkeypatch, [PRIMARY], docs=docs)

    push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                              team=TEAM)

    assert sorted(docs[PRIMARY]) == sorted(keep)
    assert list(written) == [(PRIMARY, "Documents/example.ipa")]


def test_the_sweep_comes_first_even_on_a_push_that_is_then_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    docs = {PRIMARY: [*ABANDONED, "example.ipa"]}  # a same-named file of the user's
    fake_device(monkeypatch, [PRIMARY], docs=docs)

    with pytest.raises(LiveContainerError, match="did not push"):
        push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                                  team=TEAM)

    assert docs[PRIMARY] == ["example.ipa"]


def test_a_sweep_that_fails_does_not_stop_the_push(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from graft_ipa.lc_runner import push_ipa_to_livecontainer

    written = fake_device(monkeypatch, [PRIMARY], docs={PRIMARY: list(ABANDONED)})

    async def unlistable(self, folder):
        raise AfcException("Failed to list the folder.", 1)

    monkeypatch.setattr(_HouseArrest, "listdir", unlistable)
    push_ipa_to_livecontainer(_example_ipa(tmp_path), "UDID1", inner_filename="example.ipa",
                              team=TEAM)
    assert (PRIMARY, "Documents/example.ipa") in written


def test_lc_clean_removes_the_partials_this_mac_left_in_every_livecontainer(tmp_path, monkeypatch):
    """Recorded or not: the push that left it was cut off before its record
    was written, and that may have been the first push into that container."""
    from .test_lc_install import FakeDevice, Recorder

    device = FakeDevice(
        {PRIMARY: {}, LC2: {}, "com.example.app": {}},
        docs={PRIMARY: [*ABANDONED, *NOT_ABANDONED], LC2: [ABANDONED[0]],
              "com.example.app": [ABANDONED[0]]},
    )
    monkeypatch.setattr(lc_manage, "UsbmuxDevice", lambda: device)
    rec = Recorder()

    lc_manage.run_clean(_free_cfg(tmp_path), udid=None, events=rec.events)

    assert sorted(device.removed) == sorted(
        [(LC2, ABANDONED[0])] + [(PRIMARY, n) for n in ABANDONED]
    )
    lines = [e["line"] for e in rec.of("log")]
    assert any(PRIMARY in line and "unfinished push" in line for line in lines)
    assert not any("nothing removed" in line for line in lines)


def test_lc_clean_counts_a_removed_partial_as_removed_not_kept(tmp_path, monkeypatch) -> None:
    from .test_lc_install import FakeDevice, Recorder

    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: ["example.ipa", ABANDONED[0], "mine.ipa"]})
    rec = Recorder()

    lc_manage.clean(udid="u1", device_ops=device, events=rec.events,
                    pushed={PRIMARY: {"example.ipa"}})

    assert sorted(device.removed) == sorted([(PRIMARY, "example.ipa"), (PRIMARY, ABANDONED[0])])
    [cleaned] = rec.of("lc-cleaned")
    assert cleaned["removed"] == ["example.ipa"] and cleaned["kept"] == 1


def test_a_cert_only_clean_leaves_partials_alone(tmp_path, monkeypatch) -> None:
    """Its contract: the recorded certificates and nothing else."""
    from .test_lc_install import FakeDevice, Recorder

    device = FakeDevice({PRIMARY: {}}, docs={PRIMARY: [*ABANDONED, "AppleDevelopment.p12"]})

    lc_manage.clean(udid="u1", device_ops=device, events=Recorder().events, cert_only=True,
                    pushed={}, certs={PRIMARY: {"AppleDevelopment.p12"}})

    assert device.removed == [(PRIMARY, "AppleDevelopment.p12")]
