"""`secret_input`: a line from a file descriptor, held where it can be wiped."""

from __future__ import annotations

import os

import pytest

from graft_ipa import secret_input
from graft_ipa.secret_input import SecretLine, SecretTooLong, read_line


@pytest.fixture
def pipe():
    r, w = os.pipe()
    yield r, w
    for fd in (r, w):
        try:
            os.close(fd)
        except OSError:
            pass


def test_one_line_is_read_and_the_next_stays_in_the_pipe(pipe) -> None:
    """The code arrives on the same stdin minutes after the password. A read
    that took more than one line would swallow it."""
    r, w = pipe
    os.write(w, b"first secret\nsecond\n")
    with read_line(r) as line:
        assert bytes(line.view) == b"first secret"
    with read_line(r) as line:
        assert bytes(line.view) == b"second"


def test_a_crlf_ending_is_dropped(pipe) -> None:
    r, w = pipe
    os.write(w, b"abc\r\n")
    assert bytes(read_line(r).view) == b"abc"


def test_end_of_input_before_anything_is_none(pipe) -> None:
    r, w = pipe
    os.close(w)
    assert read_line(r) is None


def test_a_last_line_without_a_newline_still_counts(pipe) -> None:
    r, w = pipe
    os.write(w, b"abc")
    os.close(w)
    assert bytes(read_line(r).view) == b"abc"


def test_no_input_in_time_is_a_timeout(pipe) -> None:
    r, _ = pipe
    with pytest.raises(TimeoutError):
        read_line(r, timeout=0.05)


def test_a_runaway_line_is_refused(pipe) -> None:
    r, w = pipe
    os.write(w, b"x" * 40)
    with pytest.raises(SecretTooLong):
        read_line(r, capacity=16)


def test_a_failed_read_wipes_what_it_had(pipe, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[SecretLine] = []
    real = secret_input.SecretLine

    def recording(capacity: int) -> SecretLine:
        made.append(real(capacity))
        return made[-1]

    monkeypatch.setattr(secret_input, "SecretLine", recording)
    r, w = pipe
    os.write(w, b"partial")
    with pytest.raises(TimeoutError):
        read_line(r, timeout=0.05)
    assert made[0]._buf == bytearray(len(made[0]._buf))


def test_wipe_zeroes_the_whole_buffer() -> None:
    line = SecretLine(8)
    line._buf[:5] = b"hunte"
    line._len = 5
    line.wipe()
    assert line._buf == bytearray(8)
    assert len(line) == 0


def test_leaving_the_with_block_wipes(pipe) -> None:
    r, w = pipe
    os.write(w, b"hunter2\n")
    with read_line(r) as line:
        pass
    assert line._buf == bytearray(len(line._buf))


def test_the_repr_never_shows_the_secret(pipe) -> None:
    r, w = pipe
    os.write(w, b"hunter2\n")
    line = read_line(r)
    assert "hunter2" not in repr(line)
    assert "hunter2" not in str(line)


def test_redact_replaces_every_copy(pipe) -> None:
    r, w = pipe
    os.write(w, b"hunter2\n")
    line = read_line(r)
    assert secret_input.redact("bad hunter2 and hunter2!", line) == "bad [redacted] and [redacted]!"


def test_redact_ignores_a_missing_or_empty_secret() -> None:
    assert secret_input.redact("text", None, SecretLine()) == "text"


@pytest.mark.parametrize(
    ("typed", "ok"),
    [(b"482913", True), (b"482 913", True), (b"48291", False), (b"48291a", False), (b"4829130", False)],
)
def test_a_code_is_six_digits_once_spaces_are_dropped(pipe, typed: bytes, ok: bool) -> None:
    """ipatool's own normalizeAuthCode strips white space, then wants six digits."""
    r, w = pipe
    os.write(w, typed + b"\n")
    line = read_line(r)
    secret_input.drop_spaces(line)
    assert secret_input.is_six_digits(line) is ok


def _echo_during(prompt_kwargs: dict) -> bool:
    """Whether the terminal echoed while `prompt_line` read, on a pty."""
    import os
    import pty
    import termios
    import threading

    from graft_ipa import secret_input

    master, slave = pty.openpty()
    seen: list[bool] = []
    real = secret_input.read_line

    def spy(fd: int, **kw):
        seen.append(bool(termios.tcgetattr(fd)[3] & termios.ECHO))
        return real(fd, **kw)

    try:
        secret_input.read_line = spy
        threading.Timer(0.1, lambda: os.write(master, b"typed\n")).start()
        line = secret_input.prompt_line(slave, "ask: ", timeout=5, **prompt_kwargs)
        assert line is not None and bytes(line.view) == b"typed"
    finally:
        secret_input.read_line = real
        os.close(master)
        os.close(slave)
    return seen[0]


def test_a_secret_prompt_turns_echo_off(capsys) -> None:
    assert _echo_during({}) is False


def test_the_apple_id_prompt_keeps_echo_on(capsys) -> None:
    """The Apple ID isn't secret; it is only kept off argv (decision 3)."""
    assert _echo_during({"echo": True}) is True
