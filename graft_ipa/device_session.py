"""How this project opens, runs and closes a pymobiledevice3 session.

Opening: `open_lockdown`, which tells the user to tap Trust and gives up after
`PAIR_TIMEOUT_SECONDS`, where pymobiledevice3 waits for ever in silence.
Running: `run`, under which a Stop cancels the session's task at an await
instead of tearing the event loop down around a clean-up still waiting on the
phone. Closing: the rest of this docstring, and `Teardown` for a clean-up that
must not hang.

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

import asyncio
import contextlib
import contextvars
import logging
import signal
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
from typing import Any, TypeVar

T = TypeVar("T")

# pymobiledevice3 logs through `logging`, and graft configures none, so its
# WARNING and ERROR records go to stderr through `logging.lastResort`: every
# Stop mid-upload printed `AFC: no waiter for packet_num=N` there. The app
# shows a failed run's stderr as its log, so that line read as part of the
# failure. A NullHandler is a handler, which is all `lastResort` checks for;
# records still propagate, so a root logger someone does configure sees them.
logging.getLogger("pymobiledevice3").addHandler(logging.NullHandler())


@contextlib.asynccontextmanager
async def closing_best_effort(
    session: Any, *, within: Teardown | None = None,
) -> AsyncIterator[Any]:
    """Yield `session`, then close it, tolerating a close that fails.

    `session` is anything with an awaitable `close()` — a `LockdownClient`, or
    a service such as `InstallationProxyService` that opened a connection of its
    own and is not closed by closing the lockdown session it was built on.

    `within` puts the close on a clean-up's time budget (`Teardown`).

    Not `contextlib.aclosing`, which is the same shape without the suppression
    and is therefore exactly the bug this module documents.
    """
    try:
        yield session
    finally:
        if within is not None:
            await within(session.close())
        else:
            with contextlib.suppress(Exception):
                await session.close()


# ---------- a clean-up that cannot hang ----------

#: How long a session's clean-up may take, every wait in it together. Under
#: the app's `cliStopGrace` (10 s), after which it SIGKILLs graft and nothing
#: of graft's own clean-up runs: not the build's temp folder, not `build-end`.
TEARDOWN_SECONDS = 5.0


class Teardown:
    """One time budget, shared by every wait of one clean-up.

    Closing a file, removing it and closing the services each wait for the
    phone, and a phone that has stopped answering answers none of them. One
    budget rather than one per wait, so five waits cannot add up to more
    than the app's grace. It starts at the first wait; each wait gets what is
    left, and failing (or running out) is suppressed: a clean-up is best
    effort.

    A second Stop ends the clean-up (re-check D2). Its cancellation is not
    suppressed, so the wait in progress ends; and once `run` has had two
    Stops, every later wait, of this `Teardown` or any other in the session,
    is skipped. Canceling the one wait was not enough: the next `finally`
    started a fresh wait on the rest of the budget, and graft still went on
    for all of it.
    """

    def __init__(self, seconds: float | None = None) -> None:
        self._seconds = seconds
        self._deadline: float | None = None

    async def __call__(self, step: Awaitable[Any]) -> None:
        if len(_stops.get()) > 1:
            if asyncio.iscoroutine(step):
                step.close()  # never started: no "was never awaited"
            return
        loop = asyncio.get_running_loop()
        if self._deadline is None:
            seconds = TEARDOWN_SECONDS if self._seconds is None else self._seconds
            self._deadline = loop.time() + seconds
        with contextlib.suppress(Exception):  # TimeoutError included
            await asyncio.wait_for(step, max(0.0, self._deadline - loop.time()))


# ---------- a Stop that lands at an await ----------

_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)

#: The Stops the session running in this context has had, in order: the
#: list `run` appends to, read by `Teardown`. Empty outside `run`, and off
#: the main thread, where no Stop reaches a session.
_stops: contextvars.ContextVar[Sequence[int]] = contextvars.ContextVar("graft_stops", default=())


def run(coro: Coroutine[Any, Any, T]) -> T:
    """`asyncio.run(coro)`, where a Stop cancels `coro` instead of tearing
    through it.

    `lc_manage.exit_on_termination` raises `Stopped` at whatever bytecode the
    main thread is on. Inside `asyncio.run` that is usually `select()`: the
    `Stopped` then leaves the loop, and `asyncio.run`'s teardown cancels every
    task at once, pymobiledevice3's AFC reader among them. The install's
    clean-up then sends FILE_CLOSE and waits for a reply nothing is left to
    deliver, and graft hangs until the app SIGKILLs it: no `build-end`, the
    build's temp folder left behind, the partial IPA left on the phone.

    So while `coro` runs on the main thread, SIGTERM and SIGHUP cancel its task
    instead. That lands at the await it is in, as Ctrl-C does under asyncio,
    with the reader still running, so the clean-up's replies arrive (and
    `Teardown` bounds them when they don't). A second Stop ends that
    clean-up where it is (`Teardown`, `_stops`). Once `asyncio.run` has returned,
    the signal goes to the handler it interrupted, which is
    `exit_on_termination`'s, and that raises `Stopped` as before; under the
    default handler, the process ends as it would have. A signal that was
    ignored stays ignored (`nohup`).

    Off the main thread no handler is set: handlers can only be set on the
    main thread, and `--lc-install`'s worker is abandoned on a Stop by design
    (`lc_manage._install_one`).

    On either thread, what the loop would report on its own (`Future
    exception was never retrieved`, with pymobiledevice3's traceback) goes to
    this module's logger at DEBUG instead of stderr. A phone that drops
    mid-upload produces exactly that: the AFC reader fails every pending
    request, including one whose sender had already given up on it. The
    install's own error says what happened; the app shows a failed run's
    stderr as its log, and a traceback there is noise.
    """
    received: list[int] = []
    try:
        return asyncio.run(_cancelled_by_signals(coro, received))
    finally:
        if received:
            # The interrupted handler's turn. `raise_signal` runs a Python
            # handler before it returns, so `Stopped` is raised from here,
            # in place of the CancelledError the stop left in flight.
            signal.raise_signal(received[0])


def _quietly(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
    _log.debug(
        "%s", context.get("message", "unhandled exception in the event loop"),
        exc_info=context.get("exception"),
    )


_log = logging.getLogger(__name__)


async def _cancelled_by_signals(coro: Coroutine[Any, Any, T], received: list[int]) -> T:
    loop = asyncio.get_running_loop()
    # Kept after the loop closes: a future collected then still reports here.
    loop.set_exception_handler(_quietly)
    task = asyncio.current_task()
    assert task is not None
    if threading.current_thread() is not threading.main_thread():
        return await coro

    def on_signal(signum: int, _frame: object) -> None:
        # Only a note and a wake-up, so it is safe at any bytecode: the
        # cancel itself happens in the loop, between its callbacks.
        received.append(signum)
        loop.call_soon_threadsafe(task.cancel)

    # In this task's context, so `coro`, which runs in this task, sees it.
    _stops.set(received)
    previous = {}
    for sig in _STOP_SIGNALS:
        if signal.getsignal(sig) in (signal.SIG_IGN, None):
            continue
        previous[sig] = signal.signal(sig, on_signal)
    try:
        return await coro
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


# ---------- a phone that has never trusted this Mac ----------

#: How long graft waits for the user to answer "Trust This Computer?". In
#: 1.1, `ideviceinstaller` failed at once; pymobiledevice3's default is to
#: wait for ever, in silence, so a first-time user saw an install that never
#: moved.
PAIR_TIMEOUT_SECONDS = 60.0

TRUST_PROMPT = "Unlock the phone and tap Trust on the 'Trust This Computer?' prompt"


#: Called with the prompt's text whenever `say_trust_prompt` runs. The CLI's
#: dispatch registers one under --json (`cli._trust_prompts_as_events`) so the
#: prompt is also a `log` event the app shows live: the app reads stderr only
#: after a non-zero exit, which for this prompt is after the wait has failed.
_TRUST_LISTENERS: list[Callable[[str], None]] = []


@contextlib.contextmanager
def trust_prompt_listener(listener: Callable[[str], None]):
    """Have `listener` hear every trust prompt while the block runs."""
    _TRUST_LISTENERS.append(listener)
    try:
        yield
    finally:
        _TRUST_LISTENERS.remove(listener)


def say_trust_prompt() -> None:
    """The default `on_trust_prompt`: a line on stderr, where a terminal user
    sees it while graft waits, and to every registered listener (the app's
    live log, under --json). A listener that fails never stops the prompt."""
    line = f"{TRUST_PROMPT} (waiting up to {PAIR_TIMEOUT_SECONDS:.0f} s)"
    print(line, file=sys.stderr, flush=True)
    for listener in list(_TRUST_LISTENERS):
        with contextlib.suppress(Exception):
            listener(line)


async def open_lockdown(
    udid: str | None, *,
    on_trust_prompt: Callable[[], None] | None = say_trust_prompt,
    pair_timeout: float | None = None,
) -> Any:
    """A lockdown session with `udid`, pairing first if the phone asks.

    pymobiledevice3's `create_using_usbmux` pairs by itself, and waits for the
    Trust dialog with no limit and no word (its 'waiting user pairing
    dialog...' is an INFO record nobody shows). Here the session is opened
    without that, and when the phone has no pairing with this Mac, the user
    is told (`on_trust_prompt`) before the same `pair` and the same check
    `_handle_autopair` does, bounded by `pair_timeout`. Out of time, it raises
    pymobiledevice3's `PairingDialogResponsePendingError`, which
    `installer._install_hint` explains. A tap on Don't Trust is its
    `UserDeniedPairingError`; a locked phone, `PasswordRequiredError`.
    """
    from pymobiledevice3.exceptions import FatalPairingError
    from pymobiledevice3.lockdown import create_using_usbmux

    lock = await create_using_usbmux(serial=udid, autopair=False)
    try:
        if not lock.paired:
            if on_trust_prompt is not None:
                on_trust_prompt()
            await lock.pair(
                timeout=PAIR_TIMEOUT_SECONDS if pair_timeout is None else pair_timeout
            )
            if not await lock.validate_pairing():
                raise FatalPairingError()
    except BaseException:
        with contextlib.suppress(Exception):
            await lock.close()
        raise
    return lock
