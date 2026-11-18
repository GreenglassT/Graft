"""Reading a .deb's control stanza for the Tweaks library.

The contract these all circle: `control_fields` is best-effort. `--tweaks` has
to keep listing a directory that contains a half-finished download, a renamed
zip, or a .deb compressed with something this interpreter cannot open. Every
failure mode below must answer `{}`, never raise — the listing is the feature,
the metadata is the garnish.
"""

from __future__ import annotations

import contextlib
import io
import signal
import tarfile

import pytest

from graft_ipa.deb import _MAX_CONTROL_TAR, _MAX_CONTROL_TEXT, control_fields

CONTROL = (
    "Package: com.example.tweak\n"
    "Name: Example Tweak\n"
    "Version: 1.2.3\n"
    "Section: Tweaks\n"
    "Author: Someone <a@b.c>\n"
    "Description: does a thing\n"
)



@contextlib.contextmanager
def _deadline(seconds: int):
    """Turn "never returns" into a failure instead of a hung suite.

    A wall-clock assertion after the call only fires if the call *returns*; an
    infinite loop just stalls pytest forever, which is a test that can never
    go red. SIGALRM interrupts the loop itself.
    """
    def fire(signum, frame):
        raise TimeoutError(f"did not return within {seconds}s")

    previous = signal.signal(signal.SIGALRM, fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def _ar_header(name: str, size: int) -> bytes:
    """One ar header. The size is a parameter so a test can declare a length
    the payload does not have — including a negative one."""
    return f"{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{size:<10}".encode() + b"`\n"


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    """A minimal ar archive — the container format a .deb actually is."""
    out = b"!<arch>\n"
    for name, payload in members:
        out += _ar_header(name, len(payload)) + payload
        if len(payload) % 2:
            out += b"\n"  # members are padded to an even offset
    return out


def _control_tar(text: str = CONTROL, *, name: str = "./control", mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tar:
        data = text.encode()
        info = tarfile.TarInfo(name)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _deb(control_member: str = "control.tar.gz", **kw) -> bytes:
    return _ar([
        ("debian-binary", b"2.0\n"),
        (control_member, _control_tar(**kw)),
        ("data.tar.gz", _control_tar(text="", name="./x")),
    ])


def test_reads_the_fields_the_library_shows(tmp_path):
    path = tmp_path / "t.deb"
    path.write_bytes(_deb())
    assert control_fields(path) == {
        # `package_id` is the identifier, not the display name — it is what
        # every "is there a newer version" lookup is keyed by, and the library
        # could not be compared against anything without it.
        "package_id": "com.example.tweak",
        "name": "Example Tweak",
        "version": "1.2.3",
        "section": "Tweaks",
        "author": "Someone <a@b.c>",
    }


def test_description_is_not_surfaced(tmp_path):
    """Deliberately not in `_FIELDS`. A library row is one line and a
    Description runs to paragraphs, so it is excluded at the source rather than
    truncated at the view — and the control file here has one, to prove the
    exclusion is a choice and not an absence."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb())
    fields = control_fields(path)
    assert "does a thing" not in str(fields)
    assert set(fields) == {"package_id", "name", "version", "section", "author"}


def test_name_falls_back_to_package(tmp_path):
    """`Name` is optional in Debian control and plenty of tweaks omit it;
    `Package` is mandatory, and is what Sileo and Zebra show in its place."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(text="Package: com.example.bare\nVersion: 9\n"))
    fields = control_fields(path)
    assert fields["name"] == "com.example.bare"
    assert fields["version"] == "9"


@pytest.mark.parametrize("member", ["control.tar", "control.tar.xz", "control.tar.bz2"])
def test_reads_every_compression_tarfile_sniffs(tmp_path, member):
    """gz/xz/bz2 need no branching — `tarfile` picks the decompressor off the
    stream. This pins that the member *name* is not what we dispatch on."""
    mode = {"control.tar": "w", "control.tar.xz": "w:xz", "control.tar.bz2": "w:bz2"}[member]
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(control_member=member, mode=mode))
    assert control_fields(path)["version"] == "1.2.3"


def test_a_control_member_without_the_leading_dot(tmp_path):
    """Both `./control` and `control` are seen in the wild."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(name="control"))
    assert control_fields(path)["name"] == "Example Tweak"


# --- degradation: every one of these must be a listing without metadata ---


def test_a_file_that_is_not_an_archive(tmp_path):
    path = tmp_path / "t.deb"
    path.write_bytes(b"this is not a deb")
    assert control_fields(path) == {}


def test_a_truncated_download(tmp_path):
    """The realistic bad file: an interrupted fetch leaves a valid ar header
    over a member that claims more bytes than the file has."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb()[:80])
    assert control_fields(path) == {}


def test_an_archive_with_no_control_member(tmp_path):
    path = tmp_path / "t.deb"
    path.write_bytes(_ar([("debian-binary", b"2.0\n"), ("data.tar.gz", _control_tar())]))
    assert control_fields(path) == {}


def test_a_control_member_that_is_not_a_tar(tmp_path):
    path = tmp_path / "t.deb"
    path.write_bytes(_ar([("control.tar.gz", b"garbage")]))
    assert control_fields(path) == {}


def test_a_missing_file(tmp_path):
    assert control_fields(tmp_path / "nope.deb") == {}


def test_a_directory(tmp_path):
    """`--tweaks` skips directories before it gets here, but this is the
    function's own promise, not the caller's."""
    (tmp_path / "d.deb").mkdir()
    assert control_fields(tmp_path / "d.deb") == {}


def test_an_empty_file(tmp_path):
    path = tmp_path / "t.deb"
    path.write_bytes(b"")
    assert control_fields(path) == {}


def test_zstd_when_the_interpreter_has_it(tmp_path):
    """zstd only became a `tarfile` mode in Python 3.14. On an older
    interpreter a zstd .deb is one this function cannot read, and the contract
    for that is `{}` — a missing name in a list, not a traceback. Both halves
    are asserted, because which one applies is a property of the runtime.
    """
    try:
        blob = _deb(control_member="control.tar.zst", mode="w:zst")
    except (tarfile.CompressionError, ValueError):
        pytest.skip("this interpreter cannot write zstd tars either")
    path = tmp_path / "t.deb"
    path.write_bytes(blob)
    assert control_fields(path)["version"] == "1.2.3"


# --- hostile input: these bytes come off third-party repos ---


def test_a_negative_member_size_terminates(tmp_path):
    """The one that hung. A negative size passed the old `end > len(blob)`
    bounds check, and `pos` then advanced by `60 + size` — zero for -60 — so
    the generator looped forever on a single malformed header and `--tweaks`
    never returned. A corrupt download was enough to make the whole tweak
    library unreachable until the file was deleted by hand.

    Guarded by SIGALRM rather than a wall-clock assertion after the fact: a
    check that runs *after* the call only fires if the call returns, so an
    infinite loop would stall the suite instead of failing it.
    """
    path = tmp_path / "t.deb"
    path.write_bytes(b"!<arch>\n" + _ar_header("data.tar.gz", -60) + b"x" * 200)
    with _deadline(5):
        assert control_fields(path) == {}


@pytest.mark.parametrize("size", [-1, -60, -61, -1000])
def test_every_negative_size_terminates(tmp_path, size):
    path = tmp_path / "t.deb"
    path.write_bytes(b"!<arch>\n" + _ar_header("control.tar.gz", size) + b"x" * 200)
    with _deadline(5):
        assert control_fields(path) == {}


def test_a_member_claiming_more_than_the_file_holds(tmp_path):
    """A truncated download, expressed as an honest header over a short body."""
    path = tmp_path / "t.deb"
    path.write_bytes(b"!<arch>\n" + _ar_header("control.tar.gz", 10_000) + b"short")
    assert control_fields(path) == {}


def test_a_decompression_bomb_is_capped(tmp_path):
    """`tarfile` decompresses lazily, so a `control` entry can declare
    gigabytes and still be a few KB on disk. Reading it unbounded turned a
    library listing into an OOM; the cap makes it a missing name instead.

    The payload is a *valid* stanza followed by padding, deliberately. An
    earlier version of this test used pure filler, which parses to `{}` with or
    without the cap — so it passed for the wrong reason and went green against
    the bug it was written for. With the cap this is rejected on size; without
    it, the leading stanza parses and the assertion below fails.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        payload = CONTROL.encode() + b"\n" + b"X" * (8 * 1024 * 1024)
        info = tarfile.TarInfo("./control")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    compressed = buf.getvalue()
    assert len(compressed) < 100_000, "premise: it compresses small"
    assert len(payload) > _MAX_CONTROL_TEXT, "premise: it expands past the cap"

    path = tmp_path / "t.deb"
    path.write_bytes(_ar([("debian-binary", b"2.0\n"), ("control.tar.gz", compressed)]))
    assert control_fields(path) == {}, "an oversized control member was parsed anyway"


def test_an_oversized_control_tar_member_is_skipped(tmp_path):
    """The outer cap, on the ar member itself — checked before a single byte of
    it is read, so a huge `control.tar` never reaches the decompressor."""
    path = tmp_path / "t.deb"
    filler = b"\0" * (_MAX_CONTROL_TAR + 1)
    path.write_bytes(_ar([("debian-binary", b"2.0\n"), ("control.tar.gz", filler)]))
    assert control_fields(path) == {}


def test_data_tar_is_never_read_into_memory(tmp_path):
    """The archive's *size* must not decide the memory cost of listing the
    library. A real tweak's `data.tar` is the whole payload; only
    `control.tar` is ever wanted, and the format puts it first.
    """
    big = b"\0" * (12 * 1024 * 1024)
    path = tmp_path / "t.deb"
    path.write_bytes(_ar([
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", _control_tar()),
        ("data.tar.gz", big),
    ]))
    reads: list[int] = []
    real_open = type(path).open

    def counting_open(self, *a, **kw):
        handle = real_open(self, *a, **kw)
        original = handle.read

        def read(n=-1):
            chunk = original(n)
            reads.append(len(chunk))
            return chunk

        handle.read = read
        return handle

    type(path).open = counting_open
    try:
        assert control_fields(path)["version"] == "1.2.3"
    finally:
        type(path).open = real_open
    assert max(reads) < len(big), "a single read pulled in the data member"
    assert sum(reads) < len(big), f"read {sum(reads)} bytes for a {len(big)}-byte archive"


@pytest.mark.parametrize("name", ["./control", "control"])
def test_the_two_control_paths_that_are_real(tmp_path, name):
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(name=name))
    assert control_fields(path)["version"] == "1.2.3"


@pytest.mark.parametrize("name", ["././control", ".../..//control", "..control", "/control"])
def test_a_crafted_control_path_is_not_accepted(tmp_path, name):
    """`lstrip("./")` strips a character *set*, so every name here reduced to
    `control` and was read as the authoritative stanza. Only a single leading
    `./` is real."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(name=name))
    assert control_fields(path) == {}


def test_author_falls_back_to_maintainer(tmp_path):
    """`repo._to_repo_package` reads `Author or Maintainer`, and re-deriving
    the mapping here had lost the fallback — a .deb declaring only
    `Maintainer:` showed an author in the Repos browser and a blank one in the
    library, which is the gap this module exists to close, one field over.
    """
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(
        text="Package: com.example.m\nVersion: 2\nMaintainer: Someone <m@x.y>\n"
    ))
    assert control_fields(path)["author"] == "Someone <m@x.y>"


def test_author_prefers_author_over_maintainer(tmp_path):
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(
        text="Package: com.example.b\nVersion: 2\n"
             "Author: The Author\nMaintainer: The Maintainer\n"
    ))
    assert control_fields(path)["author"] == "The Author"


def test_a_stanza_with_no_package_field_yields_nothing(tmp_path):
    """`Package` is mandatory in Debian control, and the shared mapping returns
    None without it — so this degrades the same way an unreadable file does
    rather than emitting a half-filled row."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(text="Name: Orphaned\nVersion: 1\n"))
    assert control_fields(path) == {}


def test_empty_fields_are_omitted_rather_than_blank(tmp_path):
    """The shared mapping defaults absent fields to "", which as a row value
    would render an empty segment in `detailLine`'s ` · ` join."""
    path = tmp_path / "t.deb"
    path.write_bytes(_deb(text="Package: com.example.bare\n"))
    fields = control_fields(path)
    # `name` falls back to the identifier, so a stanza with only `Package:`
    # yields exactly those two and no empty `version`/`section`/`author`.
    assert fields == {"package_id": "com.example.bare", "name": "com.example.bare"}
    assert all(value for value in fields.values())
