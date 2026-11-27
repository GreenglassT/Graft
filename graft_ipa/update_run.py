"""`graft --update`: the plan, then the work, in the spec's order (3.2).

1. Plan every named app, fully (`update_plan`, readiness checked), and emit it.
2. Update every tweak in any runnable app's tweak step, once each, in library
   order (`update_tweaks`). A tweak update re-points every app that uses it,
   which is why this comes before any build.
3. For each runnable app, one at a time, in the order given (sidebar order
   for `--all`): first an `install` skip for each target that won't install
   (waiting, blocked, up to date, or only offered); the App Store get, if
   planned, through the download-and-stage path alone (`appstore.fetched`
   and `cli._stage_ipa`), so it never lets go of a hold and never claims or
   buys anything; then **one build**, which installs on the first target to
   install, and the same archive on each other one in turn
   (`cli._do_update_install`: the re-install path, recorded with origin
   `update` and the build's inputs digest), spec §11.3.
4. A result per app (`update-app-end`, with one per target) and a summary
   (`update-end`).

A target that fails leaves the app's others to run, once the build has made
its archive; one the build refused before making it (Developer Mode off
there, a stranger's IPA in its LiveContainer) fails alone, and the next
target builds. A target whose device isn't connected, since the plan or
during its build or install, waits: that isn't a failure. Every step skips
itself when it's already done, so `--update` again resumes where a run
failed or was stopped (3.3), on the targets that aren't current. Stops are
immediate except where `stop_deferral` says (3.4):
each install is held, then released before the next target starts, so the
targets after a Stop are "not started".

`--plan` is `show_plan`: step 1 alone, and only `update-plan`. It reads no
keychain and runs no ipatool.
"""

from __future__ import annotations

import contextlib
import signal
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import (
    appstore,
    ipatool,
    tweak_sources,
    update_plan,
    update_readiness,
    update_refusals,
    update_tweaks,
)
from .config import Config, load_config
from .errors import (
    AppStoreError,
    DeviceNotConnectedError,
    NoDeviceConnectedError,
    Stopped,
    presented,
)
from .json_output import JsonEmitter
from .stop_deferral import StopGate
from .update_plan import STEP_BUILD, STEP_GET, STEP_INSTALL, STEP_TWEAKS, AppPlan, TargetPlan

#: `update-step`'s statuses (`CLIContract.Update` in the app).
START = "start"
END = "end"
SKIPPED = "skipped"

DOESNT_HAVE = "your Apple ID doesn't have this app; get it in the App Store first"
NOT_STARTED = "not started"
NOTHING_NEWER = "nothing newer"
NO_TWEAK_UPDATES = "no tweak updates"
TWEAK_FAILED_ONLY = "nothing else changed, and the tweak update failed"
#: An `install` skip for a target already current.
UP_TO_DATE = "up to date"
#: The build step's end when every device it would install on was unplugged
#: since the plan (each then waits).
NONE_CONNECTED = "none of its devices is connected"


@dataclass
class Built:
    """What an app's one build made, for the installs after the first
    (`cli._build_one`'s `outcome`): the archive, the build inputs' digest
    taken before cyan ran, the IPA version, and whether the build's own
    install (on the first target) finished and was recorded. `on_install`
    is told as that install begins."""

    on_install: Callable[[], None] | None = None
    #: The app's other devices the archive goes to next: the build signs for
    #: them too, so one profile covers every one (`signer.run_zsign`'s `also`).
    also_for: list[str] = field(default_factory=list)
    archive: Path | None = None
    inputs: Any = None
    version: str | None = None
    install_started: bool = False
    installed: bool = False
    #: Devices the build's profile doesn't cover (App Store Connect has no
    #: enabled device for them): their installs are refused before a push.
    not_in_profile: tuple[str, ...] = ()
    #: The build refused its device alone, before building anything: a
    #: stranger's IPA in that device's LiveContainer Documents, or Developer
    #: Mode off there (`cli._refusing_for_the_device`). The app's next device
    #: can still build.
    target_refused: bool = False
    #: The device the next install goes to (`expecting`): how to ask whether
    #: it's on usbmux now, and the sentence it waits with.
    connected: Callable[[], bool] | None = None
    waiting_sentence: str | None = None
    #: That device's answer, once asked (`gone`).
    _gone: bool | None = field(default=None, init=False, repr=False)

    def archived(self, path: Path | None, inputs: Any, version: str) -> None:
        self.archive, self.inputs, self.version = path, inputs, version

    def expecting(self, connected: Callable[[], bool], sentence: str) -> None:
        """The install that comes next is to the device `connected` asks
        about, which waits with `sentence`: the build's own, then each later
        one's (`_run_app`)."""
        self.connected, self.waiting_sentence, self._gone = connected, sentence, None

    def gone(self) -> bool:
        """Whether that device isn't connected now. Asked once per install,
        so the install's own `build-end` (`waits`, `install_waits`) and
        `_run_app` read the same answer, and can't tell two stories."""
        if self._gone is None:
            self._gone = self.connected is not None and not self.connected()
        return self._gone

    def waits(self, exc: BaseException | None) -> str | None:
        """The sentence the build's device waits with, when the build failing
        with `exc` leaves it waiting rather than failed; else None. As
        `_run_app` reads the same failure: unplugged before anything was
        built (the build's own `DeviceNotConnectedError`), or gone once the
        archive was made and before its install was done (T2-B7)."""
        if exc is None or self.installed:
            return None
        if self.archive is None:
            unplugged = isinstance(exc, (DeviceNotConnectedError, NoDeviceConnectedError))
            return self.waiting_sentence if unplugged and not self.install_started else None
        return self.waiting_sentence if self.gone() else None

    def install_waits(self, exc: BaseException | None) -> str | None:
        """`waits`, for a later device's install (`cli._do_update_install`):
        not connected when it was looked for, or gone by the time it
        failed."""
        if exc is None:
            return None
        if isinstance(exc, (DeviceNotConnectedError, NoDeviceConnectedError)) or self.gone():
            return self.waiting_sentence
        return None

    def installing(self) -> None:
        if self.install_started:
            return
        self.install_started = True
        if self.on_install is not None:
            self.on_install()


@dataclass(frozen=True)
class Pipeline:
    """The parts of a get and a build that live in cli.py
    (`cli._update_pipeline`): handed in, so this module imports nothing from
    cli.py, and a test can stand in for the build."""

    #: `cli._collect_tweak_records`.
    tweak_records: Callable[[Config], list[dict]]
    #: `cli._delivery_targets`.
    delivery: Callable[[Config], Callable[[str], update_plan.AppDeliveries]]
    #: `cli._stage_ipa`.
    stage_ipa: Callable[..., str]
    #: `cli._build_one`, for one app, delivering to `device`, filling `built`.
    build: Callable[..., None]
    #: `cli._do_update_install`: `built`'s archive on another `device`.
    install: Callable[..., None]
    #: A line for a person (`cli._say_plain`).
    say: Callable[[str], None]
    #: A warning on both channels (`cli._warn`).
    warn: Callable[[str], None]


def emit_plan(plans: list[AppPlan], *, quick: bool, emitter: JsonEmitter | None,
              say: Callable[[str], None]) -> None:
    if emitter:
        emitter.emit("update-plan", **update_plan.payload(plans, quick=quick))
        return
    for p in plans:
        say(update_plan.describe(p))


def _plan(cfg: Config, names: list[str], *, quick: bool, with_affected: bool, probe,
          pipeline: Pipeline, trusted: Mapping[str, str] | None = None) -> list[AppPlan]:
    return update_plan.plan(cfg, names, quick=quick, with_affected=with_affected, probe=probe,
                            tweak_records=pipeline.tweak_records, delivery=pipeline.delivery(cfg),
                            trusted=trusted)


def show_plan(cfg: Config, names: list[str], *, quick: bool, with_affected: bool, probe,
              pipeline: Pipeline, emitter: JsonEmitter | None,
              trusted: Mapping[str, str] | None = None) -> None:
    """`--update --plan`: what would change, and nothing changed."""
    plans = _plan(cfg, names, quick=quick, with_affected=with_affected, probe=probe, pipeline=pipeline,
                  trusted=trusted)
    emit_plan(plans, quick=quick, emitter=emitter, say=pipeline.say)


@dataclass(frozen=True)
class _TargetResult:
    """One target's part of `update-app-end`'s `targets`."""

    ok: bool
    error: str | None = None
    left: list[str] = field(default_factory=list)
    #: Its device isn't connected: not a failure (`update-app-end`'s `ok`
    #: leaves it out), and it installs on a later run.
    waiting: bool = False


#: A target that installed, or had nothing to do here.
_FINE = _TargetResult(True)


def _not_done(error: str | None) -> _TargetResult:
    return _TargetResult(False, error, [STEP_INSTALL])


def _waiting(sentence: str) -> _TargetResult:
    return _TargetResult(False, sentence, [STEP_INSTALL], waiting=True)


def _as_planned(t: TargetPlan) -> _TargetResult:
    """A target's result in an app that didn't run, or didn't reach it: still
    to install (no reason: a Stop), waiting or blocked (its reason), or fine."""
    if t.install:
        return _not_done(None)
    if t.waiting:
        return _waiting(t.waiting)
    if t.blocked_reason:
        return _not_done(t.blocked_reason)
    return _FINE


@dataclass
class _Progress:
    """One runnable app, the steps it has done, and each target's result
    once it is settled."""

    plan: AppPlan
    done: list[str] = field(default_factory=list)
    results: dict[str, _TargetResult] = field(default_factory=dict)

    @property
    def app(self) -> str:
        return self.plan.app

    @property
    def left(self) -> list[str]:
        return [s for s in self.plan.steps if s not in self.done]

    @property
    def unsettled(self) -> list[TargetPlan]:
        """The targets to install that haven't been reached."""
        return [t for t in self.plan.targets if t.install and t.udid not in self.results]

    def targets(self) -> list[dict[str, Any]]:
        out = []
        for t in self.plan.targets:
            r = self.results.get(t.udid) or _as_planned(t)
            out.append({"udid": t.udid, "device_name": t.device_name, "ok": r.ok,
                        "error": r.error, "left": list(r.left), "waiting": r.waiting})
        return out


def _quiet(_text: str) -> None:
    pass


@dataclass
class _Run:
    """The events a run emits, and its tally for `update-end`."""

    emitter: JsonEmitter | None
    say: Callable[[str], None]
    items: list[_Progress] = field(default_factory=list)
    current: _Progress | None = None
    finished: int = 0
    updated: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)
    not_started: list[str] = field(default_factory=list)
    #: Targets waiting for their device, across the run.
    waiting: int = 0
    #: The tweaks that couldn't be updated, with why (`_update_tweaks`).
    failures: dict[str, str] = field(default_factory=dict)

    def step(self, app: str | None, step: str, status: str, detail: str | None = None,
             target: TargetPlan | None = None, *, waiting: bool = False) -> None:
        """One `update-step`. An install's says whether it ended or was
        skipped because its device isn't connected (`waiting`)."""
        udid = target.udid if target is not None else None
        name = target.device_name if target is not None else None
        if self.emitter:
            extra: dict[str, Any] = {"waiting": waiting} if step == STEP_INSTALL else {}
            self.emitter.emit("update-step", app=app, step=step, status=status, detail=detail,
                              udid=udid, device_name=name, **extra)
        elif status == START and app is None:
            # The shared tweak step, named once as the app's sheet names it:
            # "→ tweaks: 1 tweak" (`AppUpdateText.logLines`).
            self.say(f"→ {step}" + (f": {detail}" if detail else ""))
        elif status == START:
            where = f" on {target.label}" if target is not None else ""
            self.say(f"→ {app}: {step}{where}" + (f" {detail}" if detail else ""))
        elif status == SKIPPED and target is not None and detail != UP_TO_DATE:
            self.say(f"  {app}: not installing on {target.label}: {detail}")

    def app_end(self, item: _Progress, *, ok: bool, stopped: bool, error: str | None,
                bucket: list[str]) -> None:
        bucket.append(item.app)
        targets = item.targets()
        if self.emitter:
            self.emitter.emit("update-app-end", app=item.app, ok=ok, stopped=stopped,
                              done=list(item.done), left=item.left, error=error, targets=targets)
            return
        left = item.left
        if ok:
            self.say(f"✓ {item.app}: updated")
        else:
            what = "stopped" if stopped else (error or "failed")
            self.say(f"✗ {item.app}: {what}" + (f" (left: {', '.join(left)})" if left else ""))
        for t, r in zip(item.plan.targets, targets, strict=True):
            if not r["ok"] and r["error"] and r["error"] != error:
                self.say(f"  {t.label}: {r['error']}")

    def stopped(self) -> None:
        """A Stop: the app it landed on, and every app after it, say so; then
        `update-end`. An app whose last install had already finished is
        updated; its targets not reached are "not started"."""
        with _stops_held():
            rest = self.items[self.finished:]
            if self.current is not None and rest and rest[0] is self.current:
                item = self.current
                if item.left or item.unsettled:
                    self.app_end(item, ok=False, stopped=True, error=None, bucket=self.failed)
                else:
                    ok, error = _outcome(item, self.failures)
                    self.app_end(item, ok=ok, stopped=False, error=error,
                                 bucket=self.updated if ok else self.failed)
                rest = rest[1:]
            for item in rest:
                self.app_end(item, ok=False, stopped=False, error=NOT_STARTED, bucket=self.not_started)
            self.end(stopped=True)

    def end(self, *, stopped: bool) -> bool:
        ok = not stopped and not self.failed and not self.blocked and not self.not_started
        if self.emitter:
            self.emitter.emit("update-end", ok=ok, stopped=stopped, updated=self.updated,
                              failed=self.failed, blocked=self.blocked, not_started=self.not_started,
                              waiting=self.waiting)
        else:
            self.say(
                f"updated: {', '.join(self.updated) or 'none'}; failed: {', '.join(self.failed) or 'none'}; "
                f"blocked: {', '.join(self.blocked) or 'none'}"
                + (f"; not started: {', '.join(self.not_started)}" if self.not_started else "")
                + (f"; waiting for {self.waiting} device{'s' if self.waiting != 1 else ''}"
                   if self.waiting else "")
            )
        return ok


@contextlib.contextmanager
def _stops_held() -> Iterator[None]:
    """SIGTERM, SIGHUP and SIGINT held for a few lines of bookkeeping, then
    delivered (and raised), as `_BuildReport.__exit__` holds them: a result is
    emitted once, and the tally agrees with it."""
    held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, held)


def _message(e: BaseException) -> str:
    # A full disk, said as one, whichever step met it (`errors.presented`,
    # the 2.3.1 review's T2-B6).
    return str(presented(e)) or type(e).__name__


def _announcer(emitter: JsonEmitter | None, pipeline: Pipeline) -> Callable[[str | None, str], None]:
    def announce(app: str | None, step: str) -> None:
        if emitter:
            emitter.emit("update-stopping", app=app, step=step)
            return
        what = "tweak update" if step == STEP_TWEAKS else "install"
        pipeline.say(f"finishing the {what}, then stopping…")

    return announce


def _update_tweaks(cfg: Config, state: _Run, *, gate: StopGate, pipeline: Pipeline) -> dict[str, str]:
    """Every tweak in a runnable app's tweak step, once, in library order.
    Returns the ones that failed, with why."""
    todo: dict[str, update_plan.TweakChange] = {}
    for item in state.items:
        for change in item.plan.tweak_updates:
            todo.setdefault(change.filename, change)
    if not todo:
        state.step(None, STEP_TWEAKS, SKIPPED, NO_TWEAK_UPDATES)
        return {}
    users = {r["filename"]: list(r.get("used_by") or []) for r in pipeline.tweak_records(cfg)}
    state.step(None, STEP_TWEAKS, START, "1 tweak" if len(todo) == 1 else f"{len(todo)} tweaks")
    failures: dict[str, str] = {}
    finished: set[str] = set()

    def applied(filename: str) -> None:
        # Inside the tweak's deferred step: counted even when a Stop is
        # waiting to be raised as that step ends.
        finished.add(filename)
        for item in state.items:
            wanted = {c.filename for c in item.plan.tweak_updates}
            if wanted and wanted <= finished and STEP_TWEAKS not in item.done:
                item.done.append(STEP_TWEAKS)

    for filename in sorted(todo):
        try:
            update_tweaks.update(cfg, todo[filename].status, users.get(filename, []), gate=gate,
                                 emitter=state.emitter, on_done=lambda _done, f=filename: applied(f),
                                 trusted=todo[filename].trusted)
        except Exception as e:  # one tweak's failure is that tweak's, not the run's
            failures[filename] = _message(e)
            pipeline.warn(f"couldn't update {filename}: {_message(e)}")
    state.step(None, STEP_TWEAKS, END, f"{len(failures)} couldn't be updated" if failures else None)
    return failures


def _trust_changed_error(p: AppPlan) -> str:
    return "; ".join(
        tweak_sources.TRUST_CHANGED.format(filename=t.name or t.filename, version=t.to_version,
                                           source=t.source)
        for t in p.trust_changed
    ) + ". Look at it again, and confirm it if you trust it"


def _get(cfg: Config, plan: AppPlan, *, emitter: JsonEmitter | None, pipeline: Pipeline) -> str:
    """The App Store step: download, decrypt and stage, and nothing else.
    Never `--appstore-get`'s verb, which lets go of a hold, and never a claim:
    an app the Apple ID doesn't have is refused, not bought."""
    app = cfg.get_app(plan.app)
    staged = appstore.staged_as_get(cfg, app, plan.store.to_version)
    if staged is not None:
        appstore.report_already_staged(plan.app, staged, emitter=emitter, say=pipeline.say)
        return staged.version
    with appstore.fetched(cfg, plan.app, emitter=emitter, say=pipeline.say, warn=pipeline.warn) as decrypted:
        return pipeline.stage_ipa(cfg, app_name=plan.app, source_path=decrypted, emitter=emitter)


def _tweak_error(failed: list[str], failures: dict[str, str]) -> str:
    return "couldn't update " + "; ".join(f"{f}: {failures[f]}" for f in failed)


def _outcome(item: _Progress, failures: dict[str, str]) -> tuple[bool, str | None]:
    """The app's `ok` and `error` once its targets are settled: ok when every
    target that isn't waiting is, and its tweaks were updated."""
    p = item.plan
    failed = sorted(c.filename for c in p.tweak_updates if c.filename in failures)
    tweak_error = _tweak_error(failed, failures) if failed else None
    errors = [tweak_error] if tweak_error else []
    results = [item.results.get(t.udid) or _as_planned(t) for t in p.targets]
    for t, r in zip(p.targets, results, strict=True):
        # A target whose only change was that tweak says nothing more.
        if not r.ok and not r.waiting and r.error and r.error not in (TWEAK_FAILED_ONLY, tweak_error):
            errors.append(f"{t.label}: {r.error}")
    ok = not failed and all(r.ok for r in results if not r.waiting)
    return ok, "; ".join(dict.fromkeys(errors)) or None


def _skip(t: TargetPlan, *, wanted: bool, tweak_error: str | None) -> tuple[str, _TargetResult]:
    """Why `t` won't install in this run, and its result."""
    if t.waiting is not None:
        return t.waiting, _waiting(t.waiting)
    if t.blocked_reason is not None:
        return t.blocked_reason, _not_done(t.blocked_reason)
    if wanted:  # its only change was a tweak update that failed
        return TWEAK_FAILED_ONLY, _not_done(tweak_error or TWEAK_FAILED_ONLY)
    if t.install_skipped_reason is not None:
        return t.install_skipped_reason, _FINE
    return UP_TO_DATE, _FINE


def _none_installed(app: str, targets: list[TargetPlan], error: str, *, item: _Progress,
                    state: _Run) -> None:
    """The get or the build failed: each target that would have installed
    gets its one `install` step, skipped with why, and that result."""
    for t in targets:
        item.results[t.udid] = _not_done(error)
        state.step(app, STEP_INSTALL, SKIPPED, error, target=t)


def _now_waiting(app: str, t: TargetPlan, *, item: _Progress, state: _Run,
                 started: bool = False) -> None:
    """`t`'s device was unplugged since the plan, or during its build or
    install: it waits, as the plan would have said, and counts in
    `update-end`'s `waiting`. `started`: its install step had begun, so this
    ends it rather than skipping it."""
    sentence = update_readiness.WAITING.format(device=t.label)
    item.results[t.udid] = _waiting(sentence)
    state.waiting += 1
    state.step(app, STEP_INSTALL, END if started else SKIPPED, sentence, target=t, waiting=True)


def _connected(cfg: Config, app: str, t: TargetPlan) -> bool:
    """Whether `t`'s device is on usbmux now, asked as the plan asked
    (`update_readiness.connection`). Asked before its build or install, so a
    device unplugged since the plan only waits, with no build of its own
    that fails for it. A failure to ask is "connected": the build or install
    then says what went wrong."""
    try:
        return update_readiness.connection(cfg, cfg.get_app(app), t.udid)[1] is None
    except Exception:
        return True


def _run_app(cfg: Config, item: _Progress, failures: dict[str, str], *, state: _Run,
             gate: StopGate, pipeline: Pipeline) -> tuple[bool, str | None]:
    p = item.plan
    failed = sorted(c.filename for c in p.tweak_updates if c.filename in failures)
    tweak_error = _tweak_error(failed, failures) if failed else None
    # The build changes when its IPA or a tweak does; a target whose own
    # delivery state calls for an install takes it either way.
    changes = p.store.planned or len(failed) < len(p.tweak_updates)
    needed = [t for t in p.targets if t.install and (changes or t.stale_change)]
    for t in p.targets:
        if t not in needed:
            detail, result = _skip(t, wanted=t.install, tweak_error=tweak_error)
            item.results[t.udid] = result
            state.step(p.app, STEP_INSTALL, SKIPPED, detail, target=t, waiting=result.waiting)

    staged: str | None = None
    if p.store.planned:
        state.step(p.app, STEP_GET, START, p.store.to_version)
        try:
            staged = _get(cfg, p, emitter=state.emitter, pipeline=pipeline)
        except appstore.GetStopped as e:
            raise Stopped(signal.SIGTERM) from e
        except Exception as e:  # this app stops; the next one still runs
            refused = isinstance(e, AppStoreError) and isinstance(e.__cause__, ipatool.LicenseRequired)
            if refused:
                update_refusals.remember(cfg.state_dir, cfg.get_app(p.app), p.store.to_version)
            error = DOESNT_HAVE if refused else _message(e)
            state.step(p.app, STEP_GET, END, error)
            _none_installed(p.app, needed, error, item=item, state=state)
            return False, error
        item.done.append(STEP_GET)
        update_refusals.forget(cfg.state_dir, p.app)
        state.step(p.app, STEP_GET, END, staged)
    else:
        state.step(p.app, STEP_GET, SKIPPED, p.store.skipped_reason or NOTHING_NEWER)
    if not needed:
        state.step(p.app, STEP_BUILD, SKIPPED, TWEAK_FAILED_ONLY)
        return False, tweak_error

    state.step(p.app, STEP_BUILD, START, needed[0].label)
    candidates = list(needed)
    #: Why the last device refused, if one did: the build step's end when
    #: no device is left to build for.
    refused: str | None = None
    while True:
        first, rest = candidates[0], candidates[1:]
        built = Built(on_install=lambda first=first: state.step(p.app, STEP_INSTALL, START, target=first),
                      also_for=[t.udid for t in rest])
        built.expecting(lambda first=first: _connected(cfg, p.app, first),
                        update_readiness.WAITING.format(device=first.label))
        error: str | None = None
        try:
            if not _connected(cfg, p.app, first):
                raise DeviceNotConnectedError(first.udid, label=first.label)
            pipeline.build(cfg=cfg, app_name=p.app, device=first.udid, emitter=state.emitter,
                           built=built)
        except (DeviceNotConnectedError, NoDeviceConnectedError) as e:
            if built.archive is not None or built.install_started:
                error = _message(e)
            else:
                # Unplugged since the plan, and that alone kept the build from
                # starting: that device waits, and the next one builds.
                _now_waiting(p.app, first, item=item, state=state)
                if rest:
                    candidates = rest
                    continue
                # Every one waits: as one alone would, not a failure, though
                # a tweak that couldn't be updated still is (final review 3 #2a).
                state.step(p.app, STEP_BUILD, END, refused or NONE_CONNECTED)
                return _outcome(item, failures)
        except Exception as e:  # this app's build failed; the next app still runs
            error = _message(e)
            if built.target_refused and built.archive is None and not built.install_started:
                # Refused for that device alone, before anything was built: it
                # fails, and the next one builds, its install the build's own
                # (final review 3 #3, spec §11.3).
                refused = error
                item.results[first.udid] = _not_done(error)
                state.step(p.app, STEP_INSTALL, SKIPPED, error, target=first)
                if rest:
                    candidates = rest
                    continue
                state.step(p.app, STEP_BUILD, END, error)
                return _outcome(item, failures)
        break
    if built.archive is None:
        # Nothing to install anywhere: every target is left to the next run.
        if error is None:
            error = "the build made no archive"
        if staged is not None:
            error = f"{staged} is downloaded; the build failed: {error}"
        if built.install_started:
            item.results[first.udid] = _not_done(error)
            state.step(p.app, STEP_INSTALL, END, error, target=first)
        state.step(p.app, STEP_BUILD, END, error)
        _none_installed(p.app, [t for t in candidates if t.udid not in item.results], error,
                        item=item, state=state)
        return False, error
    item.done.append(STEP_BUILD)
    # The first target's install was the build's own (stage 5).
    if not built.install_started:
        state.step(p.app, STEP_INSTALL, START, target=first)
    if built.installed:
        item.results[first.udid] = _FINE
        state.step(p.app, STEP_INSTALL, END, None, target=first)
    elif built.gone():
        # Unplugged after the build found it (during the pre-flight, cyan or
        # zsign, or the push itself), so stage 5 failed: it waits, as it would
        # have before the build (final review 3 #2b). The answer the build's
        # own `build-end` was given (`Built.waits`).
        _now_waiting(p.app, first, item=item, state=state, started=True)
    else:
        item.results[first.udid] = _not_done(error or "the install didn't finish")
        state.step(p.app, STEP_INSTALL, END, item.results[first.udid].error, target=first)
    state.step(p.app, STEP_BUILD, END, None)
    for t in rest:
        # A Stop held through the previous install takes effect here, before
        # this target starts: it and the ones after it are "not started".
        gate.release()
        if not _connected(cfg, p.app, t):
            # Asked before its install opens a build envelope of its own, whose
            # `build-end` would say it failed.
            _now_waiting(p.app, t, item=item, state=state)
            continue
        state.step(p.app, STEP_INSTALL, START, target=t)
        built.expecting(lambda t=t: _connected(cfg, p.app, t),
                        update_readiness.WAITING.format(device=t.label))
        try:
            pipeline.install(cfg=cfg, app_name=p.app, device=t.udid, built=built, emitter=state.emitter)
        except (DeviceNotConnectedError, NoDeviceConnectedError):
            # Unplugged since the plan: it waits, as the plan would have said.
            _now_waiting(p.app, t, item=item, state=state, started=True)
            continue
        except Exception as e:  # this target failed; the next one still installs
            if built.gone():
                # Unplugged during its push or install: it waits too.
                _now_waiting(p.app, t, item=item, state=state, started=True)
                continue
            item.results[t.udid] = _not_done(_message(e))
            state.step(p.app, STEP_INSTALL, END, _message(e), target=t)
            continue
        item.results[t.udid] = _FINE
        state.step(p.app, STEP_INSTALL, END, None, target=t)
    return _outcome(item, failures)


def _work(cfg: Config, state: _Run, *, gate: StopGate, pipeline: Pipeline) -> None:
    failures = state.failures = _update_tweaks(cfg, state, gate=gate, pipeline=pipeline)
    if any(item.plan.tweak_updates for item in state.items):
        # Re-pointed on disk: the builds read the apps' new tweak lists.
        cfg = load_config(cfg.path)
    for item in state.items:
        # A Stop held through the previous app's push or install takes effect
        # here, once that app's build has ended and its result is out, and
        # before this app starts, whatever the build did (review #6).
        gate.release()
        state.current = item
        ok, error = _run_app(cfg, item, failures, state=state, gate=gate, pipeline=pipeline)
        with _stops_held():
            state.app_end(item, ok=ok, stopped=False, error=error,
                          bucket=state.updated if ok else state.failed)
            state.current = None
            state.finished += 1
    # And one held through the last app's.
    gate.release()


def run(cfg: Config, names: list[str], *, all_apps: bool, with_affected: bool, probe,
        pipeline: Pipeline, emitter: JsonEmitter | None, trusted: Mapping[str, str] | None = None) -> int:
    """`--update` without `--plan`. Returns the exit status: 0 when every app
    it covered was updated, 1 otherwise. A Stop raises `Stopped`. A tweak
    update that isn't verified runs only when `trusted` names it
    (`--trust-tweak`): without that, it isn't part of the run."""
    state = _Run(emitter=emitter, say=pipeline.say if emitter is None else _quiet)
    gate = StopGate(_announcer(emitter, pipeline))
    with gate.installed():
        try:
            plans = _plan(cfg, names, quick=False, with_affected=with_affected, probe=probe,
                          pipeline=pipeline, trusted=trusted)
        except Stopped:
            with _stops_held():
                state.end(stopped=True)
            raise
        try:
            # The plan, the waiting count and the blocked apps' results go out
            # whole, and a Stop meanwhile takes effect once they're out (final
            # review 3 #4): each app still gets its result, and `update-end`
            # comes last.
            with _stops_held():
                emit_plan(plans, quick=False, emitter=emitter, say=pipeline.say)
                # `--all` leaves out an app whose only work is waiting for its
                # devices, as Update All (N) does: not a failure, and not an update.
                # A tweak update the person confirmed that has changed since
                # (re-verify #2): their yes was for something else, so nothing
                # of that app runs, and it says why.
                changed = [p for p in plans if p.trust_changed]
                for p in changed:
                    state.app_end(_Progress(p), ok=False, stopped=False, error=_trust_changed_error(p),
                                  bucket=state.failed)
                covered = [p for p in plans if not p.nothing_to_do and not p.trust_changed
                           and (not all_apps or (p.has_update and not p.waiting_only))]
                state.waiting = sum(1 for p in plans for t in p.targets if t.waiting)
                for p in covered:
                    if not p.runnable:
                        state.app_end(_Progress(p), ok=False, stopped=False,
                                      error=p.blocked_reason or p.install_skipped_reason,
                                      bucket=state.blocked)
                state.items = [_Progress(p) for p in covered if p.runnable]
            _work(cfg, state, gate=gate, pipeline=pipeline)
        except Stopped:
            state.stopped()
            raise
        return 0 if state.end(stopped=False) else 1
