"""The one teardown rule four modules now share.

`devices`, `device_apps`, `device_info` and `artwork`'s device rung each have
their own test for what a bare close cost *them* — a listing turned into a
connection error, a record turned into `reachable: false`, 67 icons discarded,
one icon discarded. Those stay where they are; they are about the caller's
consequence. These two are about the rule itself, which now exists once.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from graft_ipa.device_session import closing_best_effort


def test_a_close_that_fails_does_not_replace_the_answer() -> None:
    """The whole reason this is not `contextlib.aclosing`.

    Every caller computes its result inside the block and returns it; the close
    runs afterwards. A phone that drops off the network at `wait_closed()` must
    cost the session — which is gone regardless, that is what the failure means
    — and not the answer.
    """
    session = MagicMock()
    session.close = AsyncMock(side_effect=ConnectionResetError("phone went away"))

    async def read() -> str:
        async with closing_best_effort(session):
            return "the phone's complete answer"

    assert asyncio.run(read()) == "the phone's complete answer"
    session.close.assert_awaited_once()


def test_a_failure_inside_the_block_still_propagates() -> None:
    """A `finally`, not an `except`, and the difference is load-bearing.

    Three of the four callers depend on it: `list_installed_apps` is documented
    to raise so a connection failure doesn't read as an empty phone,
    `devices._describe_device` must surface as a `DeviceLookupFailure` rather
    than a silent drop, and `read_device_info` must return None for a phone it
    could not reach. Swallowing the body would turn all three into a success
    with no data — which is the failure mode each of those modules was written
    to end.

    The close still runs, so a body that failed halfway does not also leak.
    """
    session = MagicMock()
    session.close = AsyncMock()

    async def read() -> None:
        async with closing_best_effort(session):
            raise ConnectionError("the phone never answered")

    with pytest.raises(ConnectionError, match="never answered"):
        asyncio.run(read())
    session.close.assert_awaited_once()
