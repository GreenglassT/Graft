"""How this project closes a pymobiledevice3 session — one rule, one place.

Four modules open a lockdown session (or a service on top of one), read an
answer out of it and hand that answer back: `devices`, `device_apps`,
`device_info`, and `artwork`'s device rung. All four need the same two things
from the teardown, and getting either wrong has cost this project a shipped
defect.

**It must happen.** `asyncio.run` tears the event loop down with the socket
still live, so a session that is never closed leaks on the *phone*, per call —
and the GUI runs these on a timer. Measured under `-X dev` against the test
phone, one `devices.list_devices()` call with one device attached: five
unclosed resources (two usbmux sockets, two socket transports, one SSL
transport) with no teardown at all, three with the lockdown session closed
alone, none once the InstallationProxy service is closed too.

**It must not throw away the answer.** This is the half that keeps being got
wrong, and the reason it has a name here instead of a `try`/`finally` per site.
Every one of these functions computes its result *before* the close runs: the
`DeviceInfo` is built, the 67 apps are listed, the icons are decoded. A
Wi-Fi-paired phone that drops off the network between the last read and
`wait_closed()` raises out of `close()`, that exception replaces the pending
return, and the caller — which cannot tell a teardown failure from a phone that
never answered — reports a failure over an answer it already had. Shipped four
times, in four shapes:

* `devices._describe_device`: a complete `DeviceInfo` became a
  `DeviceLookupFailure`, and the sidebar drew "connected, but couldn't be read"
  over a phone it had just read perfectly — the disappearing-phone defect that
  module exists to end, reintroduced one line from the end of the function that
  fixed it.
* `device_info._read_async`: a `DeviceInfoRecord` became None, and the CLI
  emitted `reachable: false` for a phone whose storage and battery it had just
  read.
* `device_apps._icons_from_device_async`: 67 icons read over ~4s were discarded
  and none of them cached, so the next run paid the 4s again.
* `artwork._icon_from_device_async`: an icon that had already been fetched was
  discarded by `icon_from_device`'s broad `except`.

Discarding finished work is never the right price for a failed teardown. The
session is gone either way — that is what the failure means — so there is
nothing left to leak and nothing to gain by reporting it.

What is emphatically *not* suppressed is the body. This is a `finally`, not an
`except`: an exception raised by the code inside the `async with` propagates
untouched, so a lockdown that fails mid-describe is still a
`DeviceLookupFailure`, an unreachable phone is still None, and
`list_installed_apps` still raises rather than looking like an empty phone.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any


@contextlib.asynccontextmanager
async def closing_best_effort(session: Any) -> AsyncIterator[Any]:
    """Yield `session`, then close it, tolerating a close that fails.

    `session` is anything with an awaitable `close()` — a `LockdownClient`, or
    a service such as `InstallationProxyService` that opened a connection of its
    own and is not closed by closing the lockdown session it was built on.

    Not `contextlib.aclosing`, which is the same shape without the suppression
    and is therefore exactly the bug this module documents.
    """
    try:
        yield session
    finally:
        with contextlib.suppress(Exception):
            await session.close()
