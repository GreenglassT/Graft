"""Stop, deferred: the steps of an update that must finish once begun (spec 3.4, §11.2).

`graft --update` runs under a `StopGate` instead of
`lc_manage.exit_on_termination`. A SIGTERM (the app's Stop), a SIGHUP or a
SIGINT (Ctrl-C in Terminal) raises `Stopped` at once, as in a build, except
while a step that must not be cut short is running:

- a push into LiveContainer or an install. The build calls
  `finish_before_stopping` as either begins, and the first Stop is then held
  until the update's runner releases it once that app's build has ended
  (`StopGate.release`): the phone doesn't get a half-written install, and the
  build's history row and delivery record are written. A **second** Stop
  cancels it (spec §11.2), as `device_session` cancels on a
  first Stop outside an update: at the await the session is in, so its
  clean-up runs (`absorb`). That is safe: a push writes `.graft-partial` and
  renames it, and installd applies an install or doesn't.
- a tweak's move into the library and the one config write that re-points
  every app using it (`StopGate.deferring`). This one is absolute, however
  many Stops arrive: it lasts milliseconds, and no app may be left half
  re-pointed.

The first Stop during either is announced (`update-stopping`), so the app can
say "Finishing the install, then stopping…" and hold off its SIGKILL. A
notice that failed to go out is tried again on the next Stop.

With no gate installed, as in every plain build, `finish_before_stopping`
does nothing, `active` is False and `absorb` takes nothing. Main thread only,
as every handler is.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Callable, Iterator

from .errors import Stopped

#: Says a Stop is waiting: (the app, or None for the tweak step; the step).
Announce = Callable[[str | None, str], None]

#: The gate the running update installed, if any.
_gate: StopGate | None = None

#: Taken whatever they were: the app's Stop.
_ALWAYS = (signal.SIGTERM,)
#: Taken unless they arrived ignored: `nohup graft --update …` (SIGHUP), and
#: a run started in the background of a shell, which ignores SIGINT.
_UNLESS_IGNORED = (signal.SIGHUP, signal.SIGINT)


class StopGate:
    def __init__(self, announce: Announce) -> None:
        self._announce = announce
        self._step: tuple[str | None, str] | None = None
        #: The step is a push or an install (`hold`), which a second Stop
        #: cancels; else a tweak write (`deferring`), which nothing cuts short.
        self._cancellable = False
        self._pending: int | None = None
        self._announced = False

    @property
    def deferring_now(self) -> bool:
        return self._step is not None

    @property
    def stop_pending(self) -> bool:
        return self._pending is not None

    @contextlib.contextmanager
    def installed(self) -> Iterator[StopGate]:
        """Take SIGTERM, and SIGHUP and SIGINT unless they arrived ignored,
        for the block."""
        global _gate
        if threading.current_thread() is not threading.main_thread():
            yield self
            return
        taken = list(_ALWAYS) + [s for s in _UNLESS_IGNORED if signal.getsignal(s) != signal.SIG_IGN]
        previous = {sig: signal.signal(sig, self._on_signal) for sig in taken}
        _gate = self
        try:
            yield self
        finally:
            _gate = None
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _on_signal(self, signum: int, _frame: object) -> None:
        if not self.absorb(signum):
            raise Stopped(signum)

    def absorb(self, signum: int) -> bool:
        """Take this Stop without stopping anything: True during a tweak
        write, and for the first Stop during a push or an install, which is
        then pending (and announced). False otherwise: no step is held, or a
        Stop is already pending during a push or an install, which this one
        cancels."""
        if self._step is None:
            return False
        if self._cancellable and self._pending is not None:
            return False
        if self._pending is None:
            self._pending = signum
        if not self._announced:
            app, step = self._step
            try:
                self._announce(app, step)
            except Exception:  # losing the notice costs only the app's wording; the next Stop retries
                pass
            else:
                self._announced = True
        return True

    def hold(self, app: str | None, step: str) -> None:
        """From now until `release`, a first Stop waits; a second cancels."""
        if self._step is None:
            self._step = (app, step)
            self._cancellable = True

    def release(self) -> None:
        """End a hold; a Stop that arrived during it takes effect now."""
        self._step = None
        self._cancellable = False
        if self._pending is not None:
            raise Stopped(self._pending)

    @contextlib.contextmanager
    def deferring(self, app: str | None, step: str) -> Iterator[None]:
        """Every Stop during the block waits for its end. A failure in the
        block with a Stop waiting ends as the Stop."""
        if self._step is not None:
            yield
            return
        self._step = (app, step)
        self._cancellable = False
        try:
            yield
        except BaseException as exc:
            self._step = None
            if self._pending is not None and not isinstance(exc, Stopped):
                raise Stopped(self._pending) from exc
            raise
        self.release()


def finish_before_stopping(app: str | None, step: str) -> None:
    """Called by a build as a push or an install begins: with an update's gate
    installed, a first Stop from here on waits for the update to release it. A
    no-op otherwise."""
    if _gate is not None:
        _gate.hold(app, step)


def installed() -> bool:
    """An update's gate is installed (`device_session` then takes SIGINT as a
    Stop, as the gate does)."""
    return _gate is not None


def active() -> bool:
    """A Stop is being held or deferred."""
    return _gate is not None and _gate.deferring_now


def absorb(signum: int) -> bool:
    """`StopGate.absorb` on the installed gate, for `device_session`'s own
    handler, which is in place while a session runs: True when the session
    should run on, False when this Stop should cancel it. False with no gate."""
    return _gate is not None and _gate.absorb(signum)
