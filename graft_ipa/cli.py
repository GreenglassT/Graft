"""CLI entry point and pipeline orchestration."""

from __future__ import annotations

import errno
import functools
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from types import TracebackType
from typing import Any, NoReturn, TextIO

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import (
    __version__,
    appstore,
    artwork,
    canister,
    deb,
    decrypt_runner,
    device_apps,
    itunes,
    lc_runner,
    playcover,
    repo,
    updates,
)
from . import provide_ipa as provide_ipa_module
from .build_log import BuildLog, _target_of, sha256_of
from .config import (
    DEFAULT_ARCHIVE_KEEP,
    SETTINGS,
    AppConfig,
    Config,
    add_app,
    add_repo,
    default_config_path,
    load_config,
    read_settings,
    remove_app,
    remove_repo,
    set_app_options,
    set_app_tweaks,
    set_settings,
    slugify_name,
)
from .cyan_runner import run_cyan
from .data_dirs import migrate_legacy_dirs
from .delivery import (
    DeliveredSnapshot,
    DeliveryState,
    InputsDigest,
    compute_inputs_digest,
    diff_delivery,
)
from .devices import BriefDevice, DeviceInfo, list_devices, list_devices_brief
from .errors import (
    EXIT_BROKEN_PIPE,
    ConfigError,
    GraftError,
    StagedIPAInUseError,
    TweakInUseError,
    TweakNotFoundError,
)
from .init_flow import run_init
from .installer import InstallError, install, select_device
from .ipa_cache import CachedIPA, _cache_candidates, _is_cache_candidate, acquire_ipa, find_in_cache
from .ipa_inspect import (
    IPAInfo,
    _sidecar_path,
    appex_of,
    clear_stripped,
    extension_importance,
    extension_point,
    extract_ipa,
    lost_capabilities,
    mark_stripped,
    normalize_exec_bits,
    read_info_plist,
    repack_ipa,
    stripped_extensions,
    stripped_note,
    verify_decrypted,
)
from .ipa_memo import IPAInfoMemo
from .json_output import (
    APP_OPTION_FIELDS,
    CLI_ONLY_EVENTS,
    CYAN_OPTION_FIELDS,
    EVENT_TYPES,
    GUI_EVENTS,
    STAGE_NAMES,
    JsonEmitter,
)

# Bound at module level as well as reachable through the module above: the
# reconcile path is the one device call a test can exercise without a phone, so
# it has to be patchable as `cli.list_livecontainer_documents`.
from .lc_runner import list_livecontainer_documents
from .legacy_env import legacy_env_notices
from .preflight import run_all_checks
from .retention import Archive, plan_prune
from .signer import run_zsign
from .trash import move_to_trash

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    invoke_without_command=True,
    help="End-to-end iOS tweak injection and sideload pipeline.",
    # Options must be allowed to follow the app name. `invoke_without_command`
    # makes Typer build a Click *Group*, and Click sets allow_interspersed_args
    # False on Groups so a group cannot steal its subcommands' options. This
    # app registers no subcommands, so that protection guards nothing here and
    # costs real safety: `graft example --dry-run` parsed --dry-run as a
    # SECOND APP NAME, leaving dry_run False. It performed a real build and a
    # real signed install of example, then failed on an app called "--dry-run".
    # The flag that exists to avoid touching the device was the flag the device
    # got. Same for any trailing option — `example --delivery sideload`
    # swallowed the flag and its value.
    context_settings={"allow_interspersed_args": True},
)


class _PipeAwareConsole(Console):
    """A `rich.Console` that lets a hung-up reader reach `_reported`.

    rich — 15.0.0 here, and this is read off that copy — catches
    `BrokenPipeError` inside `Console._check_buffer` and calls
    `on_broken_pipe()`, whose default body sets `quiet`, points stdout at
    /dev/null and raises `SystemExit(1)`. The remedy is right; the place is
    not. `SystemExit` is a `BaseException`, so it sails straight past
    `_reported` — which left this CLI with two different answers to one
    condition, both measured before the fix against a stdout whose reader had
    gone: `--version` exited 1 via rich, while `--describe` exited 120, because
    the JSON stream writes to `sys.stdout` through `JsonEmitter` and never
    touches rich at all.

    Re-raising instead routes both through the one error boundary, which is the
    same argument `_reported` itself makes: a second place that handles a
    broken pipe is a second place that can be forgotten or can disagree.
    Against a rich old enough to predate the hook this class is inert and costs
    nothing — `_check_buffer` would not catch the error in the first place, so
    it already propagates to the boundary on its own.
    """

    def on_broken_pipe(self) -> None:
        # rich calls this from inside `except BrokenPipeError:`, so a bare
        # `raise` re-raises that exception with the traceback that shows which
        # `print` hit the dead pipe. The guard is for a future rich that calls
        # the hook outside an except block, where a bare `raise` would turn a
        # broken pipe into `RuntimeError: No active exception to re-raise` —
        # the boundary only handles what it is actually handed.
        if not isinstance(sys.exc_info()[1], BrokenPipeError):
            raise BrokenPipeError(errno.EPIPE, os.strerror(errno.EPIPE))
        raise


console = _PipeAwareConsole()
# Errors go to stderr so subprocess consumers (the GUI) can surface them via
# `result.stderr` without parsing the JSON-events stdout. Rich auto-disables
# colour when stderr isn't a TTY (i.e. when piped).
err_console = _PipeAwareConsole(stderr=True)


def _warn(emitter: JsonEmitter | None, message: str, *, carried_by: str | None = None) -> None:
    """A warning that reaches both front-ends. The only way this module warns.

    `carried_by` names the registered event that already carries this warning to
    the GUI. The `log` event is then suppressed and only stderr is written:
    `encrypted-binaries`, `livecontainer-choice` and `device-error` each have
    their own event *and* came through here, so the panel rendered each of them
    twice — once as its own line, once as a `log` line saying the same thing.
    stderr still gets it either way, because that is the only channel a terminal
    user has and no event type reaches them.

    A name rather than a boolean because the boolean was a claim nobody could
    check, and it was already wrong: `device-error` satisfied it and did not use
    it, so an identical warning three functions away took the opposite route —
    the exact "same decision, two answers" shape this pair of channels was
    introduced to end. A name is greppable, and it is verified here against
    `GUI_EVENTS`, so renaming `livecontainer-choice`'s `emit` and leaving this
    line alone is a loud failure instead of a warning that silently becomes
    stderr-only on a path whose stderr the GUI reads only on a non-zero exit.

    `console` writes to stdout, which under --json *is* the event stream: the
    GUI decodes that stream line by line with `try?` and drops anything that
    isn't a registered event, so a warning printed there is a warning the GUI
    structurally cannot show. Every warning goes out twice instead — as a `log`
    event, which the build panel renders, and on stderr, which reaches a
    consumer that has never heard of the event stream at all. That is the same
    two-channel rule `_do_devices` follows for a device it couldn't query.

    Warnings, not failures: the caller has already decided to carry on. What
    this fixes is warnings that were invisible to the front-end the user was
    actually looking at.

    `message` is escaped for the console and left raw for the event. Almost
    every caller interpolates an exception into it, and Rich reads `[...]` in a
    printed string as markup: on rich 15 a closing-tag-shaped token raises
    MarkupError and `[foo]` is deleted silently. Raised from `_record_history`
    that aborted `__exit__` before `build-end` — a warning about bookkeeping
    taking out the one event `_BuildReport` exists to guarantee. Escaping only
    the interpolated half is what keeps the `warning:` prefix coloured; the
    `log` event carries the original text because JSON has no markup.
    """
    if carried_by is not None and carried_by not in GUI_EVENTS:
        # Checked before the suppression it authorises, and raised rather than
        # ignored for the same reason `_line` raises: the alternative is a
        # warning that reaches nobody, which is indistinguishable from no
        # warning at all. `carried_by` is a literal at every call site
        # (tests/test_cli_warning_channel.py asserts that), so this is a source
        # mistake that fires in development, not a runtime condition a user can
        # reach.
        raise ValueError(
            f"carried_by={carried_by!r} is not an event the GUI decodes: it is "
            "unregistered, or declared CLI-only in json_output.py. Suppressing "
            "the `log` event on its word would leave this warning on stderr "
            "alone — which the GUI reads only off a non-zero exit."
        )
    if emitter is not None and carried_by is None:
        emitter.emit("log", source="warning", line=message)
    err_console.print(f"[yellow]warning:[/yellow] {escape(message)}")


def _print_checks(results) -> None:
    table = Table(title="pre-flight checks", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("detail / hint")
    for r in results:
        color = "green" if r.ok else "red"
        detail = r.detail if r.ok else (r.detail + ("  " + r.hint if r.hint else ""))
        table.add_row(r.name, f"[{color}]{r.status}[/{color}]", detail)
    console.print(table)


def _do_check(cfg: Config, emitter: JsonEmitter | None = None) -> bool:
    """Run pre-flight checks. Returns True if all checks passed.

    When an emitter is provided, emits structured JSON events instead of Rich output.
    """
    results = run_all_checks(cfg)
    if emitter:
        for r in results:
            payload = {"name": r.name, "ok": r.ok, "detail": r.detail or None}
            if not r.ok and r.hint:
                payload["hint"] = r.hint
            if r.action_key:
                payload["action_key"] = r.action_key
            emitter.emit("preflight-check", **payload)
        ok_count = sum(1 for r in results if r.ok)
        emitter.emit("preflight-end", ok_count=ok_count, fail_count=len(results) - ok_count)
    else:
        _print_checks(results)
    return all(r.ok for r in results)


def _print_tweaks(records: list[dict]) -> None:
    table = Table(title="tweaks")
    table.add_column("filename", style="bold")
    table.add_column("size")
    table.add_column("used by")
    for r in records:
        size_kb = r["size_bytes"] / 1024
        used = ", ".join(r["used_by"]) or "[dim]orphan[/dim]"
        table.add_row(r["filename"], f"{size_kb:,.1f} KB", used)
    console.print(table)


def _tweak_consumers(cfg: Config) -> dict[tuple[int, int] | str, list[str]]:
    """Map a tweak identity (cfg.tweak_key) -> the app names listing it.

    Keys on the identity rather than the raw config string or even the
    resolved path, so a path-form entry like /elsewhere/some-tweak.deb, a mis-cased
    entry like SOME-TWEAK.deb on a case-insensitive volume, and a symlink all match
    the one real file — and none of them make tweaks_dir/some-tweak.deb look
    orphaned. Every other tweak lookup in this module keys the same way, so
    the in-use guard, the --tweaks listing and detach can't disagree.
    """
    consumers: dict[tuple[int, int] | str, list[str]] = {}
    for name, app in cfg.apps.items():
        for entry in app.tweaks:
            consumers.setdefault(cfg.tweak_key(entry), []).append(name)
    return consumers


def _lists_tweak(cfg: Config, app: AppConfig, entry: str) -> bool:
    """Whether `app` already lists this tweak, by `cfg.tweak_key` identity.

    The membership half of the rule `_tweak_consumers` and `_do_detach_tweak`
    apply to the same data; keeping all three on one identity function is what
    stops attach from adding a duplicate that detach then removes twice.
    """
    key = cfg.tweak_key(entry)
    return any(cfg.tweak_key(t) == key for t in app.tweaks)


def _transport_label(transport: str) -> str:
    """How a transport reads in a sentence. `network` is Wi-Fi in practice."""
    return "Wi-Fi" if transport == "network" else "USB"


def _collect_tweak_records(cfg: Config) -> list[dict]:
    records: list[dict] = []
    if not cfg.tweaks_dir.exists():
        return records
    consumers = _tweak_consumers(cfg)
    for entry in sorted(cfg.tweaks_dir.iterdir()):
        if not entry.is_file():
            continue
        if entry.name.startswith("."):
            continue
        record = {
            "filename": entry.name,
            "size_bytes": entry.stat().st_size,
            "used_by": sorted(consumers.get(cfg.tweak_key(entry.name), [])),
        }
        # Name/version/section/author, read out of the .deb itself. Merged in
        # rather than assigned so a file that yields nothing — a truncated
        # download, something that isn't a .deb at all — still gets listed with
        # exactly the three keys this event has always carried. `control_fields`
        # is best-effort by contract and never raises: a library listing must
        # not be lost to one bad file in the directory.
        record.update(deb.control_fields(entry))
        records.append(record)
    return records


def _print_builds(records: list[dict]) -> None:
    table = Table(title="recent builds")
    table.add_column("when", style="dim")
    table.add_column("app", style="bold")
    table.add_column("ver")
    # Shown for the same reason the GUI is about to branch on it, and named the
    # same as the JSON field so the two readings agree. A terminal user picks
    # rows by eye for `--reinstall <id>`, which pushes to a phone; a Mac build
    # sent there points its tweak dylib at the bundled Preferences stub and
    # breaks silently. `mac` is called out and `device` left dim, because the
    # row worth noticing is the rare one.
    table.add_column("target")
    table.add_column("tweaks", overflow="fold", max_width=40)
    table.add_column("status")
    for r in records:
        status = "[green]✓[/green]" if r["success"] else f"[red]✗[/red] {r.get('error') or ''}"
        tweaks_summary = ", ".join(r["tweaks"][:2])
        if len(r["tweaks"]) > 2:
            tweaks_summary += f" +{len(r['tweaks']) - 2}"
        target = "[magenta]mac[/magenta]" if r["target"] == "mac" else "[dim]device[/dim]"
        table.add_row(
            r["timestamp"], r["app"], r["version"], target,
            tweaks_summary or "[dim]none[/dim]", status,
        )
    console.print(table)


def _do_builds(cfg: Config, app: str | None, limit: int, emitter: JsonEmitter | None = None) -> None:
    log_path = cfg.state_dir / "log.sqlite"
    if not log_path.exists():
        if emitter:
            return
        console.print("[dim]no builds yet[/dim]")
        return
    log = BuildLog(log_path)
    rows = log.recent(app=app, limit=limit)
    records = [
        {
            "id": r["id"],
            "timestamp": r["timestamp"],
            "app": r["app"],
            "bundle_id": r["bundle_id"],
            "version": r["version"],
            "tweaks": (r["tweaks"] or "").split(",") if r["tweaks"] else [],
            "ipa_path": r["ipa_path"],
            "ipa_sha256": r["ipa_sha256"],
            "log_path": r["log_path"],
            "signing_team": r["signing_team"],
            "udid": r["udid"],
            "target": r["target"],
            "success": bool(r["success"]),
            "error": r["error"],
        }
        for r in rows
    ]
    if emitter:
        for r in records:
            emitter.emit("build-record", **r)
        return
    _print_builds(records)


def _do_reveal_build(cfg: Config, build_id: int) -> None:
    import sqlite3 as _sqlite3
    log_path = cfg.state_dir / "log.sqlite"
    if not log_path.exists():
        raise GraftError("no build log yet")
    with _sqlite3.connect(log_path) as conn:
        conn.row_factory = _sqlite3.Row
        row = conn.execute("SELECT ipa_path FROM builds WHERE id = ?", (build_id,)).fetchone()
    if row is None:
        raise GraftError(f"no build with id {build_id}")
    ipa_path = row["ipa_path"]
    if not ipa_path:
        raise GraftError(f"build {build_id} has no archived IPA path")
    if not Path(ipa_path).exists():
        raise GraftError(f"archived IPA missing: {ipa_path}")
    subprocess.run(["open", "-R", ipa_path], check=False)


def _do_build_log(cfg: Config, build_id: int) -> None:
    """Stream the saved per-build JSON-Lines log to stdout."""
    import sqlite3 as _sqlite3
    db_path = cfg.state_dir / "log.sqlite"
    if not db_path.exists():
        raise GraftError("no build log yet")
    with _sqlite3.connect(db_path) as conn:
        conn.row_factory = _sqlite3.Row
        row = conn.execute("SELECT log_path FROM builds WHERE id = ?", (build_id,)).fetchone()
    if row is None:
        raise GraftError(f"no build with id {build_id}")
    log_path = row["log_path"]
    if not log_path:
        raise GraftError(
            f"build {build_id} has no captured log",
            hint="logs are only captured for builds run after the log-persistence feature shipped",
        )
    if not Path(log_path).exists():
        raise GraftError(f"saved log missing: {log_path}")
    # Stream the file straight through — it's already valid JSON-Lines.
    with open(log_path) as f:
        for line in f:
            sys.stdout.write(line)
    sys.stdout.flush()


def _record_build_row(
    log: BuildLog, *, app: str, bundle_id: str, version: str, tweaks: list[str],
    ipa_path: Path | None, log_path: Path | None, signing_team: str | None,
    udid: str | None, success: bool, error: str | None, for_playcover: bool,
) -> None:
    """Write the build row, translating the flag into the recorded target.

    Extracted so the translation is reachable by a test: inside
    `_BuildReport._record_history`, the one line that decides whether the GUI
    will offer this artifact to a phone can only be reached by running a whole
    build, and a mutation of it was undetectable.
    """
    log.record(
        app=app, bundle_id=bundle_id, version=version, tweaks=tweaks,
        ipa_path=ipa_path, log_path=log_path, signing_team=signing_team,
        udid=udid, success=success, error=error,
        target="mac" if for_playcover else "device",
    )


class _BuildReport:
    """The one place a build's terminal records are written.

    Both build paths — `_build_one` and `_do_reinstall_build` — run inside one
    of these and neither emits `build-start`/`build-end` itself. That is what
    makes the GUI's guarantee structural instead of a rule every new guard has
    to remember:

    * `build-start` goes out in `__enter__`, so nothing can run ahead of it.
      `_do_reinstall_build` used to check `select_device`, the archived IPA and
      the app's config entry first, all of which raise — the Re-install sheet
      showed "Failed — exit 8" over an empty log panel. `_build_one` opened its
      tee log first, so a full or read-only state dir produced no JSON event at
      all: the process died with a traceback the GUI could not attribute.
    * `build-end` goes out in `__exit__`, exactly once, however the block was
      left. `_build_one`'s deliberately widened `except Exception` was the only
      thing standing behind that, and anything hoisted above the `try` for
      convenience stepped quietly out from behind it.
    * The bookkeeping that used to run *after* the terminal event — the history
      row — now runs inside `__exit__` before it, and can only warn. A locked
      log.sqlite (the file a concurrent `--reconcile` writes) once emitted
      `build-end{ok:true}`, threw on the way past, and was answered with a
      second `build-end{ok:false}`; the GUI takes the last event it sees, so an
      app installed and running on the phone reported "✗ database is locked".
      One emit, last, cannot say that.

    The build fills in `log`/`tweaks`/`version`/`ipa_path`/`signing_team`/`udid`
    as it learns them and `__exit__` reads them off, so the history row needs no
    second call site that could be reached on one path and skipped on another.
    `log` and `tweaks` are set from inside the block rather than passed in
    because opening log.sqlite and resolving the app both raise, and anything
    that raises has to happen after `build-start`.
    """

    def __init__(
        self,
        emitter: JsonEmitter | None,
        *,
        app_name: str,
        bundle_id: str,
        delivery: str | None,
        dry_run: bool = False,
        for_playcover: bool = False,
    ) -> None:
        self._emitter = emitter
        self._app_name = app_name
        self._bundle_id = bundle_id
        self._delivery = delivery
        self._dry_run = dry_run
        # Passed in rather than filled in from inside the block, like `delivery`
        # and `dry_run` and unlike `version`/`ipa_path`: it comes straight from
        # argv, so it is known before `build-start` and nothing about reading it
        # can raise.
        self._for_playcover = for_playcover
        self._started = 0.0
        self._entered = False
        self._tee: TextIO | None = None
        # Stays `None` for a re-install: it re-ships an archive that already has
        # a row, and a second row would claim a build that never ran. It also
        # stays None for a build whose log.sqlite wouldn't open — that failure
        # is the build's, reported by `build-end`, not a reason to lose it.
        self.log: BuildLog | None = None
        self.tweaks: list[str] = []
        self.version: str | None = None
        self.ipa_path: Path | None = None
        self.signing_team: str | None = None
        self.udid: str | None = None
        self.log_path: Path | None = None

    @property
    def elapsed(self) -> float:
        return time.time() - self._started

    @property
    def _start_fields(self) -> dict[str, Any]:
        return {
            "app": self._app_name,
            "bundle_id": self._bundle_id,
            "delivery": self._delivery,
        }

    def __enter__(self) -> _BuildReport:
        self._started = time.time()
        self._entered = True
        if self._emitter:
            self._emitter.emit("build-start", **self._start_fields)
        return self

    def open_tee(self, log_dir: Path) -> None:
        """Tee every event to a per-build .jsonl so the GUI can replay it later.

        Deliberately carries no exception handling of its own. A state dir that
        is full or read-only has to be reported, and the only report a JSON
        consumer can act on is a `build-end` — which is available here precisely
        because this can only be called from inside the envelope.
        """
        if not self._entered:
            raise RuntimeError("open_tee() outside the build envelope")
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            path = log_dir / f"{uuid.uuid4()}.jsonl"
            handle = path.open("w")
        except OSError as exc:
            raise GraftError(
                f"couldn't open a build log under {log_dir}: {exc}",
                hint="check that the state directory is writable and has free space",
            ) from exc
        self.log_path = path
        self._tee = handle
        if self._emitter:
            self._emitter.tee = handle
            # The build already started — that is the price of being able to
            # report a state dir that won't open — so replay the one event the
            # file would otherwise be missing. A saved log the GUI replays from
            # `build-start` onwards must not begin mid-build.
            self._emitter.emit_to_tee("build-start", **self._start_fields)

    def _record_history(self, error: str | None) -> None:
        """Write the build's history row. Warns rather than raises, always.

        A build that shipped must not be reported as failed because the history
        database was locked, and a build that failed must not lose its reason
        to a second exception thrown on the way out.
        """
        if self.log is None:
            return
        ok = error is None
        # A dry run built nothing worth a success row; a dry run that *failed*
        # still owes the user a reason in `--builds`.
        if ok and self._dry_run:
            return
        try:
            _record_build_row(
                self.log,
                app=self._app_name,
                bundle_id=self._bundle_id,
                version=self.version or "unknown",
                tweaks=self.tweaks,
                # Only a successful build has an artifact, a signing team or a
                # device to name; a failed one leaves those NULL as before.
                ipa_path=self.ipa_path if ok else None,
                signing_team=self.signing_team if ok else None,
                udid=self.udid if ok else None,
                success=ok,
                error=error,
                log_path=self.log_path,
                for_playcover=self._for_playcover,
            )
        except Exception as exc:
            _warn(self._emitter, f"couldn't record this build in the history log: {exc}")

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool:
        elapsed = self.elapsed
        error = None if exc is None else (str(exc) or exc.__class__.__name__)
        # Three steps, each inside the next one's `finally`, because each of the
        # first two can throw and none of them may cost the ones after it:
        #
        # * `build-end` must go out even if the history write throws past its
        #   own guard — that terminal event is the whole reason this class
        #   exists, and the only thing left that can throw inside
        #   `_record_history` is the warning it uses to report failing.
        # * the tee must be detached even if `build-end` throws. `emit` writes
        #   stdout first and the tee second, so a state volume that fills
        #   mid-build lets `build-end{ok:true}` reach the GUI and *then* raises
        #   on `tee.write`. With the close skipped, `emitter.tee` still pointed
        #   at the finished build's file: in a multi-app `--json` run the next
        #   app appended its `build-start` there, and replaying app 1's saved
        #   log showed a transcript that changed apps halfway through.
        try:
            self._record_history(error)
        finally:
            try:
                self._emit_end(error, elapsed)
            finally:
                self._detach_tee()
        return False

    def _emit_end(self, error: str | None, elapsed: float) -> None:
        """The terminal event. Last, and exactly once, however the block was left."""
        if not self._emitter:
            return
        fields: dict[str, Any] = {
            "app": self._app_name,
            "ok": error is None,
            "elapsed_seconds": elapsed,
        }
        if error is not None:
            fields["error"] = error
        # Emitted before the tee is detached, so the replayable log ends the way
        # the live stream did rather than stopping one event short.
        self._emitter.emit("build-end", **fields)

    def _detach_tee(self) -> None:
        """Unhook this build's log file and close it. Cannot raise.

        Unhooking first and unconditionally is the point: a `close()` that
        throws must still leave the emitter without a handle, or one build's
        broken log file silently swallows the next build's events.

        The close error itself is dropped rather than warned about. Every write
        to the tee is flushed at the moment it is made, so there is no buffered
        data for `close()` to lose — a failure here is about the descriptor, not
        the log's contents. Warning would also mean emitting a `log` event after
        `build-end`, i.e. buying a diagnostic nobody needs by breaking the
        "terminal event last" rule this class is here to keep.
        """
        handle, self._tee = self._tee, None
        if handle is None:
            return
        if self._emitter is not None and self._emitter.tee is handle:
            self._emitter.tee = None
        try:
            handle.close()
        except OSError:
            pass


def _lc_target(app_cfg: AppConfig) -> str:
    """The LiveContainer an app's IPA must be pushed into.

    One rule, both push sites. `_do_reinstall_build` used to omit it and take
    `push_ipa_to_livecontainer`'s default, so with two LiveContainers installed
    and `lc_bundle_id:` pinned in config.yaml, Re-install pushed into the wrong
    container and reported success — or, when the pinned id was a suffixed one
    and bare `com.kdt.livecontainer` was not installed, failed by telling the
    user to set the pin they had already set.
    """
    return app_cfg.lc_bundle_id or lc_runner.LC_BUNDLE_ID


def _report_lc_choice(
    emitter: JsonEmitter | None,
    *,
    app_name: str,
    result: lc_runner.LCDeliveryResult,
) -> None:
    """Say which LiveContainer got the IPA when more than one is installed.

    Silent when there is only one — there is no choice to report, and a `log`
    line on every push would train the user to skip them.

    This existed as a `console.print`, on stdout, which under --json *is* the
    event stream: the GUI's decoder drops the line and shows nothing. The count
    rode along in the `stage` event's extra fields, which `BuildProgress` drops
    too. So the user opened whichever LiveContainer they had open, found no new
    IPA in its Documents tab, and concluded the tweak hadn't built — the multi-LC
    gotcha, reported nowhere a machine consumer could see it.

    Two channels for the same reason `encrypted-binaries` has two: a first-class
    event carrying the structured answer (chosen container, the others), and a
    `_warn` so the same fact reaches stderr and any consumer reading the build
    stream without a case for the new type.

    Both push sites call this. The re-install path never reported `others` at
    all, and it is the path most likely to hit the ambiguity — it re-ships an
    old artifact to whatever is installed now.
    """
    if not result.others:
        return
    if emitter:
        emitter.emit(
            "livecontainer-choice",
            app=app_name,
            bundle_id=result.bundle_id,
            others=list(result.others),
        )
    _warn(
        emitter,
        carried_by="livecontainer-choice",
        message=f"{len(result.others)} other LiveContainer(s) installed "
        f"({', '.join(result.others)}) — import {app_name} from "
        f"{result.bundle_id}, not those, or pin one with `lc_bundle_id:` "
        "in config.yaml",
    )


def _do_reinstall_build(cfg: Config, build_id: int, emitter: JsonEmitter | None = None) -> None:
    """Re-ship the archived IPA for a previous build without re-running cyan/zsign."""
    import sqlite3 as _sqlite3

    log_path = cfg.state_dir / "log.sqlite"
    if not log_path.exists():
        raise GraftError("no build log yet")
    with _sqlite3.connect(log_path) as conn:
        conn.row_factory = _sqlite3.Row
        row = conn.execute(
            "SELECT app, bundle_id, version, ipa_path, udid, success, target "
            "FROM builds WHERE id = ?",
            (build_id,),
        ).fetchone()
    if row is None:
        # The one guard that stays outside the envelope: with no row there is
        # no app to name, and a `build-start` for nothing is worse than none.
        # It reaches the GUI as `_fatal`'s stderr line and a non-zero exit.
        raise GraftError(f"no build with id {build_id}")

    app_name = row["app"]
    # Looked up, not resolved: the *guard* belongs inside the envelope with the
    # rest of them, but `build-start` carries the delivery mode and this is the
    # only place it can come from. An app that is gone from config.yaml reports
    # a null delivery, which is the truth about what we know.
    app_cfg = cfg.apps.get(app_name)

    with _BuildReport(
        emitter,
        app_name=app_name,
        bundle_id=row["bundle_id"],
        delivery=app_cfg.delivery if app_cfg else None,
    ):
        # Every pre-push guard runs here rather than above, so a re-install that
        # can't start still ends: these all raise, and outside the envelope they
        # produced no build-start and no build-end at all — the Re-install sheet
        # showed "Failed — exit 8" over an empty panel.
        if app_cfg is None:
            raise GraftError(
                f"app '{app_name}' is no longer in config.yaml — can't determine delivery mode",
            )
        # The same refusal `_build_one` makes: an archived sideload build of
        # LiveContainer was signed by zsign, and re-shipping it brings back the
        # keychain defect without a rebuild ever running.
        from .lc_manage import refuse_legacy_build

        refuse_legacy_build(app_cfg)
        if not row["success"]:
            raise GraftError(
                f"build {build_id} did not produce a signed IPA — nothing to re-install",
                hint="pick a row with a green status",
            )
        ipa_path = row["ipa_path"]
        if not ipa_path or not Path(ipa_path).exists():
            raise GraftError(
                f"archived IPA missing: {ipa_path or '(not recorded)'}",
                hint="re-run the original build to regenerate the archive",
            )

        # A Mac build must not reach a phone from here. `--for-playcover`
        # retargets the tweak's dylib at a bundled *stub* Preferences.framework
        # because macOS has no such private framework; iOS has the real one, so
        # this artifact on a phone points the tweak at the stub and breaks it
        # silently, in a settings pane nobody opens until much later.
        #
        # `main` already refuses to install a --for-playcover build on the run
        # that produces it. This is the same refusal one door over: the archive
        # outlives that run, and `--reinstall-build <id>` takes an id a person
        # picked by eye off the `--builds` table. Until Mac builds existed no
        # row could be `mac` and this door was safe by absence.
        #
        # Read through `_target_of` because that is where the closed set lives,
        # not because it behaves differently here: measured, swapping it for
        # `row["target"] == "mac"` leaves the whole reinstall suite green, NULL
        # and `"Mac"` rows included. The two agree today by construction — only
        # the exact string reads as Mac, everything else as the safe direction —
        # and the point of the call is that they go on agreeing. If a third
        # target is ever added, `build_log` is the one file that has to know.
        #
        # After the archive check, so the hint can name the file the user is
        # meant to hand over. A mac row whose archive is gone gets the missing
        # -archive error instead, which is also a refusal — neither path pushes.
        if _target_of(row) == "mac":
            raise GraftError(
                f"build {build_id} was built for this Mac, not for a phone",
                hint=f"install it here with `--install-to-mac {Path(ipa_path).name}`. "
                "Its tweak dylib points at a bundled stub Preferences.framework, "
                "which iOS has for real — on a phone the tweak would break silently",
            )

        udid = row["udid"] or cfg.default_device_udid
        udid, transport = select_device(udid)

        # What reconcile will need to find this artifact again. Set inside the
        # livecontainer branch only; a sideload leaves both None, which is what
        # tells reconcile there is no LiveContainer to look inside.
        lc_bundle_id: str | None = None
        remote_name: str | None = None

        if app_cfg.delivery == "livecontainer":
            console.print(f"→ re-installing [cyan]{app_name}[/cyan] {row['version']} into LiveContainer")
            if emitter:
                emitter.emit("stage", name="lc-push", status="start")
            lc_result = lc_runner.push_ipa_to_livecontainer(
                Path(ipa_path),
                udid=udid,
                inner_filename=f"{app_name}.ipa",
                # The app's pin, by the same rule the build path uses: without
                # it a re-install lands in whichever LiveContainer is the
                # default, which is not the one the build went to.
                lc_bundle_id=_lc_target(app_cfg),
                dry_run=False,
            )
            if emitter:
                emitter.emit("stage", name="lc-push", status="end",
                             bundle_id=lc_result.bundle_id, size_bytes=lc_result.size_bytes)
            console.print(
                f"   [green]✓[/green] {lc_result.size_bytes/1_000_000:.1f} MB into "
                f"{lc_result.bundle_id} on {udid}"
            )
            _report_lc_choice(emitter, app_name=app_name, result=lc_result)
            lc_bundle_id = lc_result.bundle_id
            # The name lc_runner reports having written, not a second guess at
            # how it was built — the same rule the build path's push follows.
            remote_name = Path(lc_result.documents_path).name
        else:
            console.print(f"→ re-installing [cyan]{app_name}[/cyan] {row['version']} via ideviceinstaller")
            if emitter:
                emitter.emit("stage", name="install", status="start")
            ir = install(Path(ipa_path), udid=udid, transport=transport, dry_run=False)
            if emitter:
                emitter.emit("stage", name="install", status="end")
            _emit_log_lines(emitter, "install", ir.stdout)
            _emit_log_lines(emitter, "install", ir.stderr)
            console.print(f"   [green]✓[/green] installed on {udid}")

        # After the push returned, which is the only point at which this
        # artifact is known to have reached the phone — the same placement rule
        # the build path's two push sites follow.
        _record_reinstall_delivery(
            cfg,
            app_name=app_name,
            bundle_id=row["bundle_id"],
            version=row["version"],
            ipa_path=Path(ipa_path),
            delivery=app_cfg.delivery,
            lc_bundle_id=lc_bundle_id,
            remote_name=remote_name,
            udid=udid,
            emitter=emitter,
        )


def _record_reinstall_delivery(
    cfg: Config,
    *,
    app_name: str,
    bundle_id: str,
    version: str,
    ipa_path: Path,
    delivery: str,
    lc_bundle_id: str | None,
    remote_name: str | None,
    udid: str | None,
    emitter: JsonEmitter | None = None,
) -> None:
    """Record that an *archived* build was re-shipped to the device.

    This is the third real push — `_build_one` has the other two — and leaving
    it out let the newest build's row go on describing a phone that now holds
    an older artifact: `current` reported about something that is not there,
    which is the exact failure this feature exists to prevent.

    It cannot go through `_record_delivery_if_pushed`. That records an
    `InputsDigest`, and `compute_inputs_digest` hashes the tweak files as they
    are now, not the ones inside this archive — a digest taken here would
    describe a build that was never pushed, and describe it confidently. So the
    row carries no digest and says why in one word: `origin="reinstall"`, which
    `_delivery_state_for` reads as stale-for-an-unnameable-reason.

    Never raises: the artifact already reached the phone, and losing the row
    costs a spurious badge where failing the command would cost the user a
    working install.
    """
    try:
        BuildLog(cfg.state_dir / "log.sqlite").record_delivery(
            app=app_name,
            bundle_id=bundle_id,
            inputs_digest=None,
            digest_version=None,
            cyan=None,
            # Not the build row's tweak names: those carry no hashes, and a
            # name paired with a null hash is this table's spelling for
            # "delivered, unhashable" — an invitation to a comparison the
            # archive cannot support. Empty is the honest no-claim.
            tweaks=[],
            # The source app version is the one thing about the archive that
            # was recorded at the time, so it is not a guess.
            source_version=version,
            ipa_sha256=sha256_of(ipa_path) if ipa_path.exists() else None,
            delivery=delivery,
            lc_bundle_id=lc_bundle_id,
            remote_name=remote_name,
            udid=udid,
            origin="reinstall",
        )
    except Exception as exc:
        _warn(emitter, f"couldn't record delivery: {exc}")


def _human_bytes(n: int) -> str:
    """`128.3 MB` — 1000-based, matching the GUI and Apple's own units.

    **Not 1024.** Everything the Swift side prints goes through
    `ByteCountFormatter` with `countStyle: .file`, which is 1000-based because
    that is how Apple quotes storage — a 128 GB phone reads "128 GB", not
    "119.2 GB". This half of the same tool was dividing by 1024 twice, so one
    file had two sizes depending on which half you asked: a dumped IPA of
    25,126,576 bytes printed as **24.0 MB** here and **25.1 MB** in the Add
    sheet, three lines apart in the same workflow. Measured 2026-08-18.
    """
    mb = n / 1_000_000
    return f"{mb / 1000:.1f} GB" if mb >= 1000 else f"{mb:.1f} MB"


def _prune_archives(
    cfg: Config,
    *,
    keep: int,
    dry_run: bool,
    exclude: Iterable[Path] = (),
    emitter: JsonEmitter | None = None,
    trash_dir: Path | None = None,
) -> tuple[list[Archive], list[tuple[Archive, str]]]:
    """Trash all but the newest `keep` archives per (app, version) in `built_dir`.

    Returns `(removed, failures)`. Shared by the `--prune-builds` verb and the
    automatic sweep after a successful build so the two can't drift; the only
    difference between them is that the build passes its own fresh archive in
    `exclude`.

    Archives go to the Trash, not `unlink()`. They are the user's rollback
    artefacts — the thing `--reinstall-build` re-ships — so a wrong retention
    setting should cost a trip to the Trash, not the build. The corollary is
    that pruning frees no disk space until the Trash is emptied, which the
    output says out loud.

    Per-file failures are collected, not raised: one unreadable archive must
    not strand the rest, and this runs inside a successful build where an
    exception would be recorded as a build failure. `trash_dir` is injectable
    for the same reason as `_do_delete_tweak`'s — so tests never touch the real
    `~/.Trash`.
    """
    import sqlite3 as _sqlite3

    try:
        plan = plan_prune(cfg.built_dir, keep=keep, exclude=exclude)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    # Deliberately not `BuildLog(db_path)` unconditionally: its constructor
    # creates the file, and pruning before the first build shouldn't leave an
    # empty history database behind. No log means no ipa_path to clear.
    db_path = cfg.state_dir / "log.sqlite"
    log = BuildLog(db_path) if db_path.exists() else None

    if not emitter and plan.remove:
        console.print(
            f"→ retention: {'would move' if dry_run else 'moving'} "
            f"{len(plan.remove)} archive(s) to the Trash "
            f"(keeping the newest {keep} per app version)"
        )

    removed: list[Archive] = []
    failures: list[tuple[Archive, str]] = []
    for archive in plan.remove:
        build_ids: list[int] = []
        trash_path: Path | None = None
        if not dry_run:
            # Clear the build-log pointer BEFORE the file moves. The GUI shows
            # "Re-install" for exactly the rows with a non-null ipa_path, so a
            # row left pointing at a trashed archive is a button that can only
            # fail. Doing it in this order means the one reachable inconsistency
            # is the harmless direction: a cleared row whose file is still on
            # disk, which costs a shortcut rather than showing a broken one.
            if log is not None:
                try:
                    build_ids = log.clear_ipa_path(archive.path)
                except _sqlite3.Error as exc:
                    failures.append((archive, f"build log update failed: {exc}"))
                    continue
            try:
                trash_path = move_to_trash(archive.path, trash_dir=trash_dir)
            except OSError as exc:
                failures.append((archive, str(exc)))
                continue
        removed.append(archive)
        if emitter:
            emitter.emit(
                "build-pruned",
                app=archive.app,
                filename=archive.path.name,
                path=str(archive.path),
                size_bytes=archive.size_bytes,
                trash_path=str(trash_path) if trash_path else None,
                build_ids=build_ids,
                dry_run=dry_run,
            )
        else:
            console.print(
                f"   [dim]{archive.app}[/dim]  {archive.path.name}  "
                f"({_human_bytes(archive.size_bytes)})"
            )

    bytes_removed = sum(a.size_bytes for a in removed)
    if emitter:
        # `prune-end` carries only a count; the detail goes out as `log` lines
        # so a GUI that shows the build log can say *which* archive resisted
        # and why, the same way cyan/zsign/install output is surfaced.
        for archive, detail in failures:
            emitter.emit(
                "log", source="retention", line=f"{archive.path.name}: {detail}"
            )
        emitter.emit(
            "prune-end",
            keep=keep,
            dry_run=dry_run,
            removed=len(removed),
            kept=len(plan.kept),
            bytes_removed=bytes_removed,
            unparsed=len(plan.unparsed),
            failed=len(failures),
        )
    else:
        for archive, detail in failures:
            # `detail` is `str(OSError)`, i.e. arbitrary text in a markup
            # string — the same hazard `_fatal` carries, one level quieter.
            err_console.print(f"   [red]✗[/red] {escape(archive.path.name)}: {escape(detail)}")
        if removed and dry_run:
            console.print(
                f"[green]✓[/green] would move {len(removed)} archive(s) to the Trash "
                f"— {_human_bytes(bytes_removed)}  [dim](dry run: nothing was moved)[/dim]"
            )
        elif removed:
            console.print(
                f"[green]✓[/green] moved {len(removed)} archive(s) to the Trash "
                f"— {_human_bytes(bytes_removed)}"
            )
            # Deliberately not phrased as a "note:" — that prefix, and
            # "warning:", belong to `_warn` alone, and
            # tests/test_cli_warning_channel.py enforces it. This line is not a
            # warning that has to reach both front-ends: it is human-mode
            # trailer text, unreachable under --json (the branch above emits
            # `prune-end` instead), so routing it through `_warn` would put a
            # `log` event on the stream for something already reported
            # structurally.
            console.print(
                "[dim]that space isn't reclaimed until you empty the Trash — "
                "until then the archives still occupy the disk.[/dim]"
            )
        else:
            console.print(
                f"[dim]retention: nothing to prune "
                f"({len(plan.kept)} archive(s), keeping {keep} per app version)[/dim]"
            )
        if plan.unparsed:
            console.print(
                f"[dim]   {len(plan.unparsed)} file(s) in {cfg.built_dir} don't match "
                f"<app>_<version>_<timestamp>.ipa — left alone[/dim]"
            )
    return removed, failures


def _do_prune_builds(
    cfg: Config,
    *,
    keep: int | None = None,
    dry_run: bool = False,
    emitter: JsonEmitter | None = None,
    trash_dir: Path | None = None,
) -> None:
    """`--prune-builds`: apply the retention policy on demand.

    `keep=None` means "whatever the config says", so `--prune-builds` with no
    `--keep` matches exactly what the automatic sweep would have done.
    """
    effective = cfg.archive_keep if keep is None else keep
    if effective < 0:
        raise ConfigError(
            f"--keep must be a non-negative integer, got {effective}",
            hint="use --keep 0 to remove every archive",
        )
    _removed, failures = _prune_archives(
        cfg, keep=effective, dry_run=dry_run, emitter=emitter, trash_dir=trash_dir
    )
    if failures:
        # The work that succeeded stands; this only makes the shell exit code
        # tell the truth about the archives that couldn't be moved.
        raise GraftError(
            f"{len(failures)} archive(s) could not be moved to the Trash",
            hint=f"check permissions on {cfg.built_dir} and ~/.Trash",
        )


def _print_devices(devices: list[DeviceInfo]) -> None:
    if not devices:
        console.print("[dim]no devices connected[/dim]")
        return
    table = Table(title="connected devices")
    table.add_column("name", style="bold")
    table.add_column("model")
    table.add_column("iOS")
    table.add_column("conn", style="dim")
    table.add_column("UDID", style="dim")
    table.add_column("LiveContainer")
    for d in devices:
        lc = d.livecontainer_bundle_id or "[dim]not installed[/dim]"
        table.add_row(d.name, d.model, d.ios_version, d.connection_type, d.udid, lc)
    console.print(table)


def _do_devices_brief(emitter: JsonEmitter | None = None) -> None:
    """`--devices --brief`: which UDIDs are on the bus, nothing more.

    Split out of `_do_devices` rather than living behind a `brief` flag inside
    it. The two share no lines — different source function, different loop body,
    different console format, different event name — and the tell is that `cfg`,
    `_do_devices`' only positional parameter, was unused in the whole branch.
    One event vocabulary per body.

    It takes no `cfg` because nothing on this path ever consulted one, and a
    parameter accepted only to be ignored invites the next reader to hunt for
    the rule that reads it. `_do_devices` keeps its `cfg` — it is the shape
    every other `_do_*` verb here has, and this is new surface rather than a
    signature anything already calls.
    """
    # No `device-error` arm: `--brief` never describes a device, so the
    # failure this verb's two-channel contract exists for cannot occur.
    # `failed: 0` is structural here, not observed.
    devices: list[BriefDevice] = list_devices_brief()
    if emitter:
        for d in devices:
            # `device-brief`, not `device`. A full listing's `device` event
            # carries six fields and the GUI's `DeviceRecord` makes four of them
            # non-optional (`name`, `model`, `ios_version`, `connection_type`),
            # so this two-field payload under that name decodes to nil and
            # `CLIRunner`'s `try?` drops it without a word — verified by
            # compiling the record standalone against the exact line emitted
            # here.
            #
            # The tempting fix — keep one name and discriminate on whether
            # `name` is present — is worse than the bug. It makes "this is a
            # brief event" and "this is a *malformed full* event" the same
            # signal: today a full event that lost its `name` throws, is
            # dropped, and the phone visibly vanishes from the list; under a
            # presence check that same corrupt event would decode cleanly as a
            # brief record and land in the poll's UDID set. Silent
            # misclassification beats a silent drop only if you never have to
            # debug it.
            #
            # Nothing is bought by sharing the name, either: no invocation ever
            # emits both shapes (`--devices --brief` emits only this one,
            # `--devices` only the full one), so no consumer ever wants "a
            # device event, either kind". A shared name would buy nothing and
            # cost a fragile decode arm forever.
            emitter.emit("device-brief", udid=d.udid, connection_type=d.connection_type)
        # `devices-end` *is* shared, and that is the contrast worth noticing:
        # it genuinely has the same shape on both paths (a count and a failure
        # count, both meaning the same thing), so one name here costs no decode
        # ambiguity at all. The rule is about shape, not about tidiness.
        emitter.emit("devices-end", count=len(devices), failed=0)
        return
    if not devices:
        # The console arm is the one a person reads, and it printed nothing at
        # all for the commonest case worth reporting. `_print_devices` says this
        # for the full listing; saying nothing here reads as a hung command.
        console.print("[dim]no devices connected[/dim]")
        return
    for d in devices:
        console.print(f"{d.udid}  {d.connection_type}")


def _do_devices(cfg: Config, *, emitter: JsonEmitter | None = None) -> None:
    listing = list_devices()
    # A device usbmux saw and we then failed to describe is not "no device
    # connected", which is what an empty list reads as everywhere downstream.
    #
    # Through `_warn`, not a hand-rolled `err_console.print`. The comment that
    # used to sit here claimed the stderr line "reaches a GUI that has never
    # heard of `device-error` too" — it does not. `--devices` exits 0, and the
    # GUI reads stderr only off a non-zero exit (`CLIError.exited(stderr:)`),
    # so for a consumer that drops the event this said nothing at all. That
    # made this the one call site whose entire purpose is a warning and the
    # only one that skipped the warning channel.
    #
    # `carried_by`, because the GUI stopped dropping it: `device-error` has a
    # `BuildEvent` case, `DevicesStore` keeps it beside the real devices and
    # `DevicesView` renders the sentence. A `log` event here would be the same
    # fact twice — and this was the site that answered the question differently
    # from `_report_lc_choice` three functions away, on the same reasoning, with
    # a test each pinning the opposite conclusion.
    for f in listing.failures:
        _warn(
            emitter,
            f"device {f.udid} is connected but could not be queried, so it is "
            f"missing from the list below: {f.error}",
            carried_by="device-error",
        )
    if emitter:
        for d in listing.devices:
            emitter.emit(
                "device",
                udid=d.udid,
                name=d.name,
                model=d.model,
                ios_version=d.ios_version,
                connection_type=d.connection_type,
                livecontainer_bundle_id=d.livecontainer_bundle_id,
                # A fact about the device, on the device's own event. The GUI
                # used to spawn `--settings` at launch purely to learn this,
                # then thread the answer down through three views to a chip.
                # `cfg` is already loaded here and was otherwise unread.
                #
                # Deliberately not "the first device when nothing is set": a
                # config with no default has none, and a default naming an
                # unplugged phone marks nothing. The GUI needs to tell those
                # apart from "this one is it".
                is_default=d.udid == cfg.default_device_udid,
            )
        for f in listing.failures:
            emitter.emit("device-error", udid=f.udid, error=f.error)
        emitter.emit("devices-end", count=len(listing.devices), failed=len(listing.failures))
        return
    _print_devices(listing.devices)


def _do_artwork(
    cfg: Config,
    *,
    app_name: str | None = None,
    refresh: bool = False,
    emitter: JsonEmitter | None = None,
) -> None:
    """Resolve icon artwork for one app, or every configured app.

    No `udid` is ever passed to `artwork.resolve` — the device rung stays
    unused here on purpose. It needs a connected phone and this verb has to
    work without one (it's how the GUI paints icons on every launch); the
    device rung exists for a later plan's installed-apps list, not this one.

    A hit is reported two ways, and a miss only one. Under `--json` a hit
    emits `artwork`; a miss emits nothing at all, because the GUI already has
    the right behaviour for "no icon" — the letter tile it draws today — and
    an event here would just be machinery with nothing to consume it.
    Without `--json`, though, a miss has to say something: `resolve` swallows
    every exception on every rung (deliberately — see its module docstring),
    so by the time it returns None the evidence for *why* is already gone,
    and the console line below is the only place left that can still name
    what was tried. It asks `artwork.attempted_sources` rather than working
    the skipping rules out for itself — this used to re-derive them from
    `cached` alone, which was right only because this caller never passes a
    udid.

    A `refresh` miss is a special case of that, and worse if left unlabelled:
    `resolve` never deletes on a miss, so a user who ran `--refresh-artwork`
    *because* an icon looked wrong gets back exactly the same file, with no
    output and no error, and no way to tell "still current" from "the tool
    said nothing". So when a refresh misses but a cached PNG from an earlier
    resolution survives, the console line says so by name and names the file
    to delete for a clean re-resolve — the one escape hatch this CLI has for
    that state. Under `--json` this stays silent like every other miss.
    """
    if app_name:
        if app_name not in cfg.apps:
            available = ", ".join(sorted(cfg.apps)) or "(none configured)"
            raise _fatal(
                ConfigError(
                    f"app '{app_name}' is not configured",
                    hint=f"available apps: {available}. Edit {cfg.path} to add one.",
                )
            )
        names = [app_name]
    else:
        names = sorted(cfg.apps)

    for name in names:
        app_cfg = cfg.apps[name]
        cached = find_in_cache(cfg, app_cfg)
        # Bound once, so the miss line below reports the rungs for the exact
        # inputs `resolve` was handed rather than for a second reading of them.
        ipa = cached.path if cached else None
        # Guarded per app, because `resolve`'s *write* path is the one part of
        # that module that can raise — every rung swallows everything, but
        # `_atomic_write` and `write_sidecar` do not, so a full disk or a
        # root-owned cache directory raises out of the first app and the
        # remaining seven never get looked at. The GUI decodes with `try?` and
        # would show a screen of letter tiles with no explanation.
        #
        # One app's failure must not be the run's, so this reports and carries
        # on. Routed through `_warn` rather than swallowed like an ordinary
        # miss, and the distinction is the point: a miss means nothing had this
        # app's icon, which is expected and needs no telling. This means the
        # cache could not be *written*, which is a real fault on the machine
        # and the user should hear about it once.
        try:
            found = artwork.resolve(app_cfg.bundle_id, ipa=ipa, refresh=refresh)
        except Exception as e:
            _warn(emitter, f"{name}: couldn't cache artwork ({e})")
            continue
        # Hit or miss first, then how to say it — rather than `--json` first
        # with hit/miss re-derived inside each half. Three outcomes, and this
        # order is the one that states each of them once.
        if found is not None:
            if emitter:
                emitter.emit(
                    "artwork",
                    bundle_id=found.bundle_id,
                    path=str(found.path),
                    source=found.source,
                    cached=found.cached,
                )
            else:
                console.print(f"[green]✓[/green] {name} — {found.source} artwork at {found.path}")
            continue
        # A miss emits nothing under --json — see the docstring above.
        if emitter:
            continue
        # A refresh that misses is not an ordinary miss: `resolve` never
        # deletes the cached PNG on a miss, so the file a previous resolution
        # wrote is still sitting there, silently unchanged, unless this says
        # so. See the docstring above.
        stale = artwork.artwork_path(app_cfg.bundle_id)
        if refresh and stale.exists():
            console.print(
                f"[yellow]⚠[/yellow] {name}: refresh found nothing; keeping the "
                f"cached icon ({stale})"
            )
            console.print("  delete that file to force a clean re-resolve")
            continue
        # Named with `resolve`'s own vocabulary — the same tokens the hit line
        # above and the `artwork` event's `source` field use — so "tried: ipa,
        # app-store" and "— app-store artwork at …" read as one glossary.
        tried = ", ".join(artwork.attempted_sources(ipa, udid=None))
        console.print(f"[yellow]no artwork found for[/yellow] {name} (tried: {tried})")


# How a state renders where there is no room for its summary: the --list table
# and the verb's own output. Looked up with `.get`, never indexed — a kind this
# map hasn't heard of is precisely "unknown", and a KeyError here would take
# down a whole listing over a badge.
_DELIVERY_MARKS = {"never": "✗", "stale": "⚠", "current": "✓", "unknown": "?"}


class _UnreadableLog:
    """Stands in for a delivery log that exists but wouldn't open.

    `None` already means "no database yet", which `_delivery_state_for` reads
    as "never delivered" — the file is created by the first build, so that is
    a true reading. It would be a false one, on every app at once, for a
    database that is merely locked or corrupt. This raises into the same guard
    that handles a failed read, so the listing degrades to `unknown` instead of
    either lying or taking the GUI's app list down with it.
    """

    # Signature mirrors the BuildLog method it stands in for.
    def latest_delivery(self, app: str, *, udid: str | None = None) -> sqlite3.Row | None:
        raise sqlite3.OperationalError(f"the delivery log could not be opened (reading {app})")


def _state_payload(app_name: str, state: DeliveryState) -> dict[str, Any]:
    """A DeliveryState as the `delivery-state` event's fields."""
    return {
        "app": app_name,
        "kind": state.kind,
        "summary": state.summary,
        "reasons": list(state.reasons),
        "pushed_at": state.pushed_at,
    }


def _delivery_state_for(
    cfg: Config,
    app_name: str,
    app_cfg: AppConfig,
    log: BuildLog | _UnreadableLog | None,
    *,
    udid: str | None = None,
) -> dict[str, Any]:
    """The delivery state of one app, as an event payload.

    Resolves the source version the same way a build would, so the digest is
    comparable to the one recorded at push time.

    `log` is passed in rather than constructed here: BuildLog.__init__ runs
    executescript plus two PRAGMAs and takes a WRITE lock, so building one per
    app would turn a read-only `--list` into a writer that opens and migrates
    the user's database once per app in the loop. The caller constructs it once
    (or passes None when the database does not exist yet).

    The row is read FIRST, before anything touches the filesystem. "Never
    delivered" is a fact about the log alone, so an app that has never been
    pushed costs no I/O — and, more importantly, still reports `never` on a
    machine with an empty IPA cache. Asking `find_in_cache` first would answer
    `unknown` there, which renders as no badge at all, hiding the one state
    this feature exists to surface on exactly the machines that have it.

    `udid` narrows "is this app current?" to "is this app current on THAT
    phone?". Left as `None`, the answer is "anywhere" — the meaning every
    caller before the per-device pane relies on, so it stays the default.
    """
    try:
        row = log.latest_delivery(app_name, udid=udid) if log is not None else None
    except sqlite3.Error:
        return _state_payload(
            app_name,
            DeliveryState(kind="unknown", summary="The delivery log can't be read."),
        )

    if row is None:
        # Wording deliberately identical to diff_delivery's own `never` branch,
        # which this path skips because it has no digest to hand it; a test
        # pins the two together.
        return _state_payload(
            app_name,
            DeliveryState(kind="never", summary="Never delivered to a device."),
        )

    if row["reconcile_ok"] == 0:
        # The device was checked and the artifact was not there. Our record
        # cannot be trusted to describe the phone, whatever the digest would
        # say — so this answers ahead of the comparison rather than after it.
        #
        # What that absence proves depends on how the app was delivered, and
        # the row carries which. A livecontainer reconcile only lists
        # Documents/, and an imported-then-cleaned-up IPA is missing from there
        # too (§3) — so state the evidence, not the conclusion.
        #
        # The sideload arm is currently unreachable, and deliberately kept.
        # `_reconcile_app` returns early on every non-livecontainer row (§7.1:
        # the row records the pre-cyan bundle id, so the only question
        # available would false-demote a default config), so `reconcile_ok` is
        # never set for one. It stays because it is the wording that arm will
        # need once a column records the effective installed bundle id — an
        # authoritative answer should not be hedged like a circumstantial one.
        return _state_payload(
            app_name,
            DeliveryState(
                kind="stale",
                summary=(
                    "Our record says delivered; the file is not on the device."
                    if row["delivery"] == "livecontainer"
                    else "The phone is out of date."
                ),
                reasons=["last delivery is no longer on the device"],
                pushed_at=row["timestamp"],
            ),
        )

    # Below the reconcile branch on purpose: both answer `stale`, and evidence
    # from the device this minute is a more specific thing to tell the user
    # than how the row got written.
    if row["origin"] == "reinstall":
        # `--reinstall-build` re-ships an archived IPA, and nothing recorded
        # what went into that archive: `compute_inputs_digest` hashes the tweak
        # files as they are NOW, so a digest taken for it would describe a
        # build that was never pushed. There is nothing to compare, and the
        # honest answer is "the phone holds something older than this config,
        # and how much older can't be reconstructed".
        #
        # `stale` rather than `unknown` because `unknown` renders as no badge
        # (§6), which would leave the user believing the phone matches the
        # config in front of them. Over-reporting work to do is an annoyance;
        # under-reporting it is the harm this feature exists to stop.
        return _state_payload(
            app_name,
            DeliveryState(
                kind="stale",
                summary="An archived build was re-installed; what went into it was never recorded.",
                reasons=["nothing to compare — push again to re-establish the baseline"],
                pushed_at=row["timestamp"],
            ),
        )

    cached = find_in_cache(cfg, app_cfg)
    if cached is None:
        # No build is possible right now, so there is nothing to compare
        # against; report `unknown` rather than guessing a version.
        return _state_payload(
            app_name,
            DeliveryState(
                kind="unknown",
                summary="No source IPA cached — can't tell what a build would produce.",
                pushed_at=row["timestamp"],
            ),
        )

    try:
        # `is not None`, not truthiness: json.dumps({}) is "{}", which is
        # truthy today, but a row that recorded an empty option set is not the
        # same claim as a row from before the column existed. NULL is the
        # legacy signal and only NULL should read as one.
        cyan = json.loads(row["cyan"]) if row["cyan"] is not None else None
        if cyan is not None and not isinstance(cyan, dict):
            # Decoding is not the same check as decoding to the right shape:
            # `[]` and `"oops"` are both valid JSON, and diff_delivery calls
            # .get() on whatever this is — outside its own guard and therefore
            # outside every guard. Raising here routes them to the same answer
            # as a cell that wouldn't decode at all.
            raise TypeError(f"cyan is {type(cyan).__name__}, not an object")
        snapshot = DeliveredSnapshot(
            inputs_digest=row["inputs_digest"],
            digest_version=row["digest_version"],
            bundle_id=row["bundle_id"],
            cyan=cyan,
            delivery=row["delivery"],
            source_version=row["source_version"],
            # No shape check needed: diff_delivery's own guard covers every way
            # this can be wrong, because it is the only thing that reads it.
            tweaks=json.loads(row["tweaks"]) if row["tweaks"] else [],
            pushed_at=row["timestamp"],
        )
    except (TypeError, ValueError):
        # diff_delivery guards its own parse of `tweaks` so that one bad row
        # can't take down a whole listing, but the row is decoded here, outside
        # that guard — which makes it only as good as this one. Spec §8: every
        # failure mode on the --list path degrades to `unknown` rather than
        # raising, because --list is the GUI's entire app list.
        return _state_payload(
            app_name,
            DeliveryState(
                kind="unknown",
                summary=(
                    "The delivery record can't be read. "
                    "Push again to re-establish the baseline."
                ),
                pushed_at=row["timestamp"],
            ),
        )

    current = compute_inputs_digest(cfg, app_cfg, source_version=cached.version)
    return _state_payload(app_name, diff_delivery(current, snapshot))


def _open_delivery_log(cfg: Config) -> BuildLog | _UnreadableLog | None:
    """One delivery log for a whole listing pass, or None if there is no database.

    Constructing a BuildLog migrates and write-locks the file, so it must
    happen once per command rather than once per app. The migration is what
    makes this worth doing at all: a log.sqlite written before the `deliveries`
    table existed would otherwise fail every query against it.
    """
    db = cfg.state_dir / "log.sqlite"
    if not db.exists():
        return None
    try:
        return BuildLog(db)
    except sqlite3.Error:
        return _UnreadableLog()


def _do_delivery_state(
    cfg: Config,
    *,
    app_name: str | None = None,
    udid: str | None = None,
    emitter: JsonEmitter | None = None,
) -> None:
    # Resolved before the log is opened, so an unknown app name fails without
    # having touched the database.
    apps = {name: cfg.get_app(name) for name in ([app_name] if app_name else sorted(cfg.apps))}
    log = _open_delivery_log(cfg)
    for name, app_cfg in apps.items():
        payload = _delivery_state_for(cfg, name, app_cfg, log, udid=udid)
        if emitter:
            emitter.emit("delivery-state", **payload)
        else:
            mark = _DELIVERY_MARKS.get(payload["kind"], "?")
            console.print(f"{mark} [bold]{name}[/bold] — {payload['summary']}")
            for reason in payload["reasons"]:
                console.print(f"    [dim]{reason}[/dim]")


def _reconcile_app(
    log: BuildLog, app_name: str, *, udid: str | None, emitter: JsonEmitter | None = None
) -> None:
    """Check that the device still has what we recorded delivering.

    Demotes only. A present file does NOT confirm anything beyond its presence
    — LiveContainer's import is a separate manual step this tool cannot observe
    (§3) — so `reconcile_ok = 1` means "the file is there", never "the app is
    installed", and nothing upstream reads it as an upgrade.

    And a demote must rest on evidence, which is what every guard below is
    about: an unreachable device, a row that names nothing to look for, and a
    row that was pushed to a different phone all leave the record untouched. A
    spurious demotion sends the user chasing a delivery that was fine, which is
    how a badge earns the right to be ignored.

    Takes the log rather than a Config: nothing here reads configuration, and
    the row is the only description of the delivery that can be trusted —
    config.yaml can have been edited since.
    """
    row = log.latest_delivery(app_name)
    if row is None:
        return

    # Listing a LiveContainer's Documents/ says nothing about a sideload, which
    # is a real top-level install. Spec §7.1: the obvious alternative — ask
    # InstallationProxyService whether the bundle id is installed — does not
    # work, because cyan ships `new_bundle_id or f"{bundle_id}.plus"` whenever
    # keep_bundle_id is false (the default) while this row records the pre-cyan
    # id. It would answer "not installed" for a working app and demote nearly
    # every sideload app on a default config. So this is a permanent skip until
    # a column records the effective installed bundle id, not a stub.
    if row["delivery"] != "livecontainer":
        return

    # A row from before this feature carries neither field. Without the
    # container there is nowhere to look; without the filename the membership
    # test below is `None in names`, false against every device on earth.
    if not row["lc_bundle_id"] or not row["remote_name"]:
        return

    # `latest_delivery` returns the newest row for the app whatever device it
    # went to. Push to phone A, connect phone B, reconcile — and a demote here
    # would paint a stale badge for a device that was never the target. A
    # different phone is not evidence, and a row with no recorded udid is
    # unmatchable rather than "this one": two unknowns must not compare equal.
    if not udid or row["udid"] != udid:
        return

    try:
        names = list_livecontainer_documents(udid=udid, lc_bundle_id=row["lc_bundle_id"])
    except Exception as exc:
        # Deliberately every exception: a device that didn't answer is the
        # "can't tell" case whatever it raised, and the local answer already on
        # screen is the right thing to leave standing. Through `_warn` so it
        # reaches the panel as a `log` event as well as stderr — stdout carries
        # the JSON event stream and cannot hold prose.
        _warn(emitter, f"couldn't check {app_name} on the device: {exc}")
        return

    log.mark_reconciled(int(row["id"]), seen=row["remote_name"] in names)


def _do_reconcile(
    cfg: Config,
    *,
    app_name: str | None = None,
    device: str | None = None,
    emitter: JsonEmitter | None = None,
) -> None:
    """Check the device for what was last delivered, then report the state.

    Best-effort throughout (§7): a device that can't be resolved leaves every
    record untouched and still reports the local answer. Failing outright would
    cost the caller the state report as well as the check it couldn't make —
    and the GUI asks for both in one call.
    """
    # Resolved first so an unknown app name fails before anything touches a
    # database or a device.
    names = [app_name] if app_name else sorted(cfg.apps)
    for name in names:
        cfg.get_app(name)

    log = _open_delivery_log(cfg)
    # `_UnreadableLog` and None both mean there is nothing here to reconcile —
    # and no reason to wake a device to find that out.
    if isinstance(log, BuildLog):
        try:
            udid, _transport = select_device(device or cfg.default_device_udid)
        except GraftError as exc:
            # `_warn`, like every other warning in this module: stdout is where
            # the JSON events go, so prose can only reach a machine consumer as
            # a `log` event.
            _warn(emitter, f"no device checked: {exc.message}")
        else:
            try:
                for name in names:
                    _reconcile_app(log, name, udid=udid, emitter=emitter)
            except sqlite3.Error as exc:
                # `mark_reconciled` is a write, and the database this writes to
                # is the one a concurrent build is writing its own rows into —
                # "database is locked" is a live possibility here in a way it
                # isn't for a read. Bookkeeping that failed must not lose the
                # user the state report they asked for in the same breath.
                _warn(emitter, f"couldn't update the delivery log: {exc}")

    # Opens the log a second time. That is one extra open on an explicit verb
    # the user waited for a USB round-trip on, not on `--list` — and it keeps
    # the reporting path identical to the one `--delivery-state` exercises.
    _do_delivery_state(cfg, app_name=app_name, emitter=emitter)


def _print_apps(cfg: Config, states: dict[str, dict[str, Any]]) -> None:
    table = Table(title="configured apps")
    table.add_column("name", style="bold")
    table.add_column("bundle id")
    table.add_column("version pin")
    table.add_column("tweaks")
    table.add_column("delivery")
    for name, a in sorted(cfg.apps.items()):
        kind = states.get(name, {}).get("kind", "unknown")
        table.add_row(
            name,
            a.bundle_id,
            a.preferred_version,
            ", ".join(a.tweaks) or "[dim]none[/dim]",
            f"{_DELIVERY_MARKS.get(kind, '?')} {kind}",
        )
    console.print(table)


def _do_search(query: str, emitter: JsonEmitter | None = None) -> None:
    pkgs = canister.search(query, limit=20)
    if emitter:
        for p in pkgs:
            emitter.emit(
                "canister-result",
                package_id=p.package_id,
                name=p.name,
                version=p.version,
                description=p.description,
                author=p.author,
                section=p.section,
                repository_id=p.repository_id,
                price=p.price,
                size_bytes=p.package_size,
                depiction=p.depiction,
            )
        emitter.emit("canister-search-end", count=len(pkgs))
        return
    if not pkgs:
        console.print(f"[yellow]no results for[/yellow] {query!r}")
        return
    table = Table(title=f"canister.me — top {len(pkgs)} match(es) for {query!r}")
    table.add_column("package id", style="cyan", no_wrap=True)
    table.add_column("name", style="bold", overflow="fold", max_width=24)
    table.add_column("version", no_wrap=True)
    table.add_column("repo", no_wrap=True)
    table.add_column("price")
    table.add_column("description", overflow="fold", max_width=36)
    for p in pkgs:
        price = p.price if p.price.lower() != "free" else "[dim]free[/dim]"
        table.add_row(p.package_id, p.name, p.version, p.repository_id, price, p.description)
    console.print(table)
    console.print(
        "[dim]→ graft --fetch <package id>  downloads the .deb into your tweaks dir[/dim]"
    )


def _do_fetch(
    cfg: Config,
    target: str,
    emitter: JsonEmitter | None = None,
    expected_sha: str | None = None,
) -> None:
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    if emitter:
        emitter.emit("canister-fetch-start", target=target)
        try:
            dest = canister.fetch_deb(target, cfg.tweaks_dir, expected_sha)
        except GraftError as e:
            emitter.emit("canister-fetch-end", ok=False, error=str(e))
            return
        emitter.emit(
            "canister-fetch-end",
            ok=True,
            filename=dest.name,
            size_bytes=dest.stat().st_size,
            path=str(dest),
        )
        return
    console.print(f"→ fetching [cyan]{target}[/cyan] into {cfg.tweaks_dir}")
    dest = canister.fetch_deb(target, cfg.tweaks_dir, expected_sha)
    size_kb = dest.stat().st_size / 1024
    console.print(f"[green]✓[/green] {escape(dest.name)}  ({size_kb:,.1f} KB)")
    console.print(
        f"[dim]→ add [bold]{escape(dest.name)}[/bold] to an app's `tweaks:` list in "
        f"{escape(str(cfg.path))} to use it[/dim]"
    )


# The rule the five success lines below follow, and why it is those five.
#
# `console.print` parses `[...]` as markup: on rich 15 a closing-tag-shaped
# token raises MarkupError and an unrecognised opening tag is deleted in
# silence. Most of this module's interpolations are values it produced itself.
# These are not — a repo's `Origin:` field, an App Store listing's name, a .deb
# filename chosen by whoever built it, a URL from argv — and, crucially, each is
# printed *after* the write it announces.
#
# So a package named with a `[/x]`-shaped token does not merely garble a line.
# `_reported` catches the MarkupError and turns it into `_fatal`, so the CLI
# exits non-zero on config.yaml that has already been changed: the GUI is told
# the repo was not added, about a repo that was. That is the class this escaping
# closes — an exit code lying about a mutation — rather than every raw
# interpolation in the file.
def _do_add_repo(cfg: Config, url: str, emitter: JsonEmitter | None = None) -> None:
    norm = repo.normalize_url(url)
    release = repo.fetch_release(norm)
    text = repo.fetch_packages(norm)
    pkgs = repo.parse_packages(text)
    # Cache the index first, then commit to config — a failed cache write must
    # never leave config.yaml referencing a repo with no cached index.
    cache = repo.cache_path(cfg.state_dir, norm)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text)
    except OSError as e:
        raise repo.RepoError(f"failed to cache package index for {norm}: {e}") from e
    add_repo(cfg.path, norm, release.name)
    if emitter:
        emitter.emit(
            "repo-added",
            url=norm,
            name=release.name,
            origin=release.origin,
            package_count=len(pkgs),
        )
        return
    console.print(
        f"[green]✓[/green] added [bold]{escape(release.name)}[/bold]  "
        f"({len(pkgs)} packages)  {escape(norm)}"
    )


def _do_remove_repo(cfg: Config, url: str, emitter: JsonEmitter | None = None) -> None:
    norm = repo.normalize_url(url)
    remove_repo(cfg.path, norm)
    repo.cache_path(cfg.state_dir, norm).unlink(missing_ok=True)
    if emitter:
        emitter.emit("repo-removed", url=norm)
        return
    console.print(f"[green]✓[/green] removed repo {escape(norm)}")


def _default_cyan_block(display_name: str) -> dict[str, Any]:
    return {
        "display_name": display_name,
        "keep_bundle_id": True,
        "strip_extensions": False,
        "strip_supported_devices": True,
        "compression_level": 6,
    }


_VALID_DELIVERIES = {"sideload", "livecontainer"}


def _do_add_app_url(
    cfg: Config,
    url_or_id: str,
    delivery: str = "sideload",
    emitter: JsonEmitter | None = None,
) -> None:
    if delivery not in _VALID_DELIVERIES:
        raise ConfigError(
            f"invalid delivery {delivery!r} (must be one of: "
            f"{', '.join(sorted(_VALID_DELIVERIES))})"
        )
    track_id = itunes.parse_id_from_url(url_or_id)
    info = itunes.lookup(track_id)
    desired = slugify_name(info.name)
    app_cfg = {
        "bundle_id": info.bundle_id,
        "preferred_version": "latest",
        "delivery": delivery,
        "tweaks": [],
        "cyan": _default_cyan_block(info.name),
    }
    # dedup atomically inside add_app — re-reads disk so concurrent --add-app
    # calls can't race past each other onto the same slug.
    name = add_app(cfg.path, desired, app_cfg, dedup=True)
    if emitter:
        emitter.emit(
            "app-added",
            name=name,
            bundle_id=info.bundle_id,
            display_name=info.name,
            delivery=delivery,
        )
        return
    console.print(
        f"[green]✓[/green] added [bold]{escape(info.name)}[/bold]  "
        f"({escape(info.bundle_id)})  → {escape(str(cfg.path))} apps.{escape(name)}"
    )


def _do_add_app_manual(
    cfg: Config,
    *,
    name: str,
    bundle_id: str,
    display_name: str | None,
    delivery: str,
    emitter: JsonEmitter | None = None,
) -> None:
    if delivery not in _VALID_DELIVERIES:
        raise ConfigError(
            f"invalid delivery {delivery!r} (must be one of: "
            f"{', '.join(sorted(_VALID_DELIVERIES))})"
        )
    desired = slugify_name(name)
    # Resolve the post-dedup label preview now; if a collision pushes us to
    # `name_2`, the display name still tracks what the user typed (or the
    # original slug as a fallback) — not the suffixed slug.
    label = display_name or desired
    app_cfg: dict[str, Any] = {
        "bundle_id": bundle_id,
        "preferred_version": "latest",
        "delivery": delivery,
        "tweaks": [],
        "cyan": _default_cyan_block(label),
    }
    final_name = add_app(cfg.path, desired, app_cfg, dedup=True)
    if emitter:
        emitter.emit(
            "app-added",
            name=final_name,
            bundle_id=bundle_id,
            display_name=label,
            delivery=delivery,
        )
        return
    console.print(
        f"[green]✓[/green] added [bold]{escape(label)}[/bold]  "
        f"({escape(bundle_id)})  → {escape(str(cfg.path))} apps.{escape(final_name)}"
    )


def _say_plain(text: str) -> None:
    """Human-mode output for a module that has no console of its own.

    Not markup: `appstore.py`'s lines carry tool paths and tool error text,
    and the same reason `_fatal` escapes applies.
    """
    console.print(text, markup=False, highlight=False)


def _do_provide_ipa(
    cfg: Config,
    *,
    app_name: str,
    source_path: Path,
    emitter: JsonEmitter | None = None,
    allow_unverified: bool = False,
) -> None:
    """Validate a user-supplied IPA, decrypt it if it came straight from the
    App Store, and stage it.

    The decrypt is new (2026-11-17). An encrypted drop used to be cached as-is
    and the build then died at stage 2.5; now the main binary's cryptid decides,
    and an encrypted one goes through UnfairPlay exactly as `--appstore-get`'s
    download does — or is refused with the enrollment command, so nothing the
    build would reject is ever cached. A decrypted drop is untouched.
    """
    app = cfg.get_app(app_name)  # raises ConfigError if unknown
    # Validated before anything is decrypted: a drop on the wrong app should
    # fail in a moment on its plist, not after UnfairPlay has read a gigabyte.
    provide_ipa_module.validate_ipa(source_path, app.bundle_id)
    with appstore.decrypted_if_encrypted(
        cfg, app_name, source_path, emitter=emitter,
        allow_unverified=allow_unverified,
        say=_say_plain, warn=lambda message: _warn(emitter, message),
    ) as usable:
        _stage_ipa(cfg, app_name=app_name, source_path=usable, emitter=emitter)


def _stage_ipa(
    cfg: Config,
    *,
    app_name: str,
    source_path: Path,
    emitter: JsonEmitter | None,
) -> None:
    """Copy a decrypted IPA into the cache under its canonical name, and say so.

    Shared by `--provide-ipa` and `--appstore-get`, so both end in the same
    `ipa-provided` event the GUI already refreshes on.
    """
    app = cfg.get_app(app_name)
    dest, info = provide_ipa_module.provide(
        cache_dir=cfg.cache_dir,
        expected_bundle_id=app.bundle_id,
        source_path=source_path,
    )
    size = dest.stat().st_size
    if emitter:
        emitter.emit(
            "ipa-provided",
            app=app_name,
            bundle_id=info.bundle_id,
            version=info.version,
            filename=dest.name,
            path=str(dest),
            size_bytes=size,
        )
        return
    console.print(
        f"[green]✓[/green] cached [bold]{dest.name}[/bold]  "
        f"({size // 1024} KB)  → {dest}"
    )


def _do_cached_ipa(
    cfg: Config,
    *,
    app_name: str,
    emitter: JsonEmitter | None = None,
) -> None:
    app = cfg.get_app(app_name)  # raises ConfigError if unknown
    cached = find_in_cache(cfg, app)
    if emitter:
        if cached is None:
            emitter.emit("cached-ipa", app=app_name, exists=False)
            return
        emitter.emit(
            "cached-ipa",
            app=app_name,
            exists=True,
            bundle_id=cached.bundle_id,
            version=cached.version,
            filename=cached.path.name,
            path=str(cached.path),
            size_bytes=cached.path.stat().st_size,
        )
        return
    if cached is None:
        console.print(f"[yellow]no cached IPA for[/yellow] {app_name}")
        return
    size = cached.path.stat().st_size
    console.print(
        f"[bold]{cached.path.name}[/bold]  ({size // 1024} KB)  → {cached.path}"
    )


def _do_staged_ipas(cfg: Config, *, emitter: JsonEmitter | None = None) -> None:
    """Everything in `cache_dir`, as each file's own Info.plist describes it.

    The cache half of a join the GUI's device pane makes: the phone says what
    is installed, this says what is staged, and an app with both can be added
    and built without touching the network. `--cached-ipa` cannot answer it —
    it takes a config app name, and the whole point is the app that is not in
    the config yet.

    Read through `IPAInfoMemo`, the same reader `find_in_cache` uses, so an
    unchanged cache costs a `stat` per file rather than a central-directory
    parse: 16.1 ms over ten IPAs with half of them cold, 0.2 ms warm.

    `preferred` is decided here rather than in Swift. Two versions of one app
    can sit in this directory, and which one a build would use — assuming
    `preferred_version: latest`, since this listing has no app config to pin
    against — is `find_in_cache`'s rule: `max` by `version_key`, because
    `21.9.1` sorts after `21.16.2` as a string and before it as a version. An
    app pinned to an explicit version resolves to that version regardless of
    what else is newest in cache; moot for every caller today, which reads
    `preferred` only to decide what to offer, never what a pinned app would
    build. A second implementation of that version comparison in another
    language is still a second thing to be wrong.
    """
    found: list[tuple[Path, IPAInfo, int]] = []
    by_bundle: dict[str, list[tuple[Path, IPAInfo]]] = {}
    if cfg.cache_dir.is_dir():
        # `IPAInfoMemo.load`/`.save` degrade to no-ops on a missing directory
        # already — the guard below isn't rescuing anything from a crash. It
        # is here so a cache that has never been created costs one `stat`
        # (this check) rather than an open-plus-readdir attempt against a
        # path that structurally holds nothing — the same early exit
        # `find_in_cache` takes on `not cfg.cache_dir.exists()`.
        memo = IPAInfoMemo.load(cfg.cache_dir)
        try:
            for path in sorted(cfg.cache_dir.glob("*.ipa")):
                # `*.ipa` matches leading dots too — pathlib globbing is not a
                # shell glob. `provide_ipa.py`
                # (`.{bundle_id}_{version}.<random>.partial.ipa`) writes an
                # in-flight copy straight into cache_dir
                # under exactly that shape, and for a window it is a complete,
                # readable zip — mid-write or freshly-written-but-not-yet-
                # `shutil.move`d into its canonical name. `find_in_cache`
                # never trips on it: its first two globs require the filename
                # to start with the real `{bundle_id}_` prefix, and its third
                # match's `stem.startswith(short)` test cannot match a stem
                # that starts with a dot either, since no real bundle short
                # name does. This listing has no such structural guard, so it
                # needs its own.
                if path.name.startswith("."):
                    continue
                info = memo.info_for(path)
                # Every reason `read_info_plist` would have raised — not a zip,
                # no Payload/<App>.app/Info.plist, vanished between the glob and
                # now. Skipped, as `find_in_cache` skips them.
                if info is None:
                    continue
                # `find_in_cache`'s own candidate test (`_is_cache_candidate`,
                # shared rather than mirrored — see its own doc), applied
                # before a file is even a `found`/`preferred` candidate below.
                # A file matching neither of its two shapes is invisible to a
                # build no matter what its own Info.plist says, and offering
                # it here anyway told the sheet "the build will use this" for
                # a file that build could then never find: a false positive,
                # not a miss, and the one shape this whole feature exists to
                # prevent.
                #
                # Filtered out entirely rather than kept and marked
                # `preferred: false`: `StagedIPAStore.load()` keeps the first
                # `staged-ipa` record it sees for a bundle id regardless of
                # `preferred`, and `InstalledAppsSection.rowState` offers
                # `.addable` for anything it holds without ever reading
                # `preferred` itself — so a lone non-preferred record would
                # still reach the row as an addable app when this is the
                # *only* file for that bundle id. Omitting it here is the
                # only shape that keeps the promise.
                if not _is_cache_candidate(path, info.bundle_id):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    # Read here, in the same pass that just proved the file
                    # readable, rather than statted again later against a
                    # `payload`/console line already half-built: a writer
                    # finishing its `shutil.move` of a same-named file
                    # between a first and a second pass raised
                    # `FileNotFoundError` from an unguarded `stat`, uncaught,
                    # after this function had already emitted some
                    # `staged-ipa` lines and before `staged-ipas-end`. Task 5's
                    # GUI store has to be able to tell "no IPAs staged" apart
                    # from "the listing never ran" — that is the whole reason
                    # `staged-ipas-end` exists as a sentinel — so it must not
                    # treat a run that died mid-stream as a clean empty
                    # answer. One pass removes the window instead of
                    # narrowing it, rather than leaning on that store to
                    # notice.
                    continue
                found.append((path, info, size))
                by_bundle.setdefault(info.bundle_id, []).append((path, info))
        finally:
            memo.save()

    # Which file in each bundle's group a build would actually use —
    # `find_in_cache`'s own rule (`max` by `version_key`), scanned in the same
    # order via the shared `_cache_candidates`: canonically-named files
    # before hand-copied ones, each bucket sorted by filename. That order,
    # not just "unique", is what a flat filename sort across every staged
    # file used to get backwards — see `_cache_candidates`'s own doc. Every
    # path in `entries` is guaranteed to appear in `order` too, since both
    # came from the same `_is_cache_candidate` test against the same
    # `bundle_id`.
    preferred_path: dict[str, Path] = {}
    for bundle_id, entries in by_bundle.items():
        order = _cache_candidates(cfg.cache_dir, bundle_id)
        info_by_path = dict(entries)
        ordered = [(p, info_by_path[p]) for p in order if p in info_by_path]
        winner = max(ordered, key=lambda e: repo.version_key(e[1].version))
        preferred_path[bundle_id] = winner[0]

    if emitter:
        for path, info, size in found:
            note = stripped_note(path)
            payload: dict[str, Any] = {
                "bundle_id": info.bundle_id,
                "version": info.version,
                "display_name": info.display_name,
                "filename": path.name,
                "path": str(path),
                "size_bytes": size,
                "preferred": preferred_path.get(info.bundle_id) == path,
                "stripped": note.removed if note else [],
            }
            # Absent, not empty — see the event's own note in `EVENT_TYPES`.
            if note and note.lost is not None:
                payload["lost"] = note.lost
            if note and note.importance:
                payload["importance"] = note.importance
            emitter.emit("staged-ipa", **payload)
        emitter.emit("staged-ipas-end", count=len(found))
        return

    if not found:
        console.print(f"[yellow]nothing staged in[/yellow] {escape(str(cfg.cache_dir))}")
        return
    for path, info, size in found:
        note = stripped_note(path)
        partial = f"  [yellow](minus {len(note.removed)})[/yellow]" if note else ""
        console.print(
            f"[bold]{escape(path.name)}[/bold]  "
            f"({size // 1024} KB)  "
            f"{escape(info.bundle_id)} v{escape(info.version)}{partial}"
        )


def _is_same_file(a: Path, b: Path) -> bool:
    """`os.path.samefile`, treating "can't tell" as "no".

    A plain `!=` on the paths could never fail — it compares strings — but
    `samefile` stats both, and `_do_staged_ipas`'s own comment (above) records
    this exact race happening for real in this directory: a file present when
    `_cache_candidates` glob ran and gone by the time something opens it,
    a writer's `shutil.move` landing between two passes. Both sides of
    `_do_delete_staged`'s guard already treat a vanished file as "not a real
    candidate" — `memo.info_for` returns `None` for one, and this makes a
    vanished-mid-comparison one degrade the same way rather than raising out
    of the only destructive verb here.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _do_delete_staged(
    cfg: Config,
    *,
    filename: str,
    force: bool,
    emitter: JsonEmitter | None = None,
) -> None:
    """Remove one staged IPA and its stripped sidecar.

    Keyed on filename rather than bundle id because filenames are unique within
    `cache_dir` and bundle ids are not — two versions of one app are two files,
    and telling them apart is most of the point of the surface that calls this.

    `Path(filename).name` is *compared* against `filename` deliberately rather
    than trusting the argument — not taken, which would delete `cache_dir /
    Path(filename).name` and silently accept a path that had one. This deletes
    files, and a caller that passed `../../something` must be refused outright
    rather than quietly reinterpreted.

    The refusal is narrower than "an app claims this bundle id", in three ways
    that all have to hold together or the guard clears a delete it shouldn't:

    1. It is decided *per claiming app*, not once for the bundle id.
       `find_in_cache` also filters candidates against `app.preferred_version`
       when that isn't `"latest"` — a sibling with a different version is not
       something a pinned app can build from, even though it shares the
       bundle id. Two app entries can claim the same bundle id with different
       pins, so this is asked once per app rather than once for the group, and
       the app list a refusal reports is the ones actually left with nothing
       (`broken`), not every app that merely shares the bundle id
       (`claimed_by`) — an app a sibling still satisfies has no business in
       that list.
    2. It never fires for a file `find_in_cache` could not have reached in the
       first place, for *any* claiming app. A mis-named file with a matching
       plist was already invisible to every app that might claim its bundle
       id — `find_in_cache` filters candidates by filename before it ever
       opens one. And an app pinned away from `target`'s own version was
       already getting `None` from `find_in_cache` before this delete, since
       `target` was never a match for that app's pin either — deleting it
       changes nothing that app could build, so `claimed_by` excludes an app
       target itself does not satisfy, the same test a sibling has to pass in
       (1). Both are "target was never reachable *for this app*", just from
       filename and from version respectively.
    3. The sibling search applies the same two filters `find_in_cache` does:
       `_cache_candidates` for filename, then each candidate's own plist for
       bundle id. Skipping the plist half would let a file merely *named* like
       the app count as a sibling that isn't really usable.

    With every claiming app still able to build from some remaining file,
    nothing is lost and a confirmation would be noise.
    """
    if Path(filename).name != filename:
        raise ConfigError(
            f"{filename!r} is not a filename",
            hint="pass the name of a file in the cache, not a path",
        )
    target = cfg.cache_dir / filename
    if not target.is_file():
        raise ConfigError(
            f"no staged IPA named {filename!r} in {cfg.cache_dir}",
            hint="run --staged-ipas to see what's in the cache",
        )

    if not force:
        # `IPAInfoMemo` rather than a direct `read_info_plist(target)`:
        # measured, the uncached read is 18.7 ms and is nearly the entire cost
        # of an unforced call, against 0.06 ms for a memo load + hit + save —
        # and `--staged-ipas` just warmed this same memo for this same
        # directory. It also changes what an unreadable file does: a direct
        # read *raises*, so a truncated `.ipa` in the cache could not be
        # deleted without `--force` and failed with a bad-zip-magic error that
        # never mentions `--force`. `info_for` returning `None` instead lets
        # junk be cleaned out normally — which is arguably what a cache pane
        # is for — because `find_in_cache` could never have served that file
        # to a build either.
        memo = IPAInfoMemo.load(cfg.cache_dir)
        try:
            info = memo.info_for(target)
            if info is not None and _is_cache_candidate(target, info.bundle_id):
                claimed_by = sorted(
                    name for name, app in cfg.apps.items()
                    if app.bundle_id == info.bundle_id
                    # An app pinned away from `target`'s own version was
                    # already unbuildable before this delete — `find_in_cache`
                    # would have returned None for it regardless, since
                    # `target` was never a candidate its pin would accept
                    # either. Deleting it changes nothing for that app, so it
                    # has no stake in this refusal. `"latest"` accepts every
                    # version, matching how `find_in_cache` treats it.
                    and (app.preferred_version == "latest" or info.version == app.preferred_version)
                )
                if claimed_by:
                    # Two filters, because `_cache_candidates` is only the
                    # first half of `find_in_cache`'s rule. It matches on
                    # *filename* — the canonical `{bundle_id}_*.ipa` or a
                    # hand-copied stem — and `find_in_cache` then opens each
                    # candidate and drops it on `info.bundle_id !=
                    # app.bundle_id`. Skipping that second half here would let
                    # a file merely *named* like this app count as a sibling,
                    # and the guard would wave through a delete that leaves
                    # the app with nothing to build from. The plist decides,
                    # exactly as it does there.
                    #
                    # `os.path.samefile` rather than `!=`: on case-insensitive
                    # APFS, `_cache_candidates`' own glob can return the exact
                    # file `target` refers to under a different spelling of
                    # its case, and a bare inequality would count it as its
                    # own sibling — silently defeating the one safety check
                    # this, the only destructive verb here, has.
                    siblings = [
                        (p, sib)
                        for p in _cache_candidates(cfg.cache_dir, info.bundle_id)
                        if not _is_same_file(p, target)
                        and (sib := memo.info_for(p)) is not None
                        and sib.bundle_id == info.bundle_id
                    ]
                    # Per app, not per bundle id: a sibling only rescues an
                    # app whose pin admits its version. `preferred_version ==
                    # "latest"` accepts any sibling; a pinned app needs one
                    # whose version matches exactly, the same test
                    # `find_in_cache` applies before it will open a file.
                    broken = [
                        name for name in claimed_by
                        if not any(
                            cfg.apps[name].preferred_version == "latest"
                            or sib.version == cfg.apps[name].preferred_version
                            for _, sib in siblings
                        )
                    ]
                    if broken:
                        # Emitted before the raise, so a machine consumer gets
                        # the app list as data rather than recovering it from
                        # the prose below. `_do_delete_tweak`'s own comment
                        # records that scraping "is the only staged IPA for:
                        # ..." out of stderr was a shipped defect once
                        # already.
                        if emitter:
                            emitter.emit(
                                "staged-ipa-in-use", filename=filename, used_by=broken,
                            )
                        raise StagedIPAInUseError(
                            f"{filename} is the only staged IPA for: {', '.join(broken)}",
                            hint=(
                                "deleting it leaves nothing for the next build to use — "
                                "dump the app again first, or delete it anyway with --force"
                            ),
                        )
        finally:
            memo.save()

    sidecar = _sidecar_path(target)
    also_removed = [sidecar.name] if sidecar.exists() else []
    # Translated the same way `_do_delete_tweak` translates `move_to_trash`'s
    # OSError: untranslated, a permissions failure here would still surface as
    # *some* `GraftError` further up the stack (`_unexpected`'s generic
    # fallback), but as a bare exception class name instead of a sentence
    # naming `cache_dir`.
    try:
        target.unlink()
    except OSError as exc:
        raise GraftError(
            f"couldn't delete {filename}: {exc}",
            hint=f"check permissions on {cfg.cache_dir}",
        ) from exc
    if also_removed:
        try:
            clear_stripped(target)
        except OSError as exc:
            # The IPA is already gone at this point — a partial success, not
            # a failure to report as if nothing happened. The message says so
            # rather than leaving the sidecar failure to read as "the delete
            # didn't happen."
            raise GraftError(
                f"deleted {filename} but couldn't remove its {sidecar.name} "
                f"sidecar: {exc}",
                hint=f"check permissions on {cfg.cache_dir}",
            ) from exc

    if emitter:
        emitter.emit(
            "staged-ipa-deleted", filename=filename, also_removed=also_removed
        )
        return
    console.print(f"[green]✓[/green] deleted {escape(filename)}")
    for name in also_removed:
        console.print(f"[dim]  and {escape(name)}[/dim]")


#: PlayCover's bundle identifier. Verified against the installed app
#: (`mdls -name kMDItemCFBundleIdentifier /Applications/PlayCover.app`), and it
#: is how this hands a file over: PlayCover registers with LaunchServices as a
#: handler for `com.apple.itunes.ipa` and has no CLI of its own. The
#: `--preserve-metadata=entitlements` string in its binary is an argument it
#: passes to `codesign`, not a flag it accepts.
PLAYCOVER_BUNDLE_ID = "io.playcover.PlayCover"


#: Where PlayCover keeps its per-app settings, and the two keys that decide
#: whether a handed-over app is usable at all.
#:
#: **This is the coupling the design doc's §6 refused**, and taking it on is a
#: deliberate reversal recorded there. §6's reasoning still holds — this is an
#: undocumented layout inside another application's container, and it can move
#: without warning. What changed is the cost of *not* doing it, measured
#: 2026-08-18: both keys default to the value that breaks the app, so a
#: handoff that "succeeds" produces something that crashes on its first
#: keychain write and, once that is fixed, ignores every click. A tool that
#: hands you a broken app and says ✓ is worse than one that breaks visibly
#: when PlayCover next moves this file.
#:
#: Hence: never fatal. Every failure here leaves the handoff successful,
#: because the handoff *was*.
PLAYCOVER_SETTINGS_DIR = (
    Path.home() / "Library/Containers/io.playcover.PlayCover/App Settings"
)

#: `playChain` crashes the app in PlayTools' keychain shim the first time it
#: writes a credential; `keymapping` translates the mouse into mapped touch, so
#: on a non-game every click silently misses. Both default to True.
PLAYCOVER_SETTINGS_TO_DISABLE = ("playChain", "keymapping")


def _tame_playcover_settings(bundle_id: str, *, timeout: float = 25.0) -> str | None:
    """Turn off the two PlayCover settings that make a handed-over app unusable.

    Returns a short note on what happened, or None if there was nothing to do.
    **Never raises** — see `PLAYCOVER_SETTINGS_DIR`.

    Polls rather than writing immediately: the plist does not exist at handoff
    time. `open -b` returns once LaunchServices delivers the event, and
    PlayCover writes these settings somewhere inside the install that has not
    happened yet, so a write here races the file into existence and loses.

    The file is also rewritten on *re*install, which is why this runs on every
    handoff instead of only the first: an earlier manual fix appeared to work,
    then a reinstall put `playChain` back to true three seconds before the
    crash it was supposed to prevent.
    """
    # No settings directory at all means PlayCover has never installed
    # anything here, so there is nothing to wait *for*. Checked first because
    # otherwise every caller without PlayCover — every test among them — pays
    # the full timeout to learn the same thing: the suite went from 17s to 42s
    # before this guard existed.
    if not PLAYCOVER_SETTINGS_DIR.is_dir():
        return None

    path = PLAYCOVER_SETTINGS_DIR / f"{bundle_id}.plist"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            break
        time.sleep(0.5)
    else:
        return None

    try:
        with path.open("rb") as fh:
            settings = plistlib.load(fh)
        changed = [k for k in PLAYCOVER_SETTINGS_TO_DISABLE if settings.get(k)]
        if not changed:
            return None
        for key in changed:
            settings[key] = False
        with path.open("wb") as fh:
            plistlib.dump(settings, fh)
        return ", ".join(changed)
    except (OSError, plistlib.InvalidFileException, AttributeError, TypeError):
        # A layout that moved, a file being rewritten under us, permissions.
        # None of it makes the handoff itself untrue.
        return None



def _prepare_for_playcover(tweaked: Path, emitter: JsonEmitter | None) -> list[str]:
    """Make `tweaked` loadable on this Mac, translating the one error it raises.

    Extracted from `_build_one` so it can be tested at all: inline, the whole
    branch — including this translation — was a mutation the suite could not
    detect, and it is the only thing between a stub-build failure and a raw
    traceback after minutes of downloading, decrypting and injecting.
    """
    try:
        return playcover.prepare_ipa(tweaked)
    except playcover.PlayCoverPrepError as exc:
        raise GraftError(
            f"couldn't prepare {tweaked.name} for PlayCover: {exc.message}",
            hint=exc.hint,
        ) from exc


def _do_install_to_mac(
    cfg: Config, *, filename: str, emitter: JsonEmitter | None = None
) -> None:
    """Hand one staged IPA to PlayCover to install on this Mac.

    A handoff, deliberately. PlayCover strips the developer signature and
    re-signs ad-hoc, and replaces the IPA's iOS entitlements with macOS sandbox
    ones — measured: a raw `Payload/X.app` carries `TeamIdentifier=ABCDE12345`
    and `application-identifier`, while PlayCover's output is adhoc-signed with
    `com.apple.security.app-sandbox`. Moving a bundle ourselves would hand
    macOS a signature it will not honour and entitlements it cannot grant, so
    the only alternative to this handoff is reimplementing both — a second,
    worse copy of the one thing PlayCover exists to do.

    Nothing here narrates the install. `open` returns once the event is
    delivered, so there is no completion status to report and no progress to
    mirror; PlayCover shows its own.
    """
    if Path(filename).name != filename:
        raise ConfigError(
            f"{filename!r} is not a filename",
            hint="pass the name of a file in the cache, not a path",
        )
    # Two places, tried in order. A staged dump lives in `cache_dir`; a build
    # made with `--for-playcover` lives in `built_dir`, and that is the one
    # worth handing over when you want the *tweaked* app on this Mac. Without
    # the second, the only artifact this verb could reach was the untweaked
    # one, which is a strange gap for a feature whose whole point is running
    # your apps here.
    #
    # cache_dir first, because it is what the Cache pane lists and so what a
    # bare filename most likely means. Built filenames carry a timestamp
    # (`example_21.16.2_20260818T134203.ipa`), so a collision between the two
    # is not a real shape.
    target = cfg.cache_dir / filename
    if not target.is_file():
        built = cfg.built_dir / filename
        if built.is_file():
            target = built
    if not target.is_file():
        raise ConfigError(
            f"no IPA named {filename!r} in {cfg.cache_dir} or {cfg.built_dir}",
            hint="run --staged-ipas to see what's in the cache",
        )

    info = read_info_plist(target)
    result = subprocess.run(
        ["open", "-b", PLAYCOVER_BUNDLE_ID, str(target)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Named, not passed through. Measured — `open -b` on an unregistered
        # bundle id says "LSCopyApplicationURLsForBundleIdentifier() failed
        # while trying to determine the application with bundle identifier
        # <id>.", not the friendlier sentence an earlier draft here invented —
        # true and useless either way to somebody who never typed `open`.
        #
        # Only two states are reachable through this exit code, and the
        # message says exactly those two and no more: not installed (that
        # message above, exit 1), or installed but unable to launch (its own
        # distinct stderr — e.g. "...its executable is missing.", also exit
        # 1). A *third* wording, "refused the file", would claim PlayCover
        # opened this IPA and rejected it — but `open -b` does not consult
        # document types, so that refusal is not something a non-zero exit
        # can ever mean, and PlayCover's own Info.plist declares `public.item`
        # at Default rank besides, so it was never reachable on type grounds
        # to begin with. See the design doc's §5 for why presence is inferred
        # from this exit rather than checked separately beforehand.
        raise ConfigError(
            "PlayCover is not installed, or could not launch",
            hint=(
                f"`open -b {PLAYCOVER_BUNDLE_ID}` exited {result.returncode}: "
                f"{result.stderr.strip() or 'no output'}"
            ),
        )

    # After the handoff, not before: the settings file does not exist until
    # PlayCover has started installing. Returns None whenever it could not or
    # need not act, and never raises — the handoff already succeeded.
    tamed = _tame_playcover_settings(info.bundle_id)

    if emitter:
        # Named for what happened, not for the verb that asked for it:
        # "installed-to-mac" would assert a finished install this app cannot
        # observe (see this function's own docstring), and the GUI renders off
        # this string, not off a comment.
        #
        # `settings_disabled` is reported the same way: it names the keys this
        # actually turned off, and is absent when it did not, rather than
        # asserting the app is now usable.
        payload = {"filename": filename, "path": str(target),
                   "bundle_id": info.bundle_id}
        if tamed:
            payload["settings_disabled"] = tamed
        emitter.emit("handed-to-playcover", **payload)
        return
    console.print(
        f"[green]✓[/green] handed {escape(filename)} to PlayCover — "
        "it reports its own progress"
    )
    if tamed:
        console.print(f"[dim]  turned off PlayCover's {escape(tamed)} for this app[/dim]")


def _do_attach_tweak(
    cfg: Config,
    *,
    app_name: str,
    filename: str,
    emitter: JsonEmitter | None = None,
) -> None:
    app = cfg.get_app(app_name)  # raises ConfigError if unknown
    src = cfg.tweaks_dir / filename
    if not src.is_file():
        raise TweakNotFoundError(
            f"tweak {filename!r} not found in {cfg.tweaks_dir}",
            hint="download it first via the Repos or Canister tab",
        )
    # Identity, not string equality — the same identity detach, the in-use
    # guard and `--tweaks` all key on. An exact-string check sees a path-form
    # entry (/tweaks/some-tweak.deb) or a mis-cased one (SOME-TWEAK.deb) as a different tweak
    # and appends a SECOND entry for the same file: cyan then gets
    # `-f <path> <path>`, the delivery badge reports the tweak as listed more
    # than once, and detach — which correctly drops every spelling — removes
    # both rows when the user clicks either.
    if _lists_tweak(cfg, app, filename):
        new_tweaks = list(app.tweaks)
    else:
        new_tweaks = [*app.tweaks, filename]
    set_app_tweaks(cfg.path, app_name, new_tweaks)
    if emitter:
        emitter.emit(
            "tweak-attached",
            app=app_name,
            filename=filename,
            tweaks=new_tweaks,
        )
        return
    console.print(
        f"[green]✓[/green] attached [bold]{filename}[/bold] to {app_name}  "
        f"({len(new_tweaks)} tweak(s) total)"
    )


def _do_detach_tweak(
    cfg: Config,
    *,
    app_name: str,
    filename: str,
    emitter: JsonEmitter | None = None,
) -> None:
    app = cfg.get_app(app_name)  # raises ConfigError if unknown
    # Identity, not string equality — the same identity the delete guard uses.
    # An exact-string filter can't remove a path-form entry
    # (/tweaks/some-tweak.deb) or a mis-cased one (SOME-TWEAK.deb) when the user detaches by
    # the bare filename --tweaks showed them, so detach would report success,
    # change nothing, and leave --delete-tweak's "detach it from those apps
    # first" hint permanently unsatisfiable. Note this drops *every* spelling
    # of the same file, so a config listing it twice loses both entries.
    key = cfg.tweak_key(filename)
    new_tweaks = [t for t in app.tweaks if cfg.tweak_key(t) != key]
    if new_tweaks != app.tweaks:
        set_app_tweaks(cfg.path, app_name, new_tweaks)
    if emitter:
        emitter.emit(
            "tweak-detached",
            app=app_name,
            filename=filename,
            tweaks=new_tweaks,
        )
        return
    console.print(
        f"[green]✓[/green] detached [bold]{filename}[/bold] from {app_name}  "
        f"({len(new_tweaks)} tweak(s) total)"
    )


def _do_delete_tweak(
    cfg: Config,
    *,
    filename: str,
    emitter: JsonEmitter | None = None,
    trash_dir: Path | None = None,
) -> None:
    """Delete a tweak from tweaks_dir, refusing while any app still uses it.

    `trash_dir` is injectable so tests don't touch the real ~/.Trash.
    """
    if not filename or Path(filename).name != filename:
        # Path.__truediv__ rejects neither ".." nor absolute operands, so
        # without this guard "../../../victim.txt" or "/etc/passwd" would
        # resolve outside tweaks_dir entirely and get moved to the Trash.
        raise TweakNotFoundError(
            f"tweak {filename!r} is not a filename in {cfg.tweaks_dir}",
            hint="pass just the .deb filename, as shown by --tweaks",
        )
    target = cfg.tweaks_dir / filename
    # Same rule _collect_tweak_records uses to decide what --tweaks shows: a
    # real file, not a dotfile. Anything --tweaks wouldn't list (a directory,
    # a dangling symlink, .DS_Store) is rejected here too, for the same
    # "not found" reason a nonexistent name would be.
    if filename.startswith(".") or not target.is_file():
        raise TweakNotFoundError(
            f"tweak {filename!r} not found in {cfg.tweaks_dir}",
            hint="run --tweaks to see what's in the tweaks dir",
        )
    # cfg.tweak_key resolves the request and every config entry to the same
    # identity, so a mis-cased request ("A-TWEAK.DEB" on case-insensitive
    # APFS), a path-form config entry (/tweaks/some-tweak.deb) and a symlink all match
    # the one real file. An exact-string check missed all three and deleted
    # tweaks that apps still referenced.
    #
    # _fatal only catches GraftError, so an untranslated OSError escapes
    # as a traceback — and under --json the GUI gets no failure event at all,
    # just a dead process. Path.is_file() above swallows OSError, but stat'ing
    # the target and every configured tweak does not, so translate here too.
    try:
        attached_to = sorted(_tweak_consumers(cfg).get(cfg.tweak_key(filename), []))
    except OSError as exc:
        raise GraftError(
            f"couldn't determine whether {filename} is still in use: {exc}",
            hint=f"check permissions on {cfg.tweaks_dir}",
        ) from exc
    if attached_to:
        # Emitted before the raise, so a machine consumer gets the app list as
        # data rather than recovering it from the prose below. The GUI offers
        # "Detach and Delete" off exactly this list, and was scraping it out of
        # stderr — the one place it depended on an English sentence, with none
        # of the contract checking every other CLI/GUI coupling has. Rewording
        # the message would have silently degraded that button while every
        # test kept passing, because the fakes carry the sentence too.
        if emitter:
            emitter.emit("tweak-in-use", filename=filename, used_by=attached_to)
        raise TweakInUseError(
            f"{filename} is attached to: {', '.join(attached_to)}",
            hint="detach it from those apps first",
        )
    # move_to_trash lets OSError through too (permissions, ENOSPC,
    # cross-device), for the same reason and with the same consequence.
    try:
        dest = move_to_trash(target, trash_dir=trash_dir)
    except OSError as exc:
        raise GraftError(
            f"couldn't move {filename} to the Trash: {exc}",
            hint="check permissions on ~/.Trash",
        ) from exc
    if emitter:
        emitter.emit(
            "tweak-deleted",
            filename=filename,
            trash_path=str(dest),
        )
        return
    console.print(
        f"[green]✓[/green] moved [bold]{filename}[/bold] to the Trash  → {dest}"
    )


def _do_remove_app(
    cfg: Config,
    *,
    app_name: str,
    emitter: JsonEmitter | None = None,
) -> None:
    """Remove an app from config. Cached IPAs and build history are kept."""
    app = cfg.get_app(app_name)  # raises ConfigError if unknown
    remove_app(cfg.path, app_name)
    if emitter:
        emitter.emit("app-removed", name=app_name, bundle_id=app.bundle_id)
        return
    console.print(
        f"[green]✓[/green] removed [bold]{app_name}[/bold] from config  "
        "(cached IPAs and build history kept)"
    )


def _do_set_app_options(
    cfg: Config,
    *,
    app_name: str,
    preferred_version: str | None = None,
    display_name: str | None = None,
    keep_bundle_id: bool | None = None,
    strip_extensions: bool | None = None,
    strip_supported_devices: bool | None = None,
    compression_level: int | None = None,
    delivery: str | None = None,
    emitter: JsonEmitter | None = None,
) -> None:
    """Patch an app's options. Only the fields actually passed are written."""
    cfg.get_app(app_name)  # raises ConfigError if unknown

    # Keyed by the contract's field names, so `changed` (and therefore the
    # `app-updated` event, and therefore --describe's app_option_fields) is
    # derived from one place instead of a parallel list. The equality check
    # below fires the moment a parameter is added here without being
    # registered — or registered without being handled here.
    supplied: dict[str, Any] = {
        "preferred_version": preferred_version,
        "display_name": display_name,
        "delivery": delivery,
        "keep_bundle_id": keep_bundle_id,
        "strip_extensions": strip_extensions,
        "strip_supported_devices": strip_supported_devices,
        "compression_level": compression_level,
    }
    if set(supplied) != APP_OPTION_FIELDS:
        raise ValueError(
            "app option fields out of sync with APP_OPTION_FIELDS: "
            f"{sorted(set(supplied) ^ APP_OPTION_FIELDS)}"
        )

    if compression_level is not None and not 0 <= compression_level <= 9:
        raise ConfigError("--compression-level must be between 0 and 9")

    # `is not None`, not truthiness: an explicit `--display-name ""` must still
    # count as changed even though set_app_options() responds to that empty
    # value by deleting the display_name key rather than writing "". The CLI
    # DID change something — Swift's request/response reconciliation in
    # ConfigStore.updateAppOptions throws if a field it asked for isn't echoed
    # back in `changed`.
    changed: dict[str, Any] = {
        name: value for name, value in supplied.items() if value is not None
    }
    if not changed:
        raise ConfigError(
            "--set-app-options requires at least one field to change",
            hint="e.g. --preferred-version, --display-name, --compression-level",
        )

    # Route each changed field by where it lives on disk instead of restating
    # the top-level ones by hand. Both halves now come from the same `changed`
    # dict, so a new field is one edit here (parameter + `supplied` entry) plus
    # one to the matching frozenset — no third site that can silently omit it
    # and report success for a value that was never written. A top-level field
    # that set_app_options() doesn't accept is a TypeError at this call, which
    # is exactly the drift the old hand-written pass-through hid.
    cyan_updates = {n: v for n, v in changed.items() if n in CYAN_OPTION_FIELDS}
    top_level_updates = {n: v for n, v in changed.items() if n not in CYAN_OPTION_FIELDS}

    set_app_options(
        cfg.path,
        app_name,
        cyan=cyan_updates or None,
        **top_level_updates,
    )
    if emitter:
        emitter.emit("app-updated", name=app_name, changed=sorted(changed))
        return
    console.print(
        f"[green]✓[/green] updated [bold]{app_name}[/bold]  ({', '.join(sorted(changed))})"
    )


def _do_repos(cfg: Config, emitter: JsonEmitter | None = None) -> None:
    rows: list[dict] = []
    for cr in cfg.repos:
        try:
            cache_url = repo.normalize_url(cr.url)
        except repo.RepoError:
            cache_url = cr.url
        cache = repo.cache_path(cfg.state_dir, cache_url)
        count = 0
        last_refreshed: str | None = None
        if cache.exists():
            last_refreshed = datetime.fromtimestamp(
                cache.stat().st_mtime, tz=UTC
            ).isoformat(timespec="seconds")
            try:
                count = len(repo.parse_packages(cache.read_text()))
            except (OSError, ValueError):
                count = 0
        rows.append(
            {
                "url": cr.url,
                "name": cr.name,
                "package_count": count,
                "last_refreshed": last_refreshed,
            }
        )
    if emitter:
        for row in rows:
            # origin is not persisted in config.yaml's repos: entries — only
            # --add-repo (a live fetch) knows it; listing from cache reports None.
            emitter.emit("repo", origin=None, **row)
        emitter.emit("repos-end", count=len(rows))
        return
    if not rows:
        console.print("[yellow]no repos configured[/yellow] — add one with --add-repo <url>")
        return
    table = Table(title=f"configured repos ({len(rows)})")
    table.add_column("name", style="bold")
    table.add_column("packages", justify="right")
    table.add_column("last refreshed")
    table.add_column("url", overflow="fold")
    for row in rows:
        table.add_row(
            row["name"],
            str(row["package_count"]),
            row["last_refreshed"] or "[dim]never[/dim]",
            row["url"],
        )
    console.print(table)


def _do_repo_packages(
    cfg: Config,
    url: str,
    refresh: bool,
    emitter: JsonEmitter | None = None,
) -> None:
    norm = repo.normalize_url(url)
    cache = repo.cache_path(cfg.state_dir, norm)
    if refresh or not cache.exists():
        if emitter:
            emitter.emit("repo-refresh-start", url=norm)
        text = repo.fetch_packages(norm)
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(text)
        except OSError as e:
            raise repo.RepoError(
                f"failed to cache package index for {norm}: {e}"
            ) from e
    else:
        try:
            text = cache.read_text()
        except OSError as e:
            raise repo.RepoError(
                f"failed to read cached index for {norm}: {e}"
            ) from e
    # Drop packages with no Filename — they have no downloadable .deb, and
    # repo.download_url would raise on them mid-stream.
    pkgs = [p for p in repo.parse_packages(text) if p.filename]
    if emitter:
        for p in pkgs:
            emitter.emit(
                "repo-package",
                package_id=p.package_id,
                name=p.name,
                version=p.version,
                description=p.description,
                author=p.author,
                section=p.section,
                filename=p.filename,
                size_bytes=p.size,
                sha256=p.sha256,
                depiction=p.depiction,
                download_url=repo.download_url(norm, p),
            )
        emitter.emit("repo-packages-end", url=norm, count=len(pkgs))
        return
    if not pkgs:
        console.print(f"[yellow]no packages found in[/yellow] {norm}")
        return
    table = Table(title=f"{norm} — {len(pkgs)} package(s)")
    table.add_column("package id", style="cyan", no_wrap=True)
    table.add_column("name", style="bold", overflow="fold", max_width=28)
    table.add_column("version", no_wrap=True)
    table.add_column("section")
    for p in pkgs:
        table.add_row(p.package_id, p.name, p.version, p.section)
    console.print(table)
    console.print(
        "[dim]→ graft --fetch <url>  downloads a .deb into your tweaks dir[/dim]"
    )


def _emit_log_lines(emitter: JsonEmitter | None, source: str, text: str | None) -> None:
    if not emitter or not text:
        return
    for line in text.splitlines():
        if line.strip():
            emitter.emit("log", source=source, line=line)


def _extensions_to_strip(still_encrypted: list, extract_dir: Path) -> set[Path]:
    """Which `.appex` bundles to remove, or an empty set meaning "do not strip".

    Empty in two cases, and the second is the one that matters:

      - nothing left encrypted is in an extension at all; there is nothing to
        strip and the dump simply failed.
      - *something* left encrypted is outside one. Then the app's own binary or
        one of its frameworks is still encrypted, which is a failed dump rather
        than the structural extension limit — and deleting extensions to make
        the check pass would hand back an IPA that signs, installs, and does not
        run. Refusing here is what keeps `--strip-encrypted-extensions` from
        being a flag that turns a loud failure into a quiet one.

    Whole bundles, not individual binaries: an extension missing its executable
    is one iOS still tries to load. If any binary inside an `.appex` could not
    be decrypted, that whole extension goes.
    """
    doomed: set[Path] = set()
    for c in still_encrypted:
        appex = appex_of(c.binary, extract_dir)
        if appex is None:
            return set()
        doomed.add(appex)
    return doomed


def _still_encrypted_hint(
    paths: list[str], backend: str, *, stripped_attempted: bool = False
) -> str:
    """What to actually do when a dump comes back with encrypted binaries.

    The two backends fail this check for opposite reasons, and the advice for
    one is harmful for the other — which the single hint this replaces got
    wrong: it named bagbak whatever had run, and said "force-quit and retry".

    Measured against SmartLife (11 extensions) on the real device:
    frida-ios-dump returned 36 binaries with exactly 11 still encrypted, and
    every one of them was a `PlugIns/*.appex`. That is structural, not a race.
    `dump.js` decrypts what `Process.enumerateModulesSync()` reports — modules
    loaded into the *host app's* process — and an app extension runs in its own
    process, so it is never enumerated and is copied from disk untouched.
    Retrying is therefore guaranteed to produce the identical result, and
    force-quitting makes it strictly worse: with no live pid frida-ios-dump
    spawns the app instead of attaching, which loses the main binary too.

    bagbak's version of this failure *is* a race — it attaches to each
    extension process, and iOS reaps short-lived ones mid-dump — so there the
    retry advice is right.
    """
    extensions_only = paths and all(".appex/" in p for p in paths)
    if stripped_attempted:
        # Extensions were removed and something is *still* encrypted, so the
        # leftovers are the app's own binaries — a failed dump, not the
        # structural extension limit, and no amount of stripping fixes it.
        return (
            "these are outside any app extension, so stripping cannot help: the dump "
            "itself came back incomplete. Keep the phone unlocked and the app "
            "foreground-active and retry."
        )
    if extensions_only and not stripped_attempted:
        # Only reachable when the user asked to keep them: a dump drops
        # undumpable extensions and stages the rest by default, so reaching here
        # with every leftover inside an `.appex` means that trade was refused.
        return (
            "these are all app extensions, and you asked to keep them. Without "
            "`--keep-encrypted-extensions` they are dropped and the rest is staged, "
            "which is usually what you want for a tweaked build — the app keeps "
            "working, minus its widgets and share sheet. "
            + (
                "frida-ios-dump cannot decrypt extensions at all (they run in their own "
                "processes), so retrying will not change this."
                if backend == decrypt_runner.FRIDA
                else "bagbak can dump extensions, but not ones whose MinimumOSVersion is "
                "newer than the device's iOS — those can't launch, so they can't be read."
            )
        )
    if backend == decrypt_runner.FRIDA and extensions_only:
        return (
            "frida-ios-dump only decrypts what is loaded into the app's own process, "
            "and app extensions run in their own — so it cannot dump them, and "
            "retrying will not change that. Use `--decrypt-backend bagbak`, which "
            "attaches to each extension process (and is why it is the flakier one)."
        )
    if backend == decrypt_runner.FRIDA:
        return (
            "frida-ios-dump decrypts only the modules loaded into the app's process. "
            "Make sure the app is foreground-active and exercised enough to have "
            "loaded these, or use `--decrypt-backend bagbak`."
        )
    return (
        "the app was running while dumped, or bagbak couldn't decrypt an extension "
        "— force-quit it on the phone and retry; iOS reaps short-lived extension "
        "processes mid-dump and some apps' extensions resist bagbak entirely"
    )


def _do_decrypt_check(cfg: Config, *, device: str | None, emitter: JsonEmitter | None) -> None:
    """Report whether a dump could run right now, and what is blocking it.

    Read-only and quick by design. It exists because every one of a dump's
    preconditions used to be discovered by starting one and waiting thirty
    seconds for "Timeout was reached" — which named none of them.
    """
    from . import decrypt_ready
    from . import devices as devices_mod

    udid = device or cfg.default_device_udid
    name = None
    try:
        for d in devices_mod.list_devices().devices:
            if udid is None or d.udid == udid:
                udid, name = d.udid, d.name
                break
    except Exception:
        pass

    report = decrypt_ready.check(udid, name)

    if emitter:
        emitter.emit(
            "decrypt-check",
            device=report.device, device_name=report.device_name,
            ready=report.ready,
            can_dump_from_list=report.can_dump_from_list,
            recommended_backend=report.recommended_backend,
            frontmost_bundle_id=report.frontmost[0] if report.frontmost else None,
            frontmost_name=report.frontmost[1] if report.frontmost else None,
            checks=[
                {"key": c.key, "label": c.label, "ok": c.ok, "detail": c.detail,
                 "fix": c.fix, "blocking": c.blocking}
                for c in report.checks
            ],
            backends=[
                {"name": b.name, "usable": b.usable, "reason": b.reason, "note": b.note}
                for b in report.backends
            ],
        )
        return

    console.print(f"[bold]{escape(report.device_name or report.device or 'device')}[/bold]")
    for c in report.checks:
        mark = ("[green]OK[/green]  " if c.ok
                else ("[red]FAIL[/red]" if c.blocking else "[yellow]note[/yellow]"))
        console.print(f"  {mark} {escape(c.label)}: {escape(c.detail)}")
        if c.fix:
            console.print(f"        [dim]{escape(c.fix)}[/dim]")
    for b in report.backends:
        mark = "[green]OK[/green]  " if b.usable else "[yellow]no[/yellow]  "
        console.print(f"  {mark} {escape(b.name)}"
                      + (f" [dim]— {escape(b.reason)}[/dim]" if b.reason else ""))
        if b.note:
            console.print(f"        [dim]{escape(b.note)}[/dim]")
    if report.ready:
        console.print(
            f"[green]✓[/green] ready — `--decrypt-frontmost` will dump "
            f"{escape(report.frontmost[1])} with {escape(report.recommended_backend or '?')}"
        )
    elif report.can_dump_from_list:
        # Nothing is blocking; the *reliable* route just is not available. Saying
        # "fix the FAIL lines" here was wrong twice over — there were none, and
        # it implied the feature was unavailable when any app could still be
        # dumped by letting the backend launch it.
        console.print(
            "[yellow]![/yellow] no app on screen — open one to dump it by attaching, "
            "or name an app and it will be launched for you"
        )
    else:
        console.print("[yellow]![/yellow] not ready — fix the FAIL lines above")


def _do_set_ssh_password(cfg: Config, *, device: str | None) -> None:
    """Store a device's root SSH password in the Keychain, for frida-ios-dump.

    Per device, because the password belongs to the phone. `--device` picks
    which; otherwise the configured default, and failing that the sole
    connected device — asking for a UDID the user would have to look up when
    only one phone is attached is a step for nothing.

    Prompted rather than taken as a flag value, for the reason every tool does
    this: an argument lands in shell history and in `ps` for anyone on the box.
    A piped stdin is still accepted so the GUI (and a test) can drive it.
    """
    from . import devices as devices_mod
    from . import keychain

    udid = device or cfg.default_device_udid
    if not udid:
        found = devices_mod.list_devices().devices
        if len(found) == 1:
            udid = found[0].udid
        elif not found:
            raise _fatal(GraftError(
                "no device connected",
                hint="plug the phone in (or pass --device <udid>)",
            ))
        else:
            names = ", ".join(f"{d.name} ({d.udid})" for d in found)
            raise _fatal(GraftError(
                "several devices are connected, so which one this password belongs "
                "to is ambiguous",
                hint=f"pass --device <udid>: {names}",
            ))

    if sys.stdin.isatty():
        import getpass
        password = getpass.getpass(f"Enter the root SSH password for {udid}: ")
    else:
        password = sys.stdin.readline().rstrip("\n")
    if not password:
        raise _fatal(GraftError("no password provided"))

    try:
        keychain.write_ssh_password(udid, password)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = (
            e.stderr.decode(errors="replace").strip()
            if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
        )
        raise _fatal(GraftError(f"failed to store password in Keychain: {detail}")) from e
    console.print(
        f"[green]✓[/green] stored the root SSH password for {escape(udid)} in the "
        f"Keychain (service={keychain.SERVICE}, account=ssh-{escape(udid)})"
    )


def _do_set_p12_password(cfg: Config) -> None:
    from . import keychain
    if not cfg.signing.team_id:
        raise _fatal(GraftError("signing.team_id is not configured", hint="run `graft --init`"))

    if sys.stdin.isatty():
        import getpass
        password = getpass.getpass(f"Enter .p12 password for team {cfg.signing.team_id}: ")
    else:
        # Stdin pipe — read one line, strip the trailing newline only.
        password = sys.stdin.readline().rstrip("\n")

    if not password:
        raise _fatal(GraftError("no password provided"))

    try:
        keychain.write_p12_password(cfg.signing.team_id, password)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = e.stderr.decode(errors="replace").strip() if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
        raise _fatal(GraftError(f"failed to store password in Keychain: {detail}")) from e
    console.print(f"[green]✓[/green] stored .p12 password in Keychain (service={keychain.SERVICE}, account=p12-{cfg.signing.team_id})")


def _do_describe(ctx: typer.Context) -> None:
    """Emit the CLI↔GUI vocabulary as a single `cli-contract` JSON line.

    The GUI duplicates flag names, event names, and app-option field names as
    Swift string literals; its CLIContractTests asserts each of its sets is a
    subset of what this reports. Everything here is derived — flags from
    Click's own parameter list, events from EVENT_TYPES — so the contract
    can't claim support for something the CLI doesn't actually accept.

    `cli_only_events` is the other half of the event contract and the reason
    `device-error` could hide: a subset check alone says nothing about the
    events the CLI emits that *nothing* decodes. Publishing the CLI's own
    declaration of which events are deliberately not for the GUI lets the Swift
    side assert the complement — `events - cli_only ⊆ eventsHandled` — against
    the installed binary, which is the direction that finds a dropped event.
    """
    flags = sorted(
        {
            opt
            # ctx.command is the Typer group whose params are this callback's;
            # secondary_opts carries the `--no-x` half of a `--x/--no-x` pair.
            for param in ctx.command.params
            for opt in (*param.opts, *param.secondary_opts)
            # Positional arguments (`apps`) declare a bare name, not a flag.
            if opt.startswith("-")
        }
    )
    JsonEmitter().emit(
        "cli-contract",
        flags=flags,
        events=sorted(EVENT_TYPES),
        setting_fields=sorted(SETTINGS),
        # The Swift side maintains its own `cliOnlyEvents` and requires
        # `events - cliOnlyEvents ⊆ eventsHandled`. Publishing this lets it
        # check that its copy still matches the CLI's, against the installed
        # binary rather than a source file it was built beside.
        cli_only_events=sorted(CLI_ONLY_EVENTS),
        # The `stage` event's `name` field, which is a vocabulary of its own
        # inside an already-checked one. `events` proves the GUI recognises a
        # `stage` line; only this proves the *name* it reads for meaning —
        # `CLIContract.Stage.decryptionCheck` — is still one the CLI puts on
        # the wire. The Python suite pins this set to exactly the names emitted
        # by this package, so a subset check on the Swift side is a statement
        # about what the installed CLI really emits, not about what it permits.
        stages=sorted(STAGE_NAMES),
        app_option_fields=sorted(APP_OPTION_FIELDS),
    )


def _fatal(e: GraftError) -> typer.Exit:
    """The single rendering of a fatal error. Escaped, for the same reason as `_warn`.

    This one is worse than a cosmetic slip: `_reported` calls it from its own
    `except` clause, so a MarkupError here replaces the legible message with
    the traceback that `_reported` exists to prevent — and `GraftError`
    messages routinely carry subprocess stderr and file paths. `markup=False`
    would work too but would print the literal `[red]` tags; escaping the
    interpolated span keeps the colour and makes only the untrusted part inert.
    """
    err_console.print(f"[red]error:[/red] {escape(e.message)}")
    if e.hint:
        err_console.print(f"  [yellow]hint:[/yellow] {escape(e.hint)}")
    return typer.Exit(code=e.exit_code)


def _unexpected(e: Exception) -> GraftError:
    """An exception this CLI has no translation for, as a reportable error.

    Named after what it is rather than hidden behind a generic message: the
    class name is often the only diagnosis available (`MuxException`,
    `ModuleNotFoundError`) and the GUI shows this text verbatim.
    """
    return GraftError(
        f"{e.__class__.__name__}: {e}",
        hint=(
            "that isn't a failure graft knows how to explain — the line "
            "above is the raw error"
        ),
    )


def _hung_up(stream: TextIO) -> bool:
    """Is `stream` itself the dead pipe, or did EPIPE come from somewhere else?

    `BrokenPipeError` is raised for two errnos and no others: EPIPE, a pipe or
    FIFO whose reader is gone, and ESHUTDOWN, a socket shut down for writing.
    A write to a real file that fails for a real reason is a plain `OSError` —
    ENOSPC for a full disk, EACCES, EIO — so treating `BrokenPipeError` as "a
    reader hung up" can never quietly discard a failed write to a file. That
    much holds by which errno the class covers.

    The socket half is not, and is why this function exists. This package talks
    SSH to the phone, and "the device went away mid-push" can surface as EPIPE
    too — a genuine failure that has to keep the legible report `_unexpected`
    gives it. So the boundary does not ask "was this EPIPE?", it asks "is the
    stream I am about to silence the one that broke?", by trying to flush it
    again. Measured in this session, on CPython 3.13: after a flush fails with
    EPIPE the unwritten bytes are still sitting in the `BufferedWriter`, so the
    next flush attempts the same doomed write and raises the same error. A
    healthy stdout flushes fine and the caller falls through to the catch-all.
    """
    try:
        stream.flush()
    except BrokenPipeError:
        return True
    except Exception:
        # A stream that is unusable for some *other* reason (closed, detached)
        # is not evidence about the exception we were handed, and is certainly
        # not something to answer with a broken-pipe status.
        return False
    return False


def _silence(stream: TextIO) -> None:
    """Point `stream`'s file descriptor at /dev/null, leaving the object alone.

    This is the half of the defect no `except` clause could reach. After the
    handler returns, CPython flushes `sys.stdout` during interpreter shutdown;
    that flush hits the same dead pipe, and the interpreter prints "Exception
    ignored on flushing sys.stdout" and exits **120**, over the top of whatever
    status the handler chose. Measured both ways in this session: the same
    child that exits 120 with a second complaint on stderr exits with the code
    it asked for, silently, once this runs first.

    Replacing the descriptor rather than the Python object is what makes it
    work — the buffered bytes still exist and are still flushed, they just land
    somewhere that accepts them.
    """
    try:
        fd = stream.fileno()
    except (OSError, ValueError, AttributeError):
        # No descriptor, so nothing for the interpreter's final flush to fail
        # on either: under Click's `CliRunner` `sys.stdout` is a wrapper over
        # an in-memory buffer and raises `io.UnsupportedOperation` here, which
        # is both an `OSError` and a `ValueError`. `AttributeError` is not for
        # a case seen in this session — it is because this runs *inside* the
        # error boundary, where a `sys.stdout` replacement that isn't quite a
        # file must not turn a broken pipe into a crash in the handler.
        return
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


def _reported(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The CLI's single error boundary. Nothing `main` dispatches may escape it.

    Every verb used to carry its own `except GraftError: raise _fatal(e)`
    — twenty-four identical copies — and the two that didn't (`--devices` and
    `--builds`) were exactly the two that failed as a raw traceback on stderr.
    That mattered more than it looks: `CLIError.exited(stderr:)` uses stderr as
    its `errorDescription`, so an unreachable usbmuxd or a pymobiledevice3
    missing from the pipx venv rendered a multi-line Python traceback as the
    user-facing device error.

    Wrapping the callback instead of each verb means a verb added later cannot
    be the twenty-fifth: there is no per-verb handler left to forget. Errors
    the CLI *does* know about keep their exit codes (the GUI keys on 16 for
    "tweak still in use"); anything else keeps exit 1, the same code the
    traceback used to produce, with a legible message in its place.

    `typer.Exit` and `typer.Abort` subclass RuntimeError — every verb ends by
    raising the former — so they have to be let through ahead of the catch-all
    or `main` would never terminate normally.

    A hung-up reader gets its own arm ahead of the catch-all for the same
    reason the boundary is one decorator: `| head` is an ordinary thing to
    type, so it needs one answer, and putting that answer here is what stops it
    being twenty-four answers. It used to fall to `_unexpected`, which says
    "that isn't a failure graft knows how to explain" about a reader
    doing exactly what `head` is for.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (typer.Exit, typer.Abort):
            raise
        except GraftError as e:
            raise _fatal(e) from e
        except BrokenPipeError as e:
            # Both streams, because a hung-up *stderr* produces the same 120 by
            # itself: measured here, a verb that warns through `err_console`
            # into a dead pipe still exited 120 on the shutdown flush while
            # only stdout was being silenced. Probe both before silencing
            # either, so the verdict is about the pipes as the verb left them.
            #
            # One path this arm cannot reach, for the record: a `_fatal` whose
            # own write to stderr breaks. That is raised from inside the
            # sibling `except GraftError` clause above, and no clause of
            # the same `try` can catch it. Click's `main()` has its own EPIPE
            # fallback for exactly that shape — `PacifyFlushWrapper` over both
            # streams, then exit 1 — and it was measured doing so: clean and
            # traceback-free, just not this arm's 141.
            broken = [s for s in (sys.stdout, sys.stderr) if _hung_up(s)]
            if not broken:
                # EPIPE from something that is not our own output — an SSH
                # socket to the phone, say. That is a real failure and keeps
                # the report it had before this arm existed.
                raise _fatal(_unexpected(e)) from e
            for stream in broken:
                _silence(stream)
            # Nothing printed, on stdout or stderr. `git log | head` and
            # `yes | head` say nothing either, and a note here would be output
            # the user did not ask for, printed *because* they asked for less
            # of it. stderr is usually the same terminal, so under `| head` the
            # note would land on screen; and for a `--json` consumer that keeps
            # stderr for error reporting, a line there reads as an error when
            # the tool worked. The exit code carries everything a script needs.
            raise typer.Exit(code=EXIT_BROKEN_PIPE) from e
        except Exception as e:
            raise _fatal(_unexpected(e)) from e

    return wrapper


# ---------------------------------------------------------------------------
# The verbs
#
# One function per verb flag. Each takes the options `main` was given by
# keyword and swallows the rest, which is what makes them uniform enough to
# dispatch from a table instead of from a chain of forty-nine `if`s. Declaring
# the options it uses — with no defaults — is what keeps that uniformity honest:
# a handler that asks for an option this CLI does not have fails on the first
# invocation of that verb rather than quietly reading a default nobody set.
#
# A handler returns the process exit code, or None for 0. Only three have
# anything to say: `--check`, the build loop, and the usage error.
# ---------------------------------------------------------------------------


def _verb_describe(*, ctx: typer.Context, **_: Any) -> None:
    _do_describe(ctx)


def _verb_version(**_: Any) -> None:
    console.print(f"graft {__version__}")


def _verb_init(**_: Any) -> None:
    run_init(console)


def _verb_setup_portal(**_: Any) -> None:
    from .setup_portal import run_setup_portal

    run_setup_portal(console)


def _verb_set_p12_password(*, cfg: Config, **_: Any) -> None:
    _do_set_p12_password(cfg)


def _verb_decrypt_check(
    *, cfg: Config, device: str | None, json_mode: bool, **_: Any
) -> None:
    _do_decrypt_check(cfg, device=device, emitter=JsonEmitter() if json_mode else None)


def _verb_set_ssh_password(*, cfg: Config, device: str | None, **_: Any) -> None:
    _do_set_ssh_password(cfg, device=device)


def _verb_list_apps(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    # Hoisted above the loop (and above the json/human split) on purpose:
    # see _delivery_state_for. The GUI calls --list on every refresh, and
    # opening a BuildLog per app would migrate and write-lock the live
    # database once per app.
    delivery_log = _open_delivery_log(cfg)
    if json_mode:
        em = JsonEmitter()
        for name, a in sorted(cfg.apps.items()):
            em.emit(
                "app",
                name=name,
                bundle_id=a.bundle_id,
                preferred_version=a.preferred_version,
                tweaks=a.tweaks,
                display_name=a.cyan.display_name,
                delivery=a.delivery,
                auto_profile=a.auto_profile,
                keep_bundle_id=a.cyan.keep_bundle_id,
                strip_extensions=a.cyan.strip_extensions,
                strip_supported_devices=a.cyan.strip_supported_devices,
                compression_level=a.cyan.compression_level,
            )
            em.emit("delivery-state", **_delivery_state_for(cfg, name, a, delivery_log))
    else:
        _print_apps(
            cfg,
            {
                name: _delivery_state_for(cfg, name, a, delivery_log)
                for name, a in sorted(cfg.apps.items())
            },
        )


def _verb_list_tweaks(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    records = _collect_tweak_records(cfg)
    if json_mode:
        em = JsonEmitter()
        for r in records:
            em.emit("tweak", **r)
    else:
        _print_tweaks(records)


def _do_tweak_updates(cfg: Config, *, emitter: JsonEmitter | None = None) -> None:
    """Report, per library file, whether a newer version is published.

    Cached repo indexes answer first and cost nothing; canister.me is asked
    only for ids they don't carry. Every file gets an event, including the ones
    with no published version anywhere — a library is mostly things you built,
    and silence about them would read the same as "up to date".
    """
    records = _collect_tweak_records(cfg)
    results = updates.statuses(records, updates.repo_catalog(cfg))

    if emitter:
        for status in results:
            emitter.emit("tweak-update", **updates.as_event(status))
        emitter.emit(
            "tweak-updates-end",
            checked=len(results),
            outdated=sum(1 for s in results if s.update_available),
            unchecked=sum(1 for s in results if s.unchecked),
        )
        return

    outdated = [s for s in results if s.update_available]
    unchecked = [s for s in results if s.unchecked]
    if not outdated:
        if unchecked:
            # Never "all up to date" on the strength of lookups that failed.
            # Offline, every answer is None and `outdated` is 0 — which reads
            # identically to having checked and being sure.
            console.print(
                f"[yellow]![/yellow] {len(unchecked)} of {len(results)} tweak(s) "
                "could not be checked; the rest are up to date"
            )
        else:
            console.print(f"[green]✓[/green] all {len(results)} tweak(s) are up to date")
        return
    table = Table(title="updates available")
    table.add_column("tweak", style="bold")
    table.add_column("installed")
    table.add_column("latest", style="green")
    table.add_column("from", style="dim")
    for status in outdated:
        assert status.available is not None
        table.add_row(
            status.filename, status.installed or "?", status.available.version,
            status.available.source,
        )
    console.print(table)


def _verb_tweak_updates(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    _do_tweak_updates(cfg, emitter=JsonEmitter() if json_mode else None)


def _repoint(
    cfg: Config, apps: list[str], old_key: object, new_filename: str, done: list[str]
) -> None:
    """Point every app in `apps` at `new_filename` instead of the old tweak.

    Appends to `done` as it goes, so a caller whose write fails partway can
    report which apps were actually changed rather than describing the whole
    update as having not happened.

    Substitute first, then collapse by identity. Checking "is the new name
    already in the list I am building" only sees entries already emitted — so
    an app listing the new file *after* the old one (the user fetched it
    through Canister and attached it alongside) got the new name twice, and
    cyan was handed the same .deb twice. Deduping the finished list has no such
    ordering dependency, and keys on `tweak_key` so a path-form entry beside a
    bare one counts as one tweak: the identity rule `_lists_tweak` applies on
    attach.
    """
    for app_name in apps:
        app = cfg.get_app(app_name)
        substituted = [
            new_filename if cfg.tweak_key(entry) == old_key else entry
            for entry in app.tweaks
        ]
        seen: set = set()
        swapped: list[str] = []
        for entry in substituted:
            key = cfg.tweak_key(entry)
            if key in seen:
                continue
            seen.add(key)
            swapped.append(entry)
        set_app_tweaks(cfg.path, app_name, swapped)
        done.append(app_name)


def _do_update_tweak(
    cfg: Config, filename: str, *, emitter: JsonEmitter | None = None
) -> None:
    """Download the newest build of `filename`'s package and re-point its apps.

    One verb rather than the GUI driving `--fetch`, `--detach-tweak` and
    `--attach-tweak` in sequence: a download that lands and an attach that
    fails is one process reporting one coherent state, instead of three
    subprocesses leaving the caller to work out which half happened.

    The old .deb is deliberately left on disk. It is the only way back if the
    new build misbehaves on device — which is the whole reason `archive_keep`
    defaults to 2 — and deleting it is a thing the user can already do
    deliberately from the row it came from.
    """
    record = next(
        (r for r in _collect_tweak_records(cfg) if r["filename"] == filename), None
    )
    if record is None:
        raise TweakNotFoundError(
            f"tweak {filename!r} not found in {cfg.tweaks_dir}",
            hint="run --tweaks to see what the library holds",
        )

    status = updates.statuses([record], updates.repo_catalog(cfg))[0]
    if not status.update_available or status.available is None:
        # Not an error: the answer to "update this" can legitimately be "there
        # is nothing newer", and a caller acting on a stale badge deserves that
        # said plainly rather than an exception.
        payload = {
            **updates.as_event(status),
            "updated": False,
            "reason": "already the newest published version"
            if status.available
            else "no published version found",
        }
        if emitter:
            emitter.emit("tweak-updated", **payload)
        else:
            console.print(f"[dim]{escape(filename)}: {payload['reason']}[/dim]")
        return

    apps = list(record.get("used_by") or [])

    # Copy the current build somewhere the download cannot overwrite, *before*
    # fetching. A repo serving a stable filename (`a-tweak.deb`) writes
    # straight over it, so the rollback this function promises did not exist in
    # exactly the case the promise is for. Kept outside `tweaks_dir` so it is
    # not itself listed as a tweak.
    rollback_dir = cfg.state_dir / "tweak-rollback"
    rollback_dir.mkdir(parents=True, exist_ok=True)
    rollback = rollback_dir / filename
    shutil.copy2(cfg.tweaks_dir / filename, rollback)

    try:
        # Pinned to the version the status reported. Resolving the package id
        # afresh here is what downloaded a stale 1.7.20 after the check said
        # 1.7.24 (canister lists every version, unordered, per request). Other
        # variants of that version are tried if the first 404s; an older
        # *version* never is.
        dest = canister.fetch_deb(
            status.available.download,
            cfg.tweaks_dir,
            status.available.sha256,
            version=status.available.version,
        )
    except Exception:
        # Nothing was replaced, so the copy is not a rollback of anything —
        # just a file that would sit in state/ forever, one per failed attempt.
        # A published version whose .deb 404s is a normal upstream state, not a
        # rare one, so this accumulates.
        rollback.unlink(missing_ok=True)
        raise

    # A repo that serves a stable filename (`a-tweak.deb`) has just
    # overwritten the file in place, and every app already points at that name.
    # Detaching and re-attaching it would be a no-op at best; at worst the
    # detach lands and the attach doesn't, and the tweak silently leaves the
    # build it was part of.
    replaced_in_place = dest.name == filename
    # Named before the loop so a write that fails partway can still report
    # which apps were re-pointed: the download has landed and some configs are
    # already rewritten, and calling that "the update failed" describes a state
    # nobody is in.
    repointed: list[str] = []
    if not replaced_in_place:
        # One write per app, substituting the entry where it stands — not
        # `_do_detach_tweak` then `_do_attach_tweak`, which is wrong twice.
        # Those read the app from the *in-memory* `cfg`, so the second one sees
        # the list as it was before the first wrote, and the app ends up
        # listing both files. And attach appends, so the tweak would move to
        # the end of a list whose order is the order cyan injects in.
        old_key = cfg.tweak_key(filename)
        try:
            _repoint(cfg, apps, old_key, dest.name, repointed)
        except GraftError as e:
            # The download landed and some apps may already be re-pointed, so
            # this is not "the update failed" — it is an update that got part
            # of the way and has to say exactly how far.
            payload = {
                "filename": filename, "new_filename": dest.name,
                "package_id": status.package_id, "installed": status.installed,
                "latest": status.available.version, "source": status.available.source,
                "apps": repointed, "replaced_in_place": False,
                "rollback": str(rollback), "updated": True, "error": str(e),
            }
            if emitter:
                emitter.emit("tweak-updated", **payload)
            else:
                console.print(f"[red]x[/red] {escape(str(e))}")
                console.print(
                    "[dim]  re-pointed before failing: "
                    f"{escape(', '.join(repointed)) or 'none'}[/dim]"
                )
            return

    payload = {
        "filename": filename,
        "new_filename": dest.name,
        "package_id": status.package_id,
        "installed": status.installed,
        "latest": status.available.version,
        "source": status.available.source,
        # What was actually re-pointed, not what was intended.
        "apps": apps if replaced_in_place else repointed,
        "replaced_in_place": replaced_in_place,
        "rollback": str(rollback),
        "updated": True,
        # Which build was taken: the fallback can land on another architecture
        # than the one installed, and that should be said, not implied. Only
        # the console line shows it today; surfacing it in the GUI is a
        # follow-up.
        "variant": canister.variant_of(dest.name),
    }
    if emitter:
        emitter.emit("tweak-updated", **payload)
        return
    variant = f", {payload['variant']}" if payload["variant"] else ""
    console.print(
        f"[green]✓[/green] {escape(filename)} → {escape(dest.name)} "
        f"({escape(status.installed or '?')} → "
        f"{escape(status.available.version)}{escape(variant)})"
    )
    if apps:
        console.print(f"[dim]  re-pointed: {escape(', '.join(apps))}[/dim]")
        console.print("[dim]  rebuild those apps to pick it up[/dim]")


def _verb_update_tweak(
    *, cfg: Config, update_tweak: str | None, json_mode: bool, **_: Any
) -> None:
    _do_update_tweak(cfg, update_tweak or "", emitter=JsonEmitter() if json_mode else None)


def _verb_search(*, search: str | None, json_mode: bool, **_: Any) -> None:
    _do_search(search, emitter=JsonEmitter() if json_mode else None)


def _verb_fetch(
    *, cfg: Config, fetch: str | None, fetch_sha: str | None, json_mode: bool, **_: Any
) -> None:
    _do_fetch(cfg, fetch, emitter=JsonEmitter() if json_mode else None, expected_sha=fetch_sha)


def _verb_add_repo(*, cfg: Config, add_repo_url: str | None, json_mode: bool, **_: Any) -> None:
    _do_add_repo(cfg, add_repo_url, emitter=JsonEmitter() if json_mode else None)


def _verb_remove_repo(
    *, cfg: Config, remove_repo_url: str | None, json_mode: bool, **_: Any
) -> None:
    _do_remove_repo(cfg, remove_repo_url, emitter=JsonEmitter() if json_mode else None)


def _verb_add_app_url(
    *,
    cfg: Config,
    add_app_url: str | None,
    add_app_delivery: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    _do_add_app_url(
        cfg,
        add_app_url,
        delivery=add_app_delivery or "sideload",
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_add_app_manual(
    *,
    cfg: Config,
    add_app_name: str | None,
    add_app_bundle_id: str | None,
    add_app_display_name: str | None,
    add_app_delivery: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    # Trim before the truthiness check — `--name "   "` would otherwise
    # slip past as a "non-empty" name and slugify to "app".
    trimmed_name = (add_app_name or "").strip()
    trimmed_bundle = (add_app_bundle_id or "").strip()
    if not trimmed_name or not trimmed_bundle:
        raise _fatal(ConfigError("--add-app-manual requires --name and --bundle-id"))
    _do_add_app_manual(
        cfg,
        name=trimmed_name,
        bundle_id=trimmed_bundle,
        display_name=add_app_display_name,
        delivery=add_app_delivery or "sideload",
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_provide_ipa(
    *,
    cfg: Config,
    provide_ipa_path: Path | None,
    provide_ipa_app: str | None,
    allow_unverified: bool | None,
    json_mode: bool,
    **_: Any,
) -> None:
    if not provide_ipa_app:
        raise _fatal(ConfigError("--provide-ipa requires --app <name>"))
    _do_provide_ipa(
        cfg,
        app_name=provide_ipa_app,
        source_path=provide_ipa_path,
        emitter=JsonEmitter() if json_mode else None,
        allow_unverified=bool(allow_unverified),
    )


def _verb_appstore_status(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    appstore.report_status(cfg, emitter=JsonEmitter() if json_mode else None, say=_say_plain)


def _verb_appstore_check(
    *, cfg: Config, provide_ipa_app: str | None, json_mode: bool, **_: Any
) -> None:
    appstore.check(
        cfg,
        [provide_ipa_app] if provide_ipa_app else list(cfg.apps),
        emitter=JsonEmitter() if json_mode else None,
        say=_say_plain,
    )


def _verb_appstore_get(
    *,
    cfg: Config,
    provide_ipa_app: str | None,
    allow_unverified: bool | None,
    json_mode: bool,
    **_: Any,
) -> None:
    if not provide_ipa_app:
        raise ConfigError("--appstore-get requires --app <name>")
    emitter = JsonEmitter() if json_mode else None
    with appstore.fetched(
        cfg, provide_ipa_app, emitter=emitter,
        allow_unverified=bool(allow_unverified),
        say=_say_plain, warn=lambda message: _warn(emitter, message),
    ) as decrypted:
        _stage_ipa(cfg, app_name=provide_ipa_app, source_path=decrypted, emitter=emitter)


def _verb_cached_ipa(*, cfg: Config, cached_ipa_app: str | None, json_mode: bool, **_: Any) -> None:
    _do_cached_ipa(
        cfg,
        app_name=cached_ipa_app,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_staged_ipas(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    _do_staged_ipas(cfg, emitter=JsonEmitter() if json_mode else None)


def _verb_delete_staged(
    *, cfg: Config, delete_staged: str | None, force: bool | None, json_mode: bool, **_: Any
) -> None:
    _do_delete_staged(
        cfg, filename=delete_staged, force=bool(force),
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_install_to_mac(
    *, cfg: Config, install_to_mac: str | None, json_mode: bool, **_: Any
) -> None:
    _do_install_to_mac(
        cfg, filename=install_to_mac,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_attach_tweak(
    *, cfg: Config, attach_tweak: str | None, provide_ipa_app: str | None, json_mode: bool, **_: Any
) -> None:
    if not provide_ipa_app:
        raise _fatal(ConfigError("--attach-tweak requires --app <name>"))
    _do_attach_tweak(
        cfg, app_name=provide_ipa_app, filename=attach_tweak,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_detach_tweak(
    *, cfg: Config, detach_tweak: str | None, provide_ipa_app: str | None, json_mode: bool, **_: Any
) -> None:
    if not provide_ipa_app:
        raise _fatal(ConfigError("--detach-tweak requires --app <name>"))
    _do_detach_tweak(
        cfg, app_name=provide_ipa_app, filename=detach_tweak,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_delete_tweak(*, cfg: Config, delete_tweak: str | None, json_mode: bool, **_: Any) -> None:
    _do_delete_tweak(
        cfg, filename=delete_tweak,
        emitter=JsonEmitter() if json_mode else None,
    )


def _do_settings(config_path: Path, *, emitter: JsonEmitter | None = None) -> None:
    """Report the settings `--set-settings` can write.

    Takes the path, not a `Config`. Loading one validates the whole document
    and raises when `signing:` is incomplete — which is precisely the state
    this pane exists to repair, so requiring a valid config made the editor
    inert on every config that needed it. A missing field reports as null.
    """
    payload = read_settings(config_path)
    if emitter:
        emitter.emit("settings", **payload)
    else:
        table = Table(title="settings")
        table.add_column("setting", style="bold")
        table.add_column("value")
        for key, value in payload.items():
            table.add_row(key, "[dim]not set[/dim]" if value is None else str(value))
        console.print(table)


def _do_set_settings(
    config_path: Path,
    *,
    emitter: JsonEmitter | None = None,
    **values: Any,
) -> None:
    """Write global settings, then report exactly which keys landed.

    `changed` is the point, not decoration: the GUI builds the set of fields it
    asked for and refuses to report success for any that come back missing —
    the same reconciliation `--set-app-options` gets, and for the same reason.
    Writing a setting the CLI silently declined is how a user ends up trusting
    a value that is not on disk.

    Takes the path for the same reason `_do_settings` does: a config too broken
    to load is the one most in need of editing.

    Under --json it also emits the resulting `settings`, so a caller does not
    have to spawn `--settings` to find out what it just wrote. That second run
    did ~0ms of work above interpreter startup and cost as much wall-clock as
    the write itself — a third of the GUI's save, for a value this process
    already has in hand — and reading it here also closes the window in which
    something else could edit the file in between.
    """
    changed = set_settings(config_path, **values)
    if emitter:
        emitter.emit("settings-updated", changed=changed)
        # Best-effort, and swallowing the error is the point: the write above
        # already landed on disk, so letting a failed *read* raise would exit
        # non-zero and have the GUI report a save that succeeded as failed —
        # `CLIStore.mutate`'s rule, one process further down. A caller that
        # gets no `settings` event falls back to running `--settings` itself,
        # which is what it did before this event existed.
        try:
            emitter.emit("settings", **read_settings(config_path))
        except (ConfigError, OSError):
            pass
    else:
        if not changed:
            console.print("[dim]nothing to change[/dim]")
        for key in changed:
            console.print(f"✓ {key}")


def _verb_set_app_options(
    *,
    cfg: Config,
    provide_ipa_app: str | None,
    opt_preferred_version: str | None,
    add_app_display_name: str | None,
    opt_keep_bundle_id: bool | None,
    opt_strip_extensions: bool | None,
    opt_strip_supported_devices: bool | None,
    opt_compression_level: int | None,
    add_app_delivery: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    if not provide_ipa_app:
        raise _fatal(ConfigError("--set-app-options requires --app <name>"))
    _do_set_app_options(
        cfg,
        app_name=provide_ipa_app,
        preferred_version=opt_preferred_version,
        display_name=add_app_display_name,
        keep_bundle_id=opt_keep_bundle_id,
        strip_extensions=opt_strip_extensions,
        strip_supported_devices=opt_strip_supported_devices,
        compression_level=opt_compression_level,
        delivery=add_app_delivery,
        emitter=JsonEmitter() if json_mode else None,
    )



def _verb_reconcile(
    *, cfg: Config, provide_ipa_app: str | None, device: str | None, json_mode: bool, **_: Any
) -> None:
    _do_reconcile(
        cfg,
        app_name=provide_ipa_app,
        device=device,
        emitter=JsonEmitter() if json_mode else None,
    )



def _verb_settings(*, config_path: Path, json_mode: bool, **_: Any) -> None:
    _do_settings(config_path, emitter=JsonEmitter() if json_mode else None)


def _verb_set_settings(
    *,
    config_path: Path,
    default_device: str | None,
    archive_keep: int | None,
    signing_cert: str | None,
    signing_profile: str | None,
    signing_team_id: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    # Keyed by the config keys, and checked against `SETTINGS`, for the reason
    # `_do_set_app_options` checks against APP_OPTION_FIELDS: `--describe`
    # advertises `setting_fields` straight from that table, so a setting
    # registered there and not wired to a flag here would have the CLI claiming
    # a capability it does not have — the one thing --describe promises cannot
    # happen. `set_settings` already rejects the opposite drift by name.
    supplied: dict[str, Any] = {
        "default_device_udid": default_device,
        "archive_keep": archive_keep,
        "signing_cert": signing_cert,
        "signing_profile": signing_profile,
        "signing_team_id": signing_team_id,
    }
    if set(supplied) != set(SETTINGS):
        raise ValueError(
            "settings flags out of sync with SETTINGS: "
            f"{sorted(set(supplied) ^ set(SETTINGS))}"
        )
    _do_set_settings(
        config_path,
        emitter=JsonEmitter() if json_mode else None,
        **supplied,
    )


def _verb_delivery_state(
    *,
    cfg: Config,
    provide_ipa_app: str | None,
    device: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    _do_delivery_state(
        cfg,
        app_name=provide_ipa_app,
        udid=device,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_remove_app(*, cfg: Config, remove_app_name: str | None, json_mode: bool, **_: Any) -> None:
    _do_remove_app(
        cfg, app_name=remove_app_name,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_repos(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    _do_repos(cfg, emitter=JsonEmitter() if json_mode else None)


def _verb_repo_packages(
    *, cfg: Config, repo_packages: str | None, refresh: bool, json_mode: bool, **_: Any
) -> None:
    _do_repo_packages(cfg, repo_packages, refresh, emitter=JsonEmitter() if json_mode else None)


def _verb_builds(
    *, cfg: Config, builds_app: str | None, builds_limit: int, json_mode: bool, **_: Any
) -> None:
    _do_builds(
        cfg,
        app=builds_app,
        limit=builds_limit,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_reveal_build(*, cfg: Config, reveal_build: int | None, **_: Any) -> None:
    _do_reveal_build(cfg, reveal_build)


def _verb_reinstall_build(
    *, cfg: Config, reinstall_build: int | None, json_mode: bool, **_: Any
) -> None:
    _do_reinstall_build(cfg, reinstall_build, emitter=JsonEmitter() if json_mode else None)


def _verb_build_log(*, cfg: Config, build_log: int | None, **_: Any) -> None:
    _do_build_log(cfg, build_log)


def _verb_prune_builds(
    *, cfg: Config, keep: int | None, dry_run: bool, json_mode: bool, **_: Any
) -> None:
    _do_prune_builds(
        cfg,
        keep=keep,
        dry_run=dry_run,
        emitter=JsonEmitter() if json_mode else None,
    )


def _do_decrypt(
    cfg: Config, bundle_id: str, *, device: str | None, mode: str, backend: str,
    strip_encrypted_extensions: bool = False, strip_extensions_forced: bool = False,
    verbose: bool = False, emitter: JsonEmitter | None = None,
) -> None:
    """Dump a decrypted IPA off the device and stage it for the pipeline.

    Three steps, and the middle one is the point: dump with bagbak, *verify the
    result is actually cryptid 0*, then stage it under the canonical name. A
    dump that silently isn't decrypted would sail through here and blow up in
    stage 2.5 of the next build with a far more confusing error — so the check
    the build does is done here too, at the moment there is someone to tell.
    """
    if emitter:
        emitter.emit("decrypt-start", bundle_id=bundle_id)

    # Said before the dump starts, not after: `--decrypt-mode main` on a bagbak
    # with no mode option dumps the entire bundle instead — minutes and hundreds
    # of megabytes the user did not ask for — and announcing "(main)" while
    # doing it was the CLI reporting a choice it had already discarded.
    if (
        backend == decrypt_runner.BAGBAK
        and mode != "all"
        and not decrypt_runner.mode_is_supported()
    ):
        _warn(
            emitter,
            f"--decrypt-mode {mode} is ignored: the installed bagbak has no mode "
            f"option and always dumps the whole bundle",
        )

    udid = device or cfg.default_device_udid
    with tempfile.TemporaryDirectory(prefix="tweak-decrypt-") as tmp:
        dump_dir = Path(tmp) / "dump"
        if not emitter:
            # Only name the mode when the installed bagbak actually has one.
            # 4.x does not, so announcing "(main)" and then dumping the whole
            # bundle was the CLI reporting a choice it had already discarded.
            supports_mode = (
                backend == decrypt_runner.BAGBAK and decrypt_runner.mode_is_supported()
            )
            label = f"bagbak ({mode})" if supports_mode else backend

            console.print(f"→ dumping [cyan]{escape(bundle_id)}[/cyan] with {label}…")
        # Verbose puts the backend's own narration in front of the user as it
        # arrives. Worth having because these tools are the opaque part: a dump
        # that has pulled 200 files and a dump that is wedged look identical
        # from outside, and until now the only difference the user saw was how
        # long they waited before a timeout.
        def on_line(line: str) -> None:
            if not verbose:
                return
            if emitter:
                emitter.emit("log", source=backend, line=line)
            else:
                console.print(f"[dim]{escape(line)}[/dim]")

        result = decrypt_runner.decrypt_app(
            bundle_id, dump_dir, udid=udid, backend=backend, mode=mode,
            on_line=on_line,
        )

        # Its own Info.plist, so the staged name matches the version dumped and
        # not whatever the caller believed was installed.
        info = read_info_plist(result.ipa_path)
        if info.bundle_id != bundle_id:
            raise decrypt_runner.DecryptError(
                f"dumped {info.bundle_id}, expected {bundle_id}",
                hint="the app id on the device does not match what was requested",
            )

        extract_dir = Path(tmp) / "verify"
        extract_ipa(result.ipa_path, extract_dir)
        # Before anything repacks: whatever the backend produced is now on disk,
        # and this is the one moment both the extracted tree and the archive it
        # came from are in hand.
        unexecutable = normalize_exec_bits(extract_dir)
        cryptid = verify_decrypted(extract_dir)
        # `not is_decrypted`, exactly as stage 2.5 does: a binary with no
        # LC_ENCRYPTION_INFO (a framework) has an empty cryptid list and is
        # fine, so testing `!= 0` per value would be wrong where the build's own
        # check is right.
        still_encrypted = [c for c in cryptid if not c.is_decrypted]
        stripped: list[str] = []
        # Kept beside `stripped` rather than inside the branch below, so the
        # write at the end of the `with` block — not the end of this
        # function, which still has the warning and the emit to get through —
        # can read them unconditionally.
        lost: list[str] = []
        importance: dict[str, str] = {}
        if still_encrypted and strip_encrypted_extensions:
            # Remove whole `.appex` bundles, not just the offending binary: an
            # extension missing its executable is a bundle iOS will still try
            # to load and fail on, which is worse than not shipping it.
            #
            # Only when *every* leftover is inside one. A main binary or a
            # framework left encrypted is a different problem — the dump went
            # wrong rather than hitting a structural limit — and deleting an
            # app's own framework to make a check pass would produce a signed,
            # installable, broken app.
            doomed = _extensions_to_strip(still_encrypted, extract_dir)
            lost = lost_capabilities(doomed)
            # Read here, beside `lost_capabilities` above, rather than in the
            # delete loop below — so the ordering constraint is impossible to
            # violate by editing, since nothing has been deleted yet at this
            # point in the function. Both `lost_capabilities` and
            # `extension_importance` open each appex's own Info.plist; once
            # `shutil.rmtree` has run, that open *raises* — but `extension_point`
            # swallows it (`except (OSError, ValueError): return None`) and
            # `extension_importance` maps the resulting `None` to `"essential"`,
            # the loudest tier, the one the GUI leads with. So the cost of
            # reading a line too late is a silently wrong answer rather than a
            # crash — and that is what this placement rules out rather than
            # merely warns against.
            importance = {
                str(a.relative_to(extract_dir)): extension_importance(a)
                for a in sorted(doomed)
            }
            essential = sorted(
                a for a in doomed
                if importance[str(a.relative_to(extract_dir))] == "essential"
            )
            if essential and not strip_extensions_forced:
                # Refusing beats succeeding here. Removing one of these does not
                # trim the app, it empties it: the sweep that found this left a
                # 0.1 MB content blocker and a 0.7 MB VPN app, each reported as decrypted,
                # each destined to install and do nothing — and to be blamed on
                # whatever tweak was injected. Neither backend can decrypt them,
                # so this is the end of the line rather than a rung.
                names = [a.name for a in essential]
                raise decrypt_runner.StillEncryptedError(
                    f"{bundle_id} cannot be decrypted without losing what it is for: "
                    f"{', '.join(names)}",
                    hint=(
                        "these are not widgets — the app is a shell without them "
                        f"({', '.join(sorted({extension_point(a) or 'unknown' for a in essential}))}). "
                        "No backend can decrypt an extension that cannot be launched on "
                        "demand, so retrying will not help. `--strip-encrypted-extensions` "
                        "removes them anyway if a partial app is still useful to you."
                    ),
                    paths=[str(a.relative_to(extract_dir)) for a in essential],
                    extensions_only=True,
                )
            if doomed:
                for appex in sorted(doomed):
                    stripped.append(str(appex.relative_to(extract_dir)))
                    shutil.rmtree(appex)
                # Re-verified rather than assumed: the point of removing them is
                # that what remains is clean, and the only honest way to say so
                # is to look again.
                cryptid = verify_decrypted(extract_dir)
                still_encrypted = [c for c in cryptid if not c.is_decrypted]
                result = decrypt_runner.DecryptResult(
                    ipa_path=repack_ipa(extract_dir, Path(tmp) / "stripped.ipa"),
                    bundle_id=result.bundle_id,
                    stdout=result.stdout,
                )

        if still_encrypted:
            rel = [str(c.binary.relative_to(extract_dir)) for c in still_encrypted]
            # When every leftover is an extension the paths add nothing — the
            # answer is "strip them", not "which ones". Naming them anyway cost
            # the *hint* its place: the GUI's banner shortens from the middle at
            # 150 characters, so eleven paths ate the file list and the opening
            # of the sentence that says what to do, leaving one that began
            # mid-word. Short message, intact advice.
            extensions_only = rel and all(".appex/" in p for p in rel)
            if extensions_only:
                summary = (
                    f"{len(still_encrypted)} of {len(cryptid)} binaries are still "
                    f"encrypted — all of them app extensions"
                )
            else:
                shown = ", ".join(rel[:3])
                if len(rel) > 3:
                    shown += f", … (+{len(rel) - 3} more)"
                summary = (
                    f"{len(still_encrypted)} of {len(cryptid)} binaries are still "
                    f"encrypted after the dump: {shown}"
                )
            raise decrypt_runner.StillEncryptedError(
                summary,
                hint=_still_encrypted_hint(rel, backend, stripped_attempted=bool(stripped)),
                paths=rel,
                extensions_only=bool(extensions_only),
            )

        if unexecutable and not stripped:
            # The strip path already repacks from this tree, so this is only for
            # a dump that was otherwise fine. Paid only when something actually
            # needed fixing — a clean dump is staged as the backend produced it.
            result = decrypt_runner.DecryptResult(
                ipa_path=repack_ipa(extract_dir, Path(tmp) / "normalized.ipa"),
                bundle_id=result.bundle_id,
                stdout=result.stdout,
            )

        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        final = cfg.cache_dir / f"{info.bundle_id}_{info.version}.ipa"
        shutil.move(str(result.ipa_path), final)
        # A stripped dump lands under the same canonical name a complete one
        # does, and nothing inside it reads differently — so the fact is
        # recorded beside it, or every later build silently reuses an app
        # missing its extensions while reporting an ordinary cache hit.
        #
        # Cleared as deliberately as it is written. `shutil.move` replaces the
        # IPA but not the note beside it, so a *complete* re-dump of the same
        # version inherited the previous run's sidecar and every later build
        # warned about extensions that were sitting right there — the one
        # warning that exists to tell the truth about a partial app, lying, until
        # someone deleted a file they were never told about.
        if stripped:
            mark_stripped(final, stripped, lost=lost, importance=importance)
        else:
            clear_stripped(final)

    if stripped:
        # Said plainly and every time: this IPA is missing functionality the
        # installed app has, and a user who forgets that will file the missing
        # widget as a bug against the tweak.
        #
        # Before the early return below, not after it. `_warn` used to be called
        # past that `return`, where `emitter` is provably None — so a --json run
        # never received the sentence naming what stopped working, and the pane
        # was left with filenames, which is the wrong unit for "password autofill
        # will not work".
        plural = len(stripped) > 1
        _warn(
            emitter,
            f"removed {len(stripped)} app {'extensions' if plural else 'extension'} "
            f"that could not be decrypted — the built app will not have "
            f"{'them' if plural else 'it'}: " + ", ".join(stripped)
            + (
                # The filenames say which bundles went; this says what the user
                # will notice. Autofill silently stops being offered, where a
                # missing widget is visibly missing.
                f". {' and '.join(lost)} will not work"
                if lost else ""
            )
            + (
                # frida-ios-dump can *never* read an extension: it decrypts the
                # modules loaded into the app's own process, and an extension
                # runs in its own. bagbak attaches to each extension instead,
                # and it demonstrably works — this cache holds Example, Bravo
                # and Delta dumped on this phone with every extension at
                # cryptid 0, including the widgets and notification-service
                # extensions.
                #
                # What changed is the pairing. Those dumps were taken while the
                # device ran frida-server 17.x with a bagbak 5.x; the installed
                # bagbak 4.x, pinned to match the 16.x server that
                # frida-ios-dump's 2021 agent needs, hangs after downloading the
                # bundle. So the honest advice names the trade rather than a
                # flag that will time out.
                ". frida-ios-dump can never read an extension — they run in "
                "their own processes. bagbak can, but not the 4.x pinned here: "
                "extensions came back whole on this phone under frida-server "
                "17.x with bagbak 5.x, which is the pairing to restore if you "
                "need them (at the cost of frida-ios-dump). A pre-decrypted IPA "
                "via `--provide-ipa` is the other route"
                if stripped else ""
            ),
        )

    if emitter:
        emitter.emit(
            "decrypt-end", bundle_id=bundle_id, version=info.version,
            path=str(final), binaries=len(cryptid), stripped=stripped,
        )
        return
    size_mb = final.stat().st_size / 1_000_000  # see _human_bytes on why 1000
    console.print(
        f"[green]✓[/green] {escape(info.bundle_id)} v{escape(info.version)} "
        f"decrypted ({len(cryptid)} binaries, cryptid 0) — {size_mb:.1f} MB"
    )
    console.print(f"[dim]  staged at {escape(str(final))} — build the app to use it[/dim]")


def _decrypt_backend_for(bundle_id: str, udid: str | None) -> str:
    """Which backend to open with — asked, not decided here.

    The readiness probe already ranks the backends that can actually *run*:
    frida-ios-dump first, since it attaches to the live process, which is the
    only reliable way to decrypt a main binary — and it pulls the bundle over
    SSH, so on a phone whose sshd has stopped the probe rules it out.

    This function used to rank the *installed* ones instead, which was a third
    copy of a rule that already existed twice. The copies then disagreed exactly
    where it hurt: with SSH down the pane labelled every row "will be launched
    and dumped with bagbak" and the dump ran frida-ios-dump anyway, spending
    twenty seconds to fail with advice about installing an OpenSSH the phone
    already had. Asking costs about two seconds against a dump measured in tens.

    Falls back to what is installed when the probe cannot answer at all — a
    dead frida-server, say. The dump's own error says that better than a
    refusal here would.
    """
    from . import decrypt_ready

    try:
        recommended = decrypt_ready.check(udid).recommended_backend
    except Exception:
        # A preference query, not a precondition: anything that stops the probe
        # answering is something the dump is about to report properly.
        recommended = None
    return recommended or decrypt_runner.preferred_backend() or decrypt_runner.FRIDA


def _other_backend(backend: str) -> str:
    return decrypt_runner.BAGBAK if backend == decrypt_runner.FRIDA else decrypt_runner.FRIDA


def _next_rung(
    error: decrypt_runner.DecryptError, backend: str, tried: set[str], *, pinned: bool,
) -> tuple[str, str] | None:
    """What to try next after `error`, or None to give up and report it.

    One question, because the dump itself answers the other. Leftovers inside
    `.appex` bundles are structural — frida-ios-dump can never decrypt an
    extension (own process, copied from disk) and bagbak cannot launch one built
    for a newer iOS than the phone runs — so `_do_decrypt` drops them from the
    tree it already extracted and stages the rest. There is nothing left for a
    second attempt to discover, and asking for one cost a whole extra dump: an
    Alpha that took 1m35 took 50 seconds once this stopped re-dumping to strip.

    What is worth another attempt is the opposite case. An encrypted *main
    binary*, or a dump that failed outright, says the run came back incomplete
    rather than hitting a limit — and the other tool genuinely may succeed.
    """
    if isinstance(error, decrypt_runner.AppWillNotRunError):
        # Nothing to escalate to: the app never ran, and the other backend
        # launches it exactly the same way.
        return None

    still = error if isinstance(error, decrypt_runner.StillEncryptedError) else None
    if still and still.extensions_only:
        # Already stripped if stripping was allowed; still here means it was
        # forbidden, or `_extensions_to_strip` refused. Either way, done.
        return None

    other = _other_backend(backend)
    # A named backend is an answer, not a hint: quietly dumping with the other
    # one would produce an IPA the user did not ask for and could not explain.
    if not pinned and other not in tried and other in decrypt_runner.available_backends():
        reason = (
            "the main binary came back still encrypted"
            if still
            else "that backend could not complete the dump"
        )
        return other, f"{reason} — trying {other}"
    return None


def _lock_key(udid: str | None) -> str:
    """The filename the one-dump-per-device lock lives under.

    Resolved rather than defaulted. `udid or "default"` let the same phone be
    locked under two names — a terminal with no configured default took
    `default.lock` while the GUI, which always sends `--device`, took
    `<udid>.lock` — so both dumps ran and reproduced exactly the collision the
    lock exists to prevent. `select_device(None)` names the phone a dump with no
    `--device` will actually reach, which is the same one.

    `unnamed` only when nothing is connected, and a dump with no device is about
    to fail for a better-explained reason than this.
    """
    if udid:
        return udid
    try:
        return select_device(None)[0] or "unnamed"
    except InstallError:
        return "unnamed"


def _decrypt_escalating(
    cfg: Config, bundle_id: str, *, device: str | None, mode: str,
    backend: str | None, strip: bool | None,
    verbose: bool, emitter: JsonEmitter | None,
) -> None:
    """Dump `bundle_id`, changing tack automatically until it is as decrypted as it gets.

    One command instead of a conversation. Before this, a dump that came back
    partly encrypted ended in an error naming the flag or the backend to try
    next, and the user re-ran it by hand — twice over for the common case of
    "wrong tool, then extensions in the way". Every one of those decisions is
    mechanical, so the CLI makes them.

    `strip` only has to say *no*. Dropping extensions that no tool can decrypt is
    what "as decrypted as it can get" means for an app that has one, and it
    happens inside the dump that found them, from the tree already extracted.
    `False` is for the user who would rather have nothing than a partial app.
    The result is always marked and always reported, so this never quietly
    becomes "less app than you asked for".
    """
    udid = device or cfg.default_device_udid
    # Keyed on the *resolved* device. `udid or "default"` let the same phone be
    # locked under two names — a terminal with no configured default took
    # `default.lock` while the GUI, which always sends `--device`, took
    # `<udid>.lock` — so both dumps ran and reproduced exactly the collision the
    # lock exists to prevent. With no device resolvable there is one implicit
    # phone, and one lock for it.
    with decrypt_runner.device_dump_lock(
        cfg.cache_dir / ".locks" / f"{_lock_key(udid)}.lock", udid
    ):
        _decrypt_climbing(
            cfg, bundle_id, udid=udid, mode=mode, backend=backend, strip=strip,
            verbose=verbose, emitter=emitter,
        )


def _decrypt_climbing(
    cfg: Config, bundle_id: str, *, udid: str | None, mode: str,
    backend: str | None, strip: bool | None, verbose: bool,
    emitter: JsonEmitter | None,
) -> None:
    """The ladder itself, with the device already claimed."""
    pinned = backend is not None
    current = backend or _decrypt_backend_for(bundle_id, udid)
    # Only `False` means "never": a dump that can be completed by dropping
    # extensions no tool can read is completed, and says so loudly. Explicitly
    # asking to strip and leaving it to us are the same request — the difference
    # was one that only showed up as an extra minute of dumping.
    strip_now = strip is not False
    tried: set[str] = set()

    # Bounded because every rung costs a real dump — a minute or more each. Two
    # is the longest useful path now that dropping undumpable extensions happens
    # inside a dump rather than as another one: open, and switch tools once.
    for _ in range(2):
        tried.add(current)
        try:
            _do_decrypt(
                cfg, bundle_id, device=udid, mode=mode, backend=current,
                strip_encrypted_extensions=strip_now,
                # `True` is a user who typed the flag; the default is not one,
                # and the difference is exactly who gets to decide that an app
                # is worth having without the thing it exists to do.
                strip_extensions_forced=strip is True,
                verbose=verbose, emitter=emitter,
            )
            return
        except decrypt_runner.DecryptError as e:
            nxt = _next_rung(e, current, tried, pinned=pinned)
            if nxt is None:
                raise
            current, why = nxt
            _say_escalating(emitter, why)
    raise decrypt_runner.DecryptError(
        f"gave up dumping {bundle_id} after 2 attempts",
        hint="the transcript above says what each attempt hit; --verbose shows the "
        "backend's own output",
    )


def _say_escalating(emitter: JsonEmitter | None, why: str) -> None:
    """Narrate a change of tack.

    Emitted unconditionally rather than under --verbose: this is the CLI
    deciding something on the user's behalf, and a minute of silence followed by
    a different tool's output is exactly how automatic behaviour becomes
    inexplicable behaviour.
    """
    if emitter:
        emitter.emit("log", source="graft", line=why)
    else:
        console.print(f"[yellow]→[/yellow] {escape(why)}")


def _dispatch_decrypt(
    cfg: Config, bundle_id: str, *, device: str | None, decrypt_mode: str | None,
    decrypt_backend: str | None, strip_encrypted_extensions: bool | None,
    verbose: bool, json_mode: bool,
) -> None:
    """The shared tail of both decrypt verbs: pick the backend, then dump.

    `--decrypt` (bundle typed) and `--decrypt-frontmost` (bundle read off the
    foreground app) end identically — default the backend and hand to
    `_do_decrypt`, which verifies cryptid 0 and stages the result. One place, so
    a new `_do_decrypt` argument or a changed mode default can't land on one
    verb and not the other.
    """
    _decrypt_escalating(
        cfg, bundle_id, device=device, mode=decrypt_mode or "all",
        backend=decrypt_backend, strip=strip_encrypted_extensions,
        verbose=verbose,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_decrypt(
    *, cfg: Config, decrypt: str | None, device: str | None, decrypt_mode: str | None,
    decrypt_backend: str | None, strip_encrypted_extensions: bool | None = None,
    verbose: bool = False, json_mode: bool, **_: Any,
) -> None:
    if not decrypt:
        raise _fatal(ConfigError("--decrypt requires a bundle id"))
    _dispatch_decrypt(
        cfg, decrypt, device=device, decrypt_mode=decrypt_mode,
        decrypt_backend=decrypt_backend,
        strip_encrypted_extensions=strip_encrypted_extensions, verbose=verbose,
        json_mode=json_mode,
    )


def _verb_decrypt_frontmost(
    *, cfg: Config, device: str | None, decrypt_mode: str | None,
    decrypt_backend: str | None, strip_encrypted_extensions: bool | None = None,
    verbose: bool = False, json_mode: bool, **_: Any,
) -> None:
    """`--decrypt`, but on whatever app is on screen — no bundle id to type.

    The reliable dump needs the target foreground-active, and the app the user
    just opened is exactly that. Asking the device what's frontmost, rather than
    taking a bundle id, both removes a step and closes the gap `app_is_running`
    can't see: a suspended background app that would pass a pid check and then
    fail the attach. From here it is the ordinary decrypt — same verify, same
    staging — on the id the device reported.
    """
    udid = device or cfg.default_device_udid
    bundle = decrypt_runner.frontmost_app(udid)
    if not bundle:
        raise _fatal(decrypt_runner.DecryptError(
            "no app is in the foreground on the device",
            hint="open the app you want to decrypt on the phone and leave it on "
            "screen, then retry — this dumps whatever is frontmost",
        ))
    # Pass the *resolved* udid, not the raw `device`: the app we detected as
    # frontmost and the app we dump are then provably the same device, rather
    # than two independent resolutions of `device or default` a future rule
    # change could split.
    _dispatch_decrypt(
        cfg, bundle, device=udid, decrypt_mode=decrypt_mode,
        decrypt_backend=decrypt_backend,
        strip_encrypted_extensions=strip_encrypted_extensions, verbose=verbose,
        json_mode=json_mode,
    )


def _do_device_apps(
    cfg: Config,
    *,
    device: str | None,
    with_icons: bool = False,
    refresh_icons: bool = False,
    emitter: JsonEmitter | None = None,
) -> None:
    """List the user apps on the target device, marking the encrypted ones.

    The target list for on-device decryption: the encrypted apps are the ones a
    dump tool (bagbak, frida-ios-dump) has any reason to attach to, because a
    sideload is already decrypted.

    `with_icons` adds each app's home-screen icon, cached to disk and reported
    as `icon_path`. Opt-in and defaulting to False: on a cold cache it is a
    second pass over the same phone that roughly doubles what the listing costs
    (measured: 4.0s of icons on top of 3.8s of listing, 67 apps, Wi-Fi), and
    every other caller here wants the names now rather than the pictures in a
    few seconds. A warm cache costs 0.5s — `icons_for` asks the phone only
    about the ids it has no believable cached answer for.

    `refresh_icons` (`--refresh-artwork`) is the escape hatch that warm path
    needs: without it there is no way to pick up a home-screen icon that
    changed under an unchanged bundle id short of deleting the cache by hand.
    Ignored unless `with_icons` is set, because there is nothing to refresh
    otherwise.
    """
    # Not `select_device`. That resolves the udid through `idevice_id`
    # (libimobiledevice), while `list_installed_apps` connects through
    # pymobiledevice3's usbmux — two libraries with two separate views of which
    # Wi-Fi devices are up right now, which differ moment to moment. Resolving
    # through one and connecting through the other made the list fail on a phone
    # the connecting library could see perfectly well. Same library, start to
    # finish.
    udid = device or cfg.default_device_udid
    apps = device_apps.list_installed_apps(udid)
    # Nothing at all is fetched unless asked. `icons_for` never raises, so an
    # unreachable or slow phone costs icons rather than the listing.
    icons = (
        device_apps.icons_for(
            [a.bundle_id for a in apps], udid=udid, refresh=refresh_icons
        )
        if with_icons
        else {}
    )
    if emitter:
        for a in apps:
            # `icon_path` is *absent*, not null, for an app with no icon. Not
            # the common case on this listing — `get_apps(application_type=
            # "User")` reports only top-level installs, so a LiveContainer guest
            # is never in it to begin with, and 66 of the 67 apps on the test
            # phone had an icon — but SpringBoard answers its generic
            # placeholder for some (one, measured: com.example.other), and
            # those are rejected rather than cached. An absent key is what the
            # GUI's optional decode already reads as "draw the letter tile"; a
            # null would be a second spelling of the same thing.
            extra = {"icon_path": icons[a.bundle_id]} if a.bundle_id in icons else {}
            emitter.emit(
                "device-app",
                bundle_id=a.bundle_id, name=a.name, version=a.version,
                encrypted=a.encrypted, frontmost=a.frontmost,
                **extra,
            )
        emitter.emit(
            "device-apps-end",
            count=len(apps), encrypted=sum(1 for a in apps if a.encrypted),
        )
        return
    table = Table(title=f"apps on {udid}")
    table.add_column("app", style="bold")
    table.add_column("bundle id")
    table.add_column("version")
    table.add_column("")
    if with_icons:
        # A column only when the flag was passed, so the seconds it spends are
        # visible in the human-readable output too rather than being work with
        # no trace. The path itself goes on the JSON events, where it is read;
        # here it would wrap across 67 rows to say the same thing.
        table.add_column("icon")
    for a in apps:
        row = [
            a.name, a.bundle_id, a.version,
            "[cyan]encrypted[/cyan]" if a.encrypted else "[dim]sideload[/dim]",
        ]
        if with_icons:
            row.append("cached" if a.bundle_id in icons else "[dim]—[/dim]")
        table.add_row(*row)
    console.print(table)


def _verb_device_apps(
    *, cfg: Config, device: str | None, with_icons: bool | None, refresh_artwork: bool,
    json_mode: bool, **_: Any
) -> None:
    _do_device_apps(
        cfg, device=device, with_icons=bool(with_icons), refresh_icons=refresh_artwork,
        emitter=JsonEmitter() if json_mode else None,
    )


def _do_device_info(cfg: Config, *, udid: str | None, emitter: JsonEmitter | None) -> None:
    """Report one device's storage and battery — the two stats `devices.py`
    and `device_apps.py` don't already cover for the GUI's per-device pane.

    `cfg.default_device_udid` is a config *field*, not a function — the
    `device or cfg.default_device_udid` idiom recurs throughout this file
    (`_do_decrypt_check`, `_do_set_ssh_password`, `_do_device_apps`, …), and is
    used again here.
    """
    # Imported here, not at module scope: `device_info.py` imports
    # `pymobiledevice3.lockdown` at *its* module scope (unlike `devices.py`,
    # `device_apps.py` and `artwork.py`'s device rung, which all defer that
    # import into an `async def`), so a module-scope `from .device_info import
    # read_device_info` here would make `cli.py` itself — imported on every
    # invocation, `--describe` included — pay the ~0.22s pymobiledevice3
    # import cost. Measured: with this import lazy, `pymobiledevice3` is
    # absent from `sys.modules` after `import graft_ipa.cli` and `--describe
    # --json` stays at its ~0.14s floor; hoisting it to the top of this file
    # regressed both.
    from .device_info import read_device_info

    target = udid or cfg.default_device_udid
    if not target:
        # The only site in this file that fatals on "no --device and no
        # configured default". `_do_device_apps` passes None straight through
        # and lets usbmux pick whichever phone it enumerates first, and four
        # other sites do the same — so this guard is self-imposed and
        # inconsistent with its neighbours, and the reason it stays that way
        # was never written down:
        #
        # `DeviceInfoRecord.udid` is echoed back from the request rather than
        # read off the lockdown session, so with no target there is nothing
        # truthful to put in the event's `udid` field — and a store keyed on
        # exactly that field would then be keyed on null. Failing here is what
        # protects the key. Do not relax this into "let usbmux choose" without
        # first making the udid come off the session.
        raise _fatal(ConfigError("no device given and no default device configured"))
    info = read_device_info(target)
    if info is None:
        # Not fatal. The pane that asks for this renders without the two
        # cells, and an unreachable phone is an ordinary state the GUI already
        # shows elsewhere (see `_do_devices`'s `device-error` handling).
        #
        # Deliberately not `carried_by="device-info"`, even though the event
        # below now carries this same fact as `reachable: false`: suppression
        # is only safe when something actually renders the carrier, and
        # `device-info` is declared in `eventsHandled` ahead of its decoder —
        # no view reads it yet. Suppressing the `log` would leave this warning
        # on stderr alone, which the GUI reads only off a non-zero exit, and
        # this verb exits 0.
        _warn(emitter, f"couldn't read device info for {target}")
    if emitter:
        # Emitted on both paths, always. Nothing on the wire was the same
        # signal as a crashed CLI or a skipped emit, and every other listing
        # verb here ends with a sentinel — see this event's entry in
        # `json_output.EVENT_TYPES` for the full argument. `reachable` is what
        # makes the null-filled version readable: a consumer can keep the last
        # good reading across a missed handshake instead of blanking a strip
        # that showed 81% a moment ago. The one that does — the GUI's
        # `DeviceInfoStore` — calls this verb when a pane opens on a phone and
        # when its Refresh is pressed, not on a timer, so what this protects is
        # a single press landing on a blip rather than one tick of a poll.
        #
        # `target`, not `info.udid`, because the record echoes the request
        # anyway (see the guard above) and the unreachable arm has no record to
        # read it off. One spelling for both.
        emitter.emit(
            "device-info",
            udid=target,
            reachable=info is not None,
            total_bytes=info.total_bytes if info is not None else None,
            free_bytes=info.free_bytes if info is not None else None,
            battery_percent=info.battery_percent if info is not None else None,
            battery_charging=info.battery_charging if info is not None else None,
        )
    elif info is not None:
        total = "?" if info.total_bytes is None else str(info.total_bytes)
        free = "?" if info.free_bytes is None else str(info.free_bytes)
        console.print(f"{info.udid}: {free} free of {total}")


def _verb_device_info(*, cfg: Config, device: str | None, json_mode: bool, **_: Any) -> None:
    _do_device_info(cfg, udid=device, emitter=JsonEmitter() if json_mode else None)


# LiveContainer management. The logic is all in `lc_manage`, imported inside
# each handler (it reaches pymobiledevice3 and cryptography, neither of which
# `--describe` or the device poll should pay for at startup); these only pick
# the output channel.


def _lc_events(emitter: JsonEmitter | None) -> Any:
    """`lc_manage`'s output channel: events under --json, lines otherwise, and
    warnings through `_warn` either way."""
    from . import lc_manage

    return lc_manage.LcEvents(
        emitter,
        say=lambda line: console.print(escape(line)),
        warn=lambda message: _warn(emitter, message),
    )


def _verb_lc_status(
    *, cfg: Config, device: str | None, lc_channel: str | None, json_mode: bool, **_: Any
) -> None:
    from . import lc_manage

    with lc_manage.exit_on_termination():
        lc_manage.run_status(
            cfg, udid=device, channel=lc_channel,
            events=_lc_events(JsonEmitter() if json_mode else None),
        )


def _verb_lc_install(
    *, cfg: Config, device: str | None, lc_instances: str | None, lc_channel: str | None,
    force: bool | None, json_mode: bool, **_: Any,
) -> None:
    from . import lc_manage

    # SIGTERM -> SystemExit, so a GUI Stop mid-signing still takes the
    # throwaway keychain off the user's search list (`lc_sign.temp_keychain`).
    with lc_manage.exit_on_termination():
        lc_manage.run_install(
            cfg, udid=device, instances=lc_instances, channel=lc_channel, force=bool(force),
            events=_lc_events(JsonEmitter() if json_mode else None),
        )


def _verb_lc_push_cert(*, cfg: Config, device: str | None, json_mode: bool, **_: Any) -> None:
    from . import lc_manage

    with lc_manage.exit_on_termination():
        lc_manage.run_push_cert(
            cfg, udid=device, events=_lc_events(JsonEmitter() if json_mode else None)
        )


def _verb_lc_clean(
    *, cfg: Config, device: str | None, lc_cert_only: bool | None, json_mode: bool, **_: Any
) -> None:
    from . import lc_manage

    with lc_manage.exit_on_termination():
        lc_manage.run_clean(
            cfg, udid=device, events=_lc_events(JsonEmitter() if json_mode else None),
            cert_only=bool(lc_cert_only),
        )


def _verb_devices(*, cfg: Config, json_mode: bool, brief: bool | None, **_: Any) -> None:
    emitter = JsonEmitter() if json_mode else None
    if brief:
        _do_devices_brief(emitter)
        return
    _do_devices(cfg, emitter=emitter)


def _verb_artwork(
    *, cfg: Config, provide_ipa_app: str | None, refresh_artwork: bool, json_mode: bool, **_: Any
) -> None:
    _do_artwork(
        cfg,
        app_name=provide_ipa_app,
        refresh=refresh_artwork,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_check(*, cfg: Config, json_mode: bool, **_: Any) -> int:
    ok = _do_check(cfg, emitter=JsonEmitter() if json_mode else None)
    return 0 if ok else 3


def _verb_build_apps(
    *,
    cfg: Config,
    apps: list[str],
    dry_run: bool,
    skip_install: bool,
    for_playcover: bool,
    skip_decrypt_check: bool,
    verbose: bool,
    device: str | None,
    json_mode: bool,
    **_: Any,
) -> int:
    """What `graft` does when no verb flag was given: build the named apps.

    Not in `_VERBS` because it has no flag to be selected by — it is what the
    order falls through to, and `main` names it in exactly one place for that
    reason. The usage error lives here rather than in `main` because "no verb
    and no app names" is a malformed *build*, and it has to stay below the
    stray-flag gate: `graft --keep 3` is a bad flag, not a missing app.
    """
    if not apps:
        console.print("usage: graft APP [APP ...]   (see --help)")
        return 64

    # Build pipeline for each requested app. Nothing about a build is set up
    # here: `_build_one` opens log.sqlite inside its own envelope, because
    # opening it is a `mkdir` plus an `executescript` in the state directory and
    # a state directory that won't take writes has to be reported as a failed
    # build rather than as a traceback before the first app is attempted.
    overall_ok = True
    em = JsonEmitter() if json_mode else None
    for app_name in apps:
        try:
            _build_one(
                cfg=cfg,
                app_name=app_name,
                dry_run=dry_run,
                skip_install=skip_install,
                for_playcover=for_playcover,
                skip_decrypt_check=skip_decrypt_check,
                verbose=verbose,
                device=device,
                emitter=em,
            )
        except Exception as e:
            # `Exception`, matching what `_build_one` re-raises. Catching only
            # GraftError here meant an OSError during example's archive
            # took bravo and delta with it: neither was attempted, the
            # exit-code summary below never ran, and a --json consumer that
            # asked for three apps got one terminal event and then silence.
            # Every app the user named is attempted; the exit code answers for
            # all of them.
            overall_ok = False
            _report_app_failure(app_name, e, emitter=em)
    return 0 if overall_ok else 1


# ---------------------------------------------------------------------------
# The dispatch order
# ---------------------------------------------------------------------------

# These flags only mean something to a specific verb; Typer silently drops
# them everywhere else. That's a footgun in both directions: with no verb,
# `graft example --compression-level 9` looks like it'd change the
# build but runs a plain one with the flag discarded; and with the wrong
# verb, `--set-app-options --app example --bundle-id com.new.id` looks
# like a rename but writes nothing. So instead of asking "is *any* verb
# active?", check each flag against the set the active verb actually
# consumes — `_Verb.accepts`, declared on the verb itself so the two cannot
# drift.
#
# "Was it passed" is `is not None` here, deliberately *not* the `_asked_for`
# rule the verbs themselves are selected by. These booleans default to None, and
# `--no-keep-bundle-id` arrives as False: a flag the user typed, and one that
# `_asked_for` would call absent.
_VERB_ONLY_FLAGS: dict[str, str] = {
    "--name": "add_app_name",
    "--bundle-id": "add_app_bundle_id",
    "--display-name": "add_app_display_name",
    "--delivery": "add_app_delivery",
    "--app": "provide_ipa_app",
    "--preferred-version": "opt_preferred_version",
    "--compression-level": "opt_compression_level",
    "--keep-bundle-id": "opt_keep_bundle_id",
    "--strip-extensions": "opt_strip_extensions",
    "--strip-supported-devices": "opt_strip_supported_devices",
    "--keep": "keep",
    "--default-device": "default_device",
    "--archive-keep": "archive_keep",
    "--decrypt-mode": "decrypt_mode",
    "--decrypt-backend": "decrypt_backend",
    "--strip-encrypted-extensions": "strip_encrypted_extensions",
    "--signing-cert": "signing_cert",
    "--signing-profile": "signing_profile",
    "--signing-team-id": "signing_team_id",
    "--brief": "brief",
    "--with-icons": "with_icons",
    "--force": "force",
    "--allow-unverified": "allow_unverified",
    "--instances": "lc_instances",
    "--channel": "lc_channel",
    "--cert-only": "lc_cert_only",
}


def _asked_for(value: Any) -> bool:
    """Was the option that selects a verb given on the command line?

    One rule for two shapes, because a verb is either a boolean switch
    (`--list`, default False) or an option carrying a value (`--search`,
    default None). Identity comparisons rather than truthiness in both
    directions: `--reveal-build 0` is a build id and `--search ""` is a query
    somebody typed, and both are falsy — while a boolean switch is never None,
    so an `is not None` test alone would report every one of them as given.
    """
    return value is not None and value is not False


class _Needs(Enum):
    """What a verb wants from the config before it runs.

    Three states because there are three answers, and the boolean this replaced
    could only give two. `--describe` needs no config at all; `--set-settings`
    needs the *path* but must never have the document validated, since a config
    too broken to load is the one most in need of editing; everything else
    needs a loaded `Config`. Collapsing the first two into `needs_config=False`
    made one flag mean "needs nothing" for four verbs and "needs the path,
    unvalidated" for two — and left both settings verbs re-deriving
    `config_path or default_config_path()` for themselves.
    """

    NOTHING = auto()
    PATH = auto()
    LOADED = auto()


@dataclass(frozen=True)
class _Verb:
    """One verb flag, at its position in the order `main` considers them.

    `param` is the name of the `main` parameter that selects it, and `flag` is
    how that parameter is spelled on the command line — needed only to name the
    verb in a diagnostic, since the flag the user typed is not recoverable from
    the parsed value.

    `accepts` is the set of verb-only flags this verb consumes; see
    `_VERB_ONLY_FLAGS`. Declaring it here rather than in a second chain beside
    the guard is the point: a verb that grows a flag cannot forget to teach the
    guard about it, and a verb that loses one cannot keep accepting it.

    `migrates` is whether `main` moves the pre-rename data directories into
    place before this verb runs (`data_dirs.migrate_legacy_dirs`). True for
    every verb that can read or write the user's files — `--init` and
    `--setup-portal` included, though they need no config: run first on an
    upgraded machine, either would otherwise create `~/.config/graft` beside
    the old directory, and from then on the old one is ignored for good. Only
    the two verbs that describe the program rather than the user's data skip it.
    """

    flag: str
    param: str
    run: Callable[..., int | None]
    needs: _Needs = _Needs.LOADED
    accepts: frozenset[str] = frozenset()
    migrates: bool = True

    def selected(self, opts: dict[str, Any]) -> bool:
        return _asked_for(opts[self.param])


def _active_verb_only_flags(opts: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The verb whose flag set applies, and the verb-only flags outside it.

    The verb is the first *selected* one that consumes any verb-only flag, not
    the first selected one full stop — `--cached-ipa` and `--delete-tweak` sit
    between verbs that do consume them and have always been passed over here,
    so `--cached-ipa X --prune-builds --keep 3` is judged against
    `--prune-builds` even though `--cached-ipa` is what will run. Reading the
    order out of `_VERBS` keeps that quirk exactly where it was: this used to be
    a second `elif` chain that had to be maintained in the same sequence as the
    dispatch below, and was.
    """
    verb = next(
        (v for v in _VERBS if isinstance(v, _Verb) and v.accepts and v.selected(opts)),
        None,
    )
    allowed = verb.accepts if verb is not None else frozenset()
    stray = sorted(
        _flag_as_typed(flag, opts[param])
        for flag, param in _VERB_ONLY_FLAGS.items()
        if opts[param] is not None and flag not in allowed
    )
    return (verb.flag if verb is not None else None), stray


#: The other half of a `--x/--no-x` pair, for the message only.
#:
#: A negated flag sets the same parameter as its positive form, so listing both
#: dict keys accused the user of passing a flag they never typed — and `sorted`
#: put the one they *didn't* type first. The value says which it was: only the
#: negative form can leave it False.
_NEGATED_FLAGS = {"--strip-encrypted-extensions": "--keep-encrypted-extensions"}


def _flag_as_typed(flag: str, value: Any) -> str:
    """`flag`, or its negative form when that is what set `value`."""
    return _NEGATED_FLAGS[flag] if value is False and flag in _NEGATED_FLAGS else flag


class _StrayFlagGate:
    """Not a verb: the position in the order where verb-only flags are checked.

    It occupies a slot in `_VERBS` because *where* it runs is behaviour. Above
    it are the verbs that consume none of these flags and have never rejected
    one — `graft --list --app example` lists every app and ignores the
    `--app`, and moving this check any higher would turn that into an error.
    Below it are the verbs the check exists for.

    "Selected" therefore means "this invocation is malformed": with nothing
    stray it falls through to the verbs beneath it exactly like a flag that
    wasn't passed. `needs` is what keeps a broken config.yaml reported ahead of
    a bad flag, which is the order it had when both were open-coded — it loads
    a config this step never reads, purely for that ordering.
    """

    flag = "(verb-only flags)"
    needs = _Needs.LOADED

    def selected(self, opts: dict[str, Any]) -> bool:
        return bool(_active_verb_only_flags(opts)[1])

    def run(self, **opts: Any) -> NoReturn:
        verb, stray = _active_verb_only_flags(opts)
        detail = (
            f"is not valid with {verb}" if verb
            else (
                "requires --add-app, --add-app-manual, --provide-ipa, "
                "--appstore-check, --appstore-get, --attach-tweak, --detach-tweak, --set-app-options, "
                "--delivery-state, --reconcile, --set-settings, --artwork, "
                "--prune-builds, --devices, --device-apps, --decrypt, "
                "--decrypt-frontmost, --delete-staged, --lc-status, --lc-install, or --lc-clean"
            )
        )
        raise _fatal(ConfigError(f"{', '.join(stray)} {detail}"))


# The whole of `main`'s control flow, in source order, which is the order it
# runs in. First match wins — given two verb flags today's behaviour is that the
# earlier one here takes it, and that is preserved rather than made an error,
# because the GUI builds argv programmatically and an error would be a new way
# for it to fail.
#
# `_Needs.NOTHING` marks the four verbs that must work on a machine that has
# never run `--init`: the GUI's contract test introspects `--describe` before
# there is a config.yaml to load, and `--init` is what creates one.
_VERBS: tuple[_Verb | _StrayFlagGate, ...] = (
    _Verb("--describe", "describe", _verb_describe, needs=_Needs.NOTHING, migrates=False),
    _Verb("--version", "version", _verb_version, needs=_Needs.NOTHING, migrates=False),
    _Verb("--init", "init", _verb_init, needs=_Needs.NOTHING),
    _Verb("--setup-portal", "setup_portal", _verb_setup_portal, needs=_Needs.NOTHING),
    # --- everything below here needs a config ---
    _Verb("--set-p12-password", "set_p12_password", _verb_set_p12_password),
    _Verb("--set-ssh-password", "set_ssh_password", _verb_set_ssh_password),
    _Verb("--decrypt-check", "decrypt_check", _verb_decrypt_check),
    _Verb("--list", "list_apps", _verb_list_apps),
    _Verb("--tweaks", "list_tweaks", _verb_list_tweaks),
    _Verb("--tweak-updates", "tweak_updates", _verb_tweak_updates),
    _Verb("--update-tweak", "update_tweak", _verb_update_tweak),
    _Verb("--search", "search", _verb_search),
    _Verb("--fetch", "fetch", _verb_fetch),
    _Verb("--add-repo", "add_repo_url", _verb_add_repo),
    _Verb("--remove-repo", "remove_repo_url", _verb_remove_repo),
    _StrayFlagGate(),
    _Verb("--add-app", "add_app_url", _verb_add_app_url, accepts=frozenset({"--delivery"})),
    _Verb(
        "--add-app-manual", "add_app_manual", _verb_add_app_manual,
        accepts=frozenset({
            "--name", "--bundle-id", "--display-name", "--delivery",
        }),
    ),
    _Verb(
        "--provide-ipa", "provide_ipa_path", _verb_provide_ipa,
        accepts=frozenset({"--app", "--allow-unverified"}),
    ),
    _Verb("--cached-ipa", "cached_ipa_app", _verb_cached_ipa),
    # App Store acquisition. Below the gate even for `--appstore-status`, which
    # consumes no verb-only flag: above it, `--appstore-status --app x` would
    # quietly ignore the `--app`, and status is not per-app.
    _Verb("--appstore-status", "appstore_status", _verb_appstore_status),
    _Verb(
        "--appstore-check", "appstore_check", _verb_appstore_check,
        accepts=frozenset({"--app"}),
    ),
    _Verb(
        "--appstore-get", "appstore_get", _verb_appstore_get,
        accepts=frozenset({"--app", "--allow-unverified"}),
    ),
    _Verb("--staged-ipas", "staged_ipas", _verb_staged_ipas),
    _Verb("--delete-staged", "delete_staged", _verb_delete_staged,
          accepts=frozenset({"--force"})),
    _Verb("--install-to-mac", "install_to_mac", _verb_install_to_mac),
    _Verb("--attach-tweak", "attach_tweak", _verb_attach_tweak, accepts=frozenset({"--app"})),
    _Verb("--detach-tweak", "detach_tweak", _verb_detach_tweak, accepts=frozenset({"--app"})),
    _Verb("--delete-tweak", "delete_tweak", _verb_delete_tweak),
    _Verb(
        "--set-app-options", "set_app_options_flag", _verb_set_app_options,
        # Deliberately NOT --name/--bundle-id: _do_set_app_options
        # has no way to rename an app or change its identity, so accepting them
        # would report success for a change that never happened.
        accepts=frozenset({
            "--app", "--display-name", "--delivery", "--preferred-version",
            "--compression-level", "--keep-bundle-id", "--strip-extensions",
            "--strip-supported-devices",
        }),
    ),
    # Ahead of --delivery-state, for the flag check as much as for the dispatch:
    # given both, the device check plus its report is strictly more than the
    # report alone. `--device` isn't in `accepts` because it isn't verb-only —
    # it's a global flag this verb happens to read.
    # `_Needs.PATH` on both: each takes the config *path* and reads or writes
    # the YAML directly. Loading a `Config` validates the whole document and
    # raises when `signing:` is incomplete, which is exactly the state this
    # editor exists to repair — routing through it made the pane inert on every
    # config that needed it. Note this is not `NOTHING`: both still require the
    # file to exist, and say so.
    #
    # They sit here, below the gate, rather than up with the `_Needs.NOTHING`
    # verbs, because both consume verb-only flags and the gate is what checks a
    # verb was given only flags it accepts. Above it, `--set-settings --app
    # example` would silently ignore the `--app`.
    #
    # `--set-settings` ahead of `--settings` for the same reason `--reconcile`
    # is ahead of `--delivery-state`: given both, writing and then reporting is
    # strictly more than reporting alone, and first match wins.
    _Verb(
        "--set-settings", "set_settings_flag", _verb_set_settings, needs=_Needs.PATH,
        accepts=frozenset({
            "--default-device", "--archive-keep",
            "--signing-cert", "--signing-profile", "--signing-team-id",
        }),
    ),
    _Verb("--settings", "list_settings", _verb_settings, needs=_Needs.PATH),
    _Verb("--reconcile", "reconcile_flag", _verb_reconcile, accepts=frozenset({"--app"})),
    _Verb(
        "--delivery-state", "delivery_state_flag", _verb_delivery_state,
        accepts=frozenset({"--app"}),
    ),
    _Verb("--remove-app", "remove_app_name", _verb_remove_app),
    _Verb("--repos", "list_repos", _verb_repos),
    _Verb("--repo-packages", "repo_packages", _verb_repo_packages),
    _Verb("--builds", "list_builds", _verb_builds),
    _Verb("--reveal-build", "reveal_build", _verb_reveal_build),
    _Verb("--reinstall-build", "reinstall_build", _verb_reinstall_build),
    _Verb("--build-log", "build_log", _verb_build_log),
    _Verb("--prune-builds", "prune_builds", _verb_prune_builds, accepts=frozenset({"--keep"})),
    _Verb("--devices", "list_devices_flag", _verb_devices, accepts=frozenset({"--brief"})),
    _Verb("--artwork", "artwork_flag", _verb_artwork, accepts=frozenset({"--app"})),
    _Verb(
        "--device-apps", "device_apps_flag", _verb_device_apps,
        accepts=frozenset({"--with-icons"}),
    ),
    # No `accepts`: `--device` is deliberately absent from every verb's
    # `accepts` set (see `_NON_VERB_PARAMS["device"]` in
    # `tests/test_cli_dispatch.py`) — it is global rather than verb-only,
    # several verbs read it and none is selected by it, so the stray-flag
    # gate never needs to know this verb consumes it.
    _Verb("--device-info", "device_info_flag", _verb_device_info),
    # LiveContainer itself (`lc_manage`). `--device` is global, as above.
    _Verb("--lc-status", "lc_status", _verb_lc_status, accepts=frozenset({"--channel"})),
    _Verb(
        "--lc-install", "lc_install", _verb_lc_install,
        accepts=frozenset({"--instances", "--channel", "--force"}),
    ),
    _Verb("--lc-push-cert", "lc_push_cert", _verb_lc_push_cert),
    _Verb("--lc-clean", "lc_clean", _verb_lc_clean, accepts=frozenset({"--cert-only"})),
    _Verb("--decrypt", "decrypt", _verb_decrypt, accepts=frozenset({"--decrypt-mode", "--decrypt-backend", "--strip-encrypted-extensions"})),
    _Verb(
        "--decrypt-frontmost", "decrypt_frontmost", _verb_decrypt_frontmost,
        accepts=frozenset({"--decrypt-mode", "--decrypt-backend", "--strip-encrypted-extensions"}),
    ),
    _Verb("--check", "check", _verb_check),
)


@app.callback()
@_reported
def main(
    ctx: typer.Context,
    apps: list[str] = typer.Argument(None, help="App name(s) from config to build & install."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would happen without executing."),
    skip_install: bool = typer.Option(False, "--skip-install", help="Build & sign only — don't install."),
    for_playcover: bool = typer.Option(False, "--for-playcover", help="Also make the build loadable on this Mac under PlayCover."),
    skip_decrypt_check: bool = typer.Option(
        False, "--skip-decrypt-check", help="Skip cryptid verification (advanced)."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Verbose output and keep temp dir."),
    device: str | None = typer.Option(None, "--device", help="UDID of target device (overrides config default)."),
    config_path: Path | None = typer.Option(None, "--config", help="Path to config.yaml."),
    list_apps: bool = typer.Option(False, "--list", help="List configured apps."),
    list_tweaks: bool = typer.Option(False, "--tweaks", help="List tweaks in tweaks_dir."),
    tweak_updates: bool = typer.Option(False, "--tweak-updates", help="Check the tweaks library for newer published versions."),
    update_tweak: str | None = typer.Option(None, "--update-tweak", help="Download the newest build of a tweak (.deb filename in tweaks_dir) and re-point the apps using it."),
    check: bool = typer.Option(False, "--check", help="Run pre-flight checks and exit."),
    init: bool = typer.Option(False, "--init", help="Interactive first-run setup."),
    setup_portal: bool = typer.Option(False, "--setup-portal", help="Configure App Store Connect API key for auto-provisioning."),
    search: str | None = typer.Option(None, "--search", help="Search canister.me for tweaks matching QUERY."),
    fetch: str | None = typer.Option(None, "--fetch", help="Download a .deb into tweaks_dir (package id or direct URL)."),
    fetch_sha: str | None = typer.Option(None, "--fetch-sha", help="Expected SHA-256 for a --fetch URL download (verified after download)."),
    add_repo_url: str | None = typer.Option(None, "--add-repo", help="Add an APT repo by URL and cache its package index."),
    remove_repo_url: str | None = typer.Option(None, "--remove-repo", help="Remove a previously added APT repo by URL."),
    add_app_url: str | None = typer.Option(None, "--add-app", help="Add an app to config.yaml from its App Store URL or numeric ID."),
    add_app_manual: bool = typer.Option(False, "--add-app-manual", help="Add an app by explicit fields (no iTunes lookup)."),
    add_app_name: str | None = typer.Option(None, "--name", help="With --add-app-manual: the YAML key / shortname (e.g. 'example')."),
    add_app_bundle_id: str | None = typer.Option(None, "--bundle-id", help="With --add-app-manual: the iOS bundle identifier (e.g. com.example.app)."),
    add_app_display_name: str | None = typer.Option(None, "--display-name", help="With --add-app-manual: display name shown in the launcher."),
    add_app_delivery: str | None = typer.Option(None, "--delivery", help="With --add-app or --add-app-manual: 'sideload' or 'livecontainer' (defaults to sideload). With --set-app-options: change the app's delivery mode."),
    provide_ipa_path: Path | None = typer.Option(None, "--provide-ipa", help="Validate a .ipa and stage it in cache_dir under the canonical filename. Requires --app."),
    provide_ipa_app: str | None = typer.Option(None, "--app", help="With --provide-ipa / --attach-tweak / --detach-tweak: the configured app name (YAML key)."),
    cached_ipa_app: str | None = typer.Option(None, "--cached-ipa", help="Report which IPA the build pipeline would use for the given app (YAML key)."),
    # --- App Store acquisition (`appstore.py`) ---
    appstore_status: bool = typer.Option(False, "--appstore-status", help="Report whether ipatool is installed and signed in and UnfairPlay is enrolled, with the Terminal commands for whatever is missing."),
    appstore_check: bool = typer.Option(False, "--appstore-check", help="Compare each app's App Store version with its cached IPA. Narrow it with --app."),
    appstore_get: bool = typer.Option(False, "--appstore-get", help="Download the App Store's latest version of --app with ipatool, decrypt it with UnfairPlay, and stage it."),
    # `bool | None` defaulting to None, like `--force`: a verb-only flag, and
    # the stray-flag gate reads "was it passed" as `opts[param] is not None`.
    allow_unverified: bool | None = typer.Option(None, "--allow-unverified", help="With --appstore-get or --provide-ipa: stage a decrypt UnfairPlay could not verify against Apple's page hashes (older apps), with a warning."),
    staged_ipas: bool = typer.Option(False, "--staged-ipas", help="Report every IPA staged in the cache, with what any strip removed."),
    delete_staged: str | None = typer.Option(None, "--delete-staged", help="Delete one staged IPA from the cache, by filename."),
    # `bool | None` defaulting to None, like `--brief` and `--with-icons`
    # below: this is a verb-only flag, and the stray-flag gate detects "was it
    # passed" as `opts[param] is not None`. A default of `False` would read as
    # passed on every invocation.
    force: bool | None = typer.Option(None, "--force", help="With --delete-staged: delete even if it is the only staged IPA an app can build from. With --lc-install: replace LiveContainers AltStore manages."),
    install_to_mac: str | None = typer.Option(None, "--install-to-mac", help="Hand one staged IPA to PlayCover to install on this Mac, by filename."),
    delivery_state_flag: bool = typer.Option(False, "--delivery-state", help="Report whether each app's last delivery still matches its config. Narrow it with --app."),
    list_settings: bool = typer.Option(False, "--settings", help="Show the global settings --set-settings can write."),
    set_settings_flag: bool = typer.Option(False, "--set-settings", help="Write global settings (see the --default-device / --archive-keep / --signing-* options)."),
    default_device: str | None = typer.Option(None, "--default-device", help="UDID the build and device commands use when none is given. Pass '' to unset."),
    archive_keep: int | None = typer.Option(None, "--archive-keep", help="How many archived builds to keep per app after each build."),
    signing_cert: str | None = typer.Option(None, "--signing-cert", help="Path to the .p12 signing certificate."),
    signing_profile: str | None = typer.Option(None, "--signing-profile", help="Path to the .mobileprovision profile."),
    signing_team_id: str | None = typer.Option(None, "--signing-team-id", help="Apple Developer team id used when signing."),
    reconcile_flag: bool = typer.Option(False, "--reconcile", help="Check the connected device for what was last delivered, then report the delivery state. Narrow it with --app."),
    attach_tweak: str | None = typer.Option(None, "--attach-tweak", help="Attach a tweak (.deb filename in tweaks_dir) to an app. Requires --app."),
    detach_tweak: str | None = typer.Option(None, "--detach-tweak", help="Detach a tweak from an app's tweaks list. Requires --app."),
    delete_tweak: str | None = typer.Option(None, "--delete-tweak", help="Delete a tweak (.deb filename in tweaks_dir), moving it to the Trash. Refuses if any app still uses it."),
    remove_app_name: str | None = typer.Option(None, "--remove-app", help="Remove an app from config. Cached IPAs and build history are kept."),
    set_app_options_flag: bool = typer.Option(False, "--set-app-options", help="Patch an app's options. Requires --app and at least one field flag."),
    opt_preferred_version: str | None = typer.Option(None, "--preferred-version", help="With --set-app-options: pin a version, or 'latest'."),
    opt_keep_bundle_id: bool | None = typer.Option(None, "--keep-bundle-id/--no-keep-bundle-id", help="With --set-app-options: preserve the original bundle id."),
    opt_strip_extensions: bool | None = typer.Option(None, "--strip-extensions/--no-strip-extensions", help="With --set-app-options: strip app extensions."),
    opt_strip_supported_devices: bool | None = typer.Option(None, "--strip-supported-devices/--no-strip-supported-devices", help="With --set-app-options: strip the supported-devices list."),
    opt_compression_level: int | None = typer.Option(None, "--compression-level", help="With --set-app-options: IPA compression level (0-9)."),
    list_repos: bool = typer.Option(False, "--repos", help="List configured APT repos."),
    repo_packages: str | None = typer.Option(None, "--repo-packages", help="List packages in an APT repo (by URL)."),
    refresh: bool = typer.Option(False, "--refresh", help="With --repo-packages: re-fetch the index before listing."),
    list_builds: bool = typer.Option(False, "--builds", help="List build history from log.sqlite."),
    builds_app: str | None = typer.Option(None, "--builds-app", help="Filter --builds output by app name."),
    builds_limit: int = typer.Option(50, "--builds-limit", help="Limit --builds output."),
    reveal_build: int | None = typer.Option(None, "--reveal-build", help="Open Finder at the archived IPA for the given build id."),
    reinstall_build: int | None = typer.Option(None, "--reinstall-build", help="Re-ship the archived IPA for a previous build (no rebuild)."),
    build_log: int | None = typer.Option(None, "--build-log", help="Print the saved JSON-Lines log for the given build id."),
    prune_builds: bool = typer.Option(False, "--prune-builds", help="Move all but the newest --keep archived IPAs per app version to the Trash."),
    keep: int | None = typer.Option(None, "--keep", help=f"With --prune-builds: archives to keep per app version (default: config archive_keep, else {DEFAULT_ARCHIVE_KEEP})."),
    list_devices_flag: bool = typer.Option(False, "--devices", help="List connected iOS devices."),
    # `bool | None` / default `None`, not the plain `bool` default-False that
    # `list_devices_flag` next door uses: this one goes in `_VERB_ONLY_FLAGS`
    # (a verb-only flag, not a verb selector), and that table's stray-flag
    # check is `opts[param] is not None` — a default of `False` would read as
    # "passed" on every invocation that doesn't type `--brief`.
    brief: bool | None = typer.Option(
        None,
        "--brief",
        help=(
            "With --devices: report only udid + connection_type, skipping the "
            "per-device lockdown handshake (name/model/iOS version). For polling "
            "whether the set of connected devices has changed."
        ),
    ),
    artwork_flag: bool = typer.Option(False, "--artwork", help="Resolve and cache app icon artwork (cached IPA, then the App Store). Narrow it with --app."),
    # One flag for one idea — "don't serve me the artwork cache" — rather than
    # a second spelling of it per verb. `--artwork` re-runs the whole cascade;
    # `--device-apps --with-icons` re-reads the phone, which is the only rung
    # it has. Both bypass the same cache under the same freshness rule
    # (`artwork._believable_sidecar`), and neither deletes anything a refresh
    # failed to replace.
    refresh_artwork: bool = typer.Option(False, "--refresh-artwork", help="With --artwork, or --device-apps --with-icons: ignore the cached icon and resolve it again. A refresh that finds nothing leaves the previously cached icon in place."),
    device_apps_flag: bool = typer.Option(False, "--device-apps", help="List user apps installed on the target device."),
    # `bool | None` defaulting to None, like `--brief` above and unlike the
    # plain-bool verb selectors: this is a verb-only flag, and the stray-flag
    # gate detects "was it passed" as `opts[param] is not None`. A default of
    # `False` would read as passed on every invocation.
    with_icons: bool | None = typer.Option(
        None,
        "--with-icons",
        help=(
            "With --device-apps: also read each app's home-screen icon off the "
            "phone, cache it, and report it as icon_path. The first run is one "
            "extra pass over the device, roughly doubling what the listing "
            "takes (measured: +4s for 67 apps over Wi-Fi); later runs ask only "
            "about ids the cache has no answer for (+0.5s on the same phone). "
            "An app SpringBoard has no icon for simply has none."
        ),
    ),
    device_info_flag: bool = typer.Option(False, "--device-info", help="Report one device's storage and battery. Narrow it with --device."),
    lc_status: bool = typer.Option(False, "--lc-status", help="Report the LiveContainers on a device (--device, else every connected one), who manages them, and the latest upstream version."),
    lc_install: bool = typer.Option(False, "--lc-install", help="Install or update LiveContainer on --device, signed so guest apps keep their logins. Refuses an AltStore-managed device without --force."),
    lc_push_cert: bool = typer.Option(False, "--lc-push-cert", help="Copy the signing .p12 into LiveContainer's Documents, for its Settings -> Import Certificate."),
    lc_clean: bool = typer.Option(False, "--lc-clean", help="Remove the certificate and the IPAs graft pushed from LiveContainer's Documents."),
    # Both `str | None` defaulting to None: verb-only flags, detected as passed
    # by `is not None` — see `force` above.
    lc_instances: str | None = typer.Option(None, "--instances", help="With --lc-install: which LiveContainers, as a comma list of 1, 2, 3 (default all three)."),
    lc_cert_only: bool | None = typer.Option(None, "--cert-only", help="With --lc-clean: remove only the certificate, keeping the IPAs still to be imported."),
    lc_channel: str | None = typer.Option(None, "--channel", help="With --lc-install / --lc-status: nightly (default, what upstream recommends) or stable."),
    decrypt: str | None = typer.Option(None, "--decrypt", help="Dump a decrypted IPA of an installed app (by bundle id) off a jailbroken device."),
    # Defaults to None, not "all": a verb-only flag is detected as "passed" by
    # `opts[param] is not None`, so a non-None default would make --decrypt-mode
    # look supplied on every command and trip the stray-flag gate. The verb
    # substitutes "all" itself.
    decrypt_mode: str | None = typer.Option(None, "--decrypt-mode", help="bagbak dump mode: all (default, full app bundle), main (binaries only), or ext."),
    decrypt_backend: str | None = typer.Option(None, "--decrypt-backend", help="Decrypt backend: frida-ios-dump (reliable, needs the app running + device SSH) or bagbak (no SSH, spawns the app)."),
    decrypt_frontmost: bool = typer.Option(False, "--decrypt-frontmost", help="Dump a decrypted IPA of whatever app is currently on screen on the device — auto-detects the foreground app, no bundle id needed. Open the app and keep it foreground first."),
    strip_encrypted_extensions: bool | None = typer.Option(None, "--strip-encrypted-extensions/--keep-encrypted-extensions", help="A dump drops app extensions it cannot decrypt (no tool can read one built for a newer iOS than the phone runs) and stages the rest — the app keeps working, minus its widgets and share sheet, and what went is always reported. `--keep-encrypted-extensions` refuses that trade and fails instead. The app's own binaries are never removed either way."),
    set_p12_password: bool = typer.Option(False, "--set-p12-password", help="Prompt for the .p12 password and store it in the macOS Keychain."),
    set_ssh_password: bool = typer.Option(False, "--set-ssh-password", help="Prompt for the device's root SSH password and store it in the macOS Keychain (needed by the frida-ios-dump decrypt backend)."),
    decrypt_check: bool = typer.Option(False, "--decrypt-check", help="Report whether the device can decrypt an app right now, and what is blocking it."),
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
    describe: bool = typer.Option(
        False,
        "--describe",
        hidden=True,
        help="Emit the machine-readable CLI↔GUI contract as one JSON line and exit.",
    ),
    json_mode: bool = typer.Option(
        False, "--json", help="Emit newline-delimited JSON events on stdout for machine consumption."
    ),
) -> None:
    """Dispatch one verb, or build the apps named positionally.

    This signature is the CLI↔GUI contract: `--describe` derives its flag list
    from Click's own parameter objects, which are built from exactly these
    declarations. Nothing else here is allowed to be a source of truth about
    what the CLI accepts.

    The body is a walk down `_VERBS`, which is where the ordering — the four
    verbs that predate a config, the load, the verb-only flag gate, and the
    rest — is written down. It was forty-nine `if`s, and the order they ran in
    was load-bearing in at least three places that only a careful reader would
    have found.
    """

    # Every option this callback was given, by parameter name, and the first
    # statement so that nothing else can be in it. `locals()` rather than
    # `ctx.params`: Click's copy holds pre-conversion values — `apps` is a tuple
    # there and a list here — so a verb reading it would get something subtly
    # different from what the annotations above promise.
    #
    # Copied, not aliased. `pyproject.toml` allows 3.11, and before PEP 667
    # (3.13) `locals()` in a function returns the frame's live `f_locals`
    # snapshot, refreshed in place by anything that materialises the frame
    # again — a nested `locals()`/`vars()`, `inspect.currentframe().f_locals`,
    # a pdb session, a tracing profiler. One of those between here and the
    # dispatch below puts `cfg` into `opts`, and `verb.run(cfg=cfg, **opts)`
    # then raises `TypeError: got multiple values for keyword argument 'cfg'`:
    # every CLI invocation dies with a traceback, on an interpreter this suite
    # cannot reproduce because it runs on 3.13, where `locals()` is already a
    # snapshot. `test_the_option_bundle_is_a_copy_of_the_frame` pins the
    # spelling instead, since only the spelling is checkable from here.
    opts = dict(locals())

    # A --for-playcover artifact is Mac-only: prepare_ipa retargets a tweak's
    # dylib at a *stub* framework, and iOS has the real one. Installing that on
    # a phone silently breaks the tweak that wanted it — the precise thing
    # playcover.py refuses to do to a phone-bound build.
    #
    # Refused rather than quietly implying --skip-install: this changes what
    # the artifact *is*, and a flag that silently suppresses an install the
    # user asked for is its own surprise. Found in review; nothing in the flag,
    # the pipeline or the tests prevented it.
    if for_playcover and not skip_install:
        raise ConfigError(
            "--for-playcover builds an artifact only this Mac can run",
            hint="pass --skip-install too, then hand the build over with "
            "--install-to-mac. Installing it on a phone would point the tweak "
            "at a stub framework iOS already has for real",
        )

    # Resolve the verb first, then load a config only if that verb needs one.
    #
    # This used to load on *reaching* the first config-needing verb rather than
    # on selecting it, which made "runs without a config" mean "and nothing
    # above me needs one either" — a property of a verb's position in this list
    # rather than of the verb. `--set-settings` has to run against a config too
    # broken to load (that is what it repairs) while sitting below the
    # stray-flag gate (that is what checks its flags), and those two are only
    # compatible once the flag means what it says.
    #
    # The documented ordering survives: a config that won't load is still
    # reported ahead of a stray-flag error, because the gate itself needs a
    # loaded one, and ahead of a default build, because that path needs one too.
    selected = next((verb for verb in _VERBS if verb.selected(opts)), None)
    # Before anything resolves a path: the directories were `…/tweak-build`
    # before the rename, and every default below answers whichever one holds
    # the user's files. One line per directory moved (or that could not be),
    # once — the second run finds the symlink and has nothing to say. Beside
    # them, one line per renamed environment variable still set by its old
    # name (`legacy_env`).
    if getattr(selected, "migrates", True):
        for notice in [*migrate_legacy_dirs(), *legacy_env_notices()]:
            _warn(JsonEmitter() if json_mode else None, notice)
    # Resolved once, here, rather than by every `_Needs.PATH` verb for itself —
    # both were spelling `config_path or default_config_path()` in their own
    # bodies, and a third would have had to remember to.
    opts["config_path"] = config_path = config_path or default_config_path()
    cfg: Config | None = None
    if selected is None or selected.needs is _Needs.LOADED:
        cfg = load_config(config_path)
    if selected is not None:
        code = selected.run(cfg=cfg, **opts)
        raise typer.Exit(code=0 if code is None else code)

    # No verb flag. `cfg` is loaded — the walk above passed every verb that
    # needs one — and a build is what this CLI does by default.
    raise typer.Exit(code=_verb_build_apps(cfg=cfg, **opts))


def _report_app_failure(app_name: str, exc: Exception, *, emitter: JsonEmitter | None) -> None:
    """The human summary of one failed app in a multi-app build.

    `_build_one` has already emitted the machine-readable `build-end` for this
    app, so under --json this is duplicate detail — and detail printed on
    stdout would land in the middle of the event stream. It goes to stderr
    there, which is where the GUI reads failures from anyway.

    Escaped like `_fatal`: this is the multi-app loop's error boundary, and it
    runs inside the `except` that keeps one app's failure from taking the rest
    down. A MarkupError raised here would escape that handler and end the run
    — the failure of app 1 skipping apps 2 and 3, which is the exact bug the
    widened `except Exception` above was added to fix.
    """
    if isinstance(exc, GraftError):
        message, hint = exc.message, exc.hint
    else:
        message, hint = f"{exc.__class__.__name__}: {exc}", None
    out = err_console if emitter else console
    out.print(f"[red]✗ {escape(app_name)} failed:[/red] {escape(message)}")
    if hint:
        out.print(f"  [yellow]hint:[/yellow] {escape(hint)}")


def _record_delivery_if_pushed(
    log: BuildLog,
    *,
    app_name: str,
    inputs: InputsDigest | None,
    ipa_path: Path | None,
    lc_bundle_id: str | None,
    remote_name: str | None,
    udid: str | None,
    dry_run: bool,
    emitter: JsonEmitter | None = None,
) -> None:
    """Record that this build reached a device.

    Called from the push sites and nowhere else. "Built but never delivered" is
    one of the states this table exists to surface, so a build that ships
    nothing — `--skip-install`, a failure before stage 5, a push that throws —
    must leave no row behind. A dry run walks the whole push block without
    moving a byte, so it is caught here rather than by placement.

    `inputs` is fingerprinted by the caller before cyan runs, never re-derived
    here: see the comment at that site. Everything the row claims about the
    build is read off that one digest, so the row and the digest it is stored
    beside cannot describe different things.

    Never raises: a failure to write history must not fail a build that already
    shipped. The cost of losing a row is a spurious "stale" badge, which is the
    safe direction — the same trade the retention guard in `_build_one` makes.
    """
    if dry_run or inputs is None:
        return
    try:
        log.record_delivery(
            app=app_name,
            bundle_id=inputs.bundle_id,
            inputs_digest=inputs.digest,
            # The digest's own format version, not a constant read back at
            # comparison time: it is what lets a later run say "an older
            # version of this tool delivered that".
            digest_version=inputs.version,
            cyan=inputs.cyan,
            tweaks=[[t.name, t.sha256] for t in inputs.tweaks],
            source_version=inputs.source_version,
            ipa_sha256=sha256_of(ipa_path) if ipa_path and ipa_path.exists() else None,
            delivery=inputs.delivery,
            lc_bundle_id=lc_bundle_id,
            remote_name=remote_name,
            udid=udid,
        )
    except Exception as exc:
        _warn(emitter, f"couldn't record delivery: {exc}")


def _build_one(
    *,
    cfg: Config,
    app_name: str,
    dry_run: bool,
    skip_install: bool,
    skip_decrypt_check: bool,
    verbose: bool,
    device: str | None,
    for_playcover: bool = False,
    emitter: JsonEmitter | None = None,
    trash_dir: Path | None = None,
    log: BuildLog | None = None,
) -> None:
    """Build one app, start to finish, entirely inside the report envelope.

    `log` is an injection seam for tests; `main` leaves it None so log.sqlite is
    opened inside the envelope, where a failure to open it is reportable.
    """
    # `.get`, not `cfg.get_app`. A `with` header is evaluated before `__enter__`
    # runs, so a lookup that raises here is outside the envelope just as surely
    # as one on the line above it — which is what this was: `cfg.get_app` on the
    # first line of the function meant an app deleted from config.yaml while the
    # GUI was open raised ConfigError with `build-start` never emitted. Zero JSON
    # events, an empty transcript and no history row, from a class of failure the
    # re-install path had already been fixed for. The real resolution, message
    # and hint included, is the first statement inside the block.
    app_cfg = cfg.apps.get(app_name)
    with _BuildReport(
        emitter,
        app_name=app_name,
        # Empty rather than absent when the app is gone: `build-start` says
        # which app is building, and that is knowable from argv alone.
        bundle_id=app_cfg.bundle_id if app_cfg else "",
        delivery=app_cfg.delivery if app_cfg else None,
        dry_run=dry_run,
        for_playcover=for_playcover,
    ) as report:
        app_cfg = cfg.get_app(app_name)
        report.tweaks = list(app_cfg.tweaks)
        # Inside the envelope so `build-end` carries the refusal: building
        # LiveContainer the sideload way signs it with zsign and breaks every
        # guest app's login. `--lc-install` is the way now.
        from .lc_manage import refuse_legacy_build

        refuse_legacy_build(app_cfg)
        # Opened here, not by the caller. `BuildLog.__init__` does a `mkdir` and
        # an `executescript` in the same state directory the tee lives in, so a
        # read-only or full volume makes it raise — and constructed in `main`
        # ahead of the loop it raised before any app was attempted, producing no
        # build event for any of them. That is the exact scenario `open_tee` was
        # hardened for, reached through a different door.
        if log is None:
            log = BuildLog(cfg.state_dir / "log.sqlite")
        report.log = log

        console.rule(f"[bold]{escape(app_name)}[/bold] — {escape(app_cfg.bundle_id)}")

        # Tee every JSON event to a per-build .jsonl so the GUI can replay it
        # later. Only meaningful when --json is in use; non-JSON runs leave no
        # machine-readable artefact.
        if emitter is not None and not dry_run:
            report.open_tee(cfg.state_dir / "build-logs")

        # `tmp_root` is read by the finally block, so it is initialised BEFORE
        # the try: a failure ahead of stage 2.5 must not trip an
        # UnboundLocalError on the way out. What a *failed* build reports is no
        # longer read off these — the envelope has it — so nothing here has to
        # stay in step with the failure path.
        cached: CachedIPA | None = None
        tmp_root: Path | None = None
        keep_tmp = verbose
        try:
            # Pre-flight — only the checks this app's delivery mode actually needs.
            # (The full, mode-agnostic check set is available via `--check`.)
            checks = run_all_checks(
                cfg, delivery=app_cfg.delivery, will_install=not skip_install, app=app_cfg
            )
            failures = [c for c in checks if not c.ok]
            if failures:
                if not emitter:
                    _print_checks(checks)
                from .errors import PreflightError
                names = ", ".join(c.name for c in failures)
                raise PreflightError(
                    f"{len(failures)} pre-flight check(s) failed: {names}",
                    hint="fix the items marked FAIL and re-run",
                )

            # Stage 2: acquire IPA
            console.print("→ stage 2: acquiring IPA")
            if emitter:
                emitter.emit("stage", name="acquire", status="start")
            cached = acquire_ipa(cfg, app_cfg)
            # Handed to the envelope as soon as it is known, so a failure from
            # here on is recorded against the version it was actually building
            # rather than "unknown".
            report.version = cached.version
            console.print(f"   using [cyan]{cached.path}[/cyan] (v{cached.version})")
            # Said on every build that reuses one, not only on the run that
            # made it. A stripped IPA is staged under the same canonical name a
            # complete dump uses and reads identically to every consumer, so
            # this is the only place a user can learn that the app they are
            # about to build has no widgets — days later, from the GUI, or as
            # somebody else.
            removed = stripped_extensions(cached.path)
            if removed:
                _warn(
                    emitter,
                    f"this IPA had {len(removed)} app "
                    f"extension{'s' if len(removed) != 1 else ''} removed to decrypt "
                    f"it — the build will not have "
                    f"{'them' if len(removed) != 1 else 'it'}: " + ", ".join(removed),
                )
            if emitter:
                emitter.emit(
                    "stage", name="acquire", status="end", path=str(cached.path),
                    version=cached.version,
                )

            # Stage 2.5: decryption verification
            tmp_root = Path(tempfile.mkdtemp(prefix=f"graft-{app_name}-"))
            if not skip_decrypt_check:
                console.print("→ stage 2.5: verifying decryption (cryptid 0)")
                if emitter:
                    emitter.emit("stage", name="verify", status="start")
                extract_dir = tmp_root / "verify"
                extract_ipa(cached.path, extract_dir)
                results = verify_decrypted(extract_dir)
                bad = [r for r in results if not r.is_decrypted]
                if bad:
                    for r in bad:
                        console.print(
                            f"  [red]✗[/red] {r.binary.relative_to(extract_dir)} cryptid={r.cryptids}"
                        )
                    # The one machine-readable form of this. It used to ride in
                    # the `stage` event's extra fields as `bad=<n>`, which
                    # `BuildEvent.progress` drops, and otherwise existed only as
                    # console text on the same stdout as the JSON stream — where
                    # the GUI's line decoder throws it away. The build stayed
                    # fully green while the user pushed an IPA that crashes on
                    # launch, with nothing anywhere saying why.
                    if emitter:
                        emitter.emit(
                            "encrypted-binaries",
                            app=app_name,
                            count=len(bad),
                            binaries=[
                                {
                                    "path": str(r.binary.relative_to(extract_dir)),
                                    "cryptids": list(r.cryptids),
                                }
                                for r in bad
                            ],
                        )
                    _warn(
                        emitter,
                        carried_by="encrypted-binaries",
                        message=f"{len(bad)} of {len(results)} Mach-O binaries are still encrypted — "
                        f"{app_name} will likely fail to launch. Re-dump from a jailbroken "
                        "device or get a freshly-decrypted IPA.",
                    )
                else:
                    console.print(f"   [green]✓[/green] {len(results)} Mach-O binaries all cryptid 0")
                if emitter:
                    emitter.emit("stage", name="verify", status="end", binaries=len(results), bad=len(bad))

            # Fingerprint the build inputs HERE, immediately before cyan consumes
            # them — not at recording time. Recording happens after stage 3, stage
            # 4 and the whole stage-5 transfer; re-reading the tweak files then
            # would let the row attest to bytes that were never injected, so a
            # tweak rebuilt during those minutes would be reported as delivered
            # while the phone holds the previous build of it. That is a false
            # `current`, the one direction this feature must never fail in.
            #
            # Taken ahead of cyan the residual error runs the safe way instead: a
            # tweak that changes in the gap is recorded at its older hash and the
            # next check over-reports `stale`. Keep this adjacent to `run_cyan`;
            # anything inserted between the two widens that gap.
            #
            # Deliberately unconditional, though a run that pushes nothing has no
            # use for it: skipping it on those runs would mean a recording call
            # misplaced onto the build path quietly found `inputs=None` and wrote
            # no row, so the one test that catches that misplacement would pass. A
            # few hashes of small .deb files is a cheap price for keeping "only the
            # push writes a row" the single thing holding that property up.
            inputs: InputsDigest | None = None
            try:
                inputs = compute_inputs_digest(cfg, app_cfg, source_version=cached.version)
            except Exception as exc:
                # Bookkeeping must not fail a build, on this side of it either.
                _warn(emitter, f"couldn't fingerprint build inputs: {exc}")

            # Stage 3: cyan
            console.print("→ stage 3: injecting tweaks with cyan")
            if emitter:
                emitter.emit("stage", name="cyan", status="start")
            tweaked = tmp_root / f"{app_name}-tweaked.ipa"
            cy = run_cyan(cfg, app_cfg, cached.path, tweaked, dry_run=dry_run)
            tweaked = cy.output_ipa
            if dry_run:
                console.print(f"   [dim]{cy.stdout}[/dim]")
            else:
                console.print(f"   [green]✓[/green] {tweaked.name}")
            if emitter:
                emitter.emit("stage", name="cyan", status="end")
            _emit_log_lines(emitter, "cyan", cy.stdout)
            _emit_log_lines(emitter, "cyan", cy.stderr)

            # Opt-in, and it has to be: iOS genuinely has the private
            # frameworks this retargets, so doing it to a phone-bound build
            # would swap a real framework for a stub and break the tweak that
            # wanted it. Runs after cyan because it operates on the injected
            # dylibs, and before stage 4 so the archive that gets kept is the
            # one that actually loads.
            if for_playcover and not dry_run:
                fixed = _prepare_for_playcover(tweaked, emitter)
                for line in fixed:
                    console.print(f"   [green]✓[/green] {escape(line)}")
                # Reported as log lines rather than a new event type. The event
                # contract is a symmetric partition asserted across both
                # languages, so a new name means a matching Swift case in the
                # same commit — worth it for something the GUI branches on, and
                # not for a line it would only print.
                _emit_log_lines(emitter, "playcover", "\n".join(fixed))
                if not fixed:
                    console.print(
                        "   [dim]nothing to stub — every injected dylib already "
                        "links frameworks macOS has[/dim]"
                    )

            # Stage 4 + 5 differ by delivery mode. What each produces goes
            # straight onto the report rather than into a local that a later
            # edit could forget to copy across: the envelope reads these when it
            # writes the history row, and a lost `ipa_path` is a build with no
            # Re-install button.
            if app_cfg.delivery == "livecontainer":
                # Skip signing entirely; LiveContainer runs the tweaked IPA unsigned.
                console.print("→ stage 4: archiving tweaked IPA (delivery=livecontainer)")
                if emitter:
                    emitter.emit("stage", name="archive", status="start")
                if not dry_run:
                    cfg.built_dir.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%Y%m%dT%H%M%S")
                    report.ipa_path = cfg.built_dir / f"{app_name}_{cached.version}_{ts}.ipa"
                    shutil.copy2(tweaked, report.ipa_path)
                    console.print(f"   archived to [cyan]{report.ipa_path}[/cyan]")
                if emitter:
                    emitter.emit("stage", name="archive", status="end", path=str(report.ipa_path) if report.ipa_path else None)

                if not skip_install:
                    # The transport `select_device` already resolved, rather
                    # than "USB" asserted regardless. usbmuxd enumerates
                    # Wi-Fi-paired devices too, and this line claimed a cable
                    # while pushing 117 MB over the network — a build log that
                    # misdescribes what it did is the thing this tool is for.
                    report.udid, transport = select_device(device or cfg.default_device_udid)
                    console.print(
                        f"→ stage 5: pushing into LiveContainer over {_transport_label(transport)}"
                    )
                    if emitter:
                        emitter.emit("stage", name="lc-push", status="start", transport=transport)
                    lc_result = lc_runner.push_ipa_to_livecontainer(
                        report.ipa_path if report.ipa_path else tweaked,
                        udid=report.udid,
                        inner_filename=f"{app_name}.ipa",
                        lc_bundle_id=_lc_target(app_cfg),
                        dry_run=dry_run,
                    )
                    if dry_run:
                        console.print(
                            f"   [dim]would push {lc_result.size_bytes} bytes "
                            f"to {lc_result.bundle_id}:{lc_result.documents_path}[/dim]"
                        )
                    else:
                        console.print(
                            f"   [green]✓[/green] {lc_result.size_bytes/1_000_000:.1f} MB into "
                            f"{lc_result.bundle_id} on {report.udid}"
                        )
                        console.print(
                            "   [dim]open LiveContainer on the phone, the new IPA appears in its "
                            "Documents tab and can be added as a managed app[/dim]"
                        )
                        _report_lc_choice(emitter, app_name=app_name, result=lc_result)
                    if emitter:
                        emitter.emit(
                            "stage", name="lc-push", status="end",
                            bundle_id=lc_result.bundle_id, size_bytes=lc_result.size_bytes,
                            others=lc_result.others,
                        )
                    # Inside `if not skip_install`, after the push returned: this is
                    # the only point at which an artifact is known to have reached
                    # the phone. Anywhere later (next to log.record) would also fire
                    # for builds that were never delivered.
                    _record_delivery_if_pushed(
                        log, app_name=app_name, inputs=inputs, ipa_path=report.ipa_path,
                        lc_bundle_id=lc_result.bundle_id,
                        # The name lc_runner reports writing, not a second copy of
                        # the f-string above it: Task 6 reconciles by looking for
                        # this file on the device, so a drift between the two would
                        # demote an app over a path we never wrote to.
                        remote_name=Path(lc_result.documents_path).name,
                        udid=report.udid, dry_run=dry_run, emitter=emitter,
                    )
            else:
                # Classic sideload: zsign + ideviceinstaller.
                console.print("→ stage 4: signing with zsign")
                if emitter:
                    emitter.emit("stage", name="sign", status="start")
                signed = tmp_root / f"{app_name}-signed.ipa"
                sg = run_zsign(cfg, tweaked, signed, app=app_cfg, dry_run=dry_run)
                for note in sg.profile_notes:
                    # An expired or device-stale auto-provisioning cache is
                    # refreshed silently by `ensure_profile_for_bundle` — which
                    # is what makes the old dead end escapable — but a round
                    # trip to Apple that nothing mentions is one the user has to
                    # reverse-engineer later. Same for a configured device ASC
                    # does not have: the build carries on without it rather than
                    # refusing to run, so the only thing standing between the
                    # user and a mystery "install failed" on that phone is this
                    # line.
                    _warn(emitter, note)
                if dry_run:
                    console.print(f"   [dim]{sg.stdout}[/dim]")
                else:
                    console.print(
                        f"   [green]✓[/green] team={sg.team_id or '?'} "
                        f"subject={sg.cert_subject or '?'} expiry={sg.expiry or '?'}"
                    )
                    report.signing_team = sg.team_id
                if emitter:
                    emitter.emit("stage", name="sign", status="end")
                _emit_log_lines(emitter, "zsign", sg.stdout)
                _emit_log_lines(emitter, "zsign", sg.stderr)

                if not dry_run:
                    cfg.built_dir.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%Y%m%dT%H%M%S")
                    report.ipa_path = cfg.built_dir / f"{app_name}_{cached.version}_{ts}.ipa"
                    shutil.copy2(signed, report.ipa_path)
                    console.print(f"   archived to [cyan]{report.ipa_path}[/cyan]")

                if not skip_install:
                    console.print("→ stage 5: installing via ideviceinstaller")
                    if emitter:
                        emitter.emit("stage", name="install", status="start")
                    report.udid, transport = select_device(device or cfg.default_device_udid)
                    ir = install(
                        signed if report.ipa_path is None else report.ipa_path,
                        udid=report.udid,
                        transport=transport,
                        dry_run=dry_run,
                    )
                    if dry_run:
                        console.print(f"   [dim]{ir.stdout}[/dim]")
                    else:
                        console.print(f"   [green]✓[/green] installed on {report.udid or '(default device)'}")
                    if emitter:
                        emitter.emit("stage", name="install", status="end")
                    _emit_log_lines(emitter, "install", ir.stdout)
                    _emit_log_lines(emitter, "install", ir.stderr)
                    _record_delivery_if_pushed(
                        log, app_name=app_name, inputs=inputs, ipa_path=report.ipa_path,
                        lc_bundle_id=None, remote_name=None, udid=report.udid,
                        dry_run=dry_run, emitter=emitter,
                    )

            console.print(
                f"[bold green]✓ {app_cfg.cyan.display_name or app_name}[/bold green] "
                f"({app_cfg.bundle_id}) {cached.version} in {report.elapsed:.1f}s"
            )
            # Retention, on the success path only: a failed build archives nothing,
            # and sweeping after one would delete the history a user is mid-way
            # through debugging. This build's own archive is excluded so it is
            # never a candidate, even at `archive_keep: 0`.
            #
            # Ahead of build-end so the prune is part of the build stream a
            # consumer sees, rather than a trailer after the event that means
            # "done"; and ahead of the history row because the only rows it
            # clears belong to older archives, which are already on disk.
            if not dry_run and report.ipa_path is not None:
                try:
                    _prune_archives(
                        cfg,
                        keep=cfg.archive_keep,
                        dry_run=False,
                        exclude=[report.ipa_path],
                        emitter=emitter,
                        trash_dir=trash_dir,
                    )
                except Exception as prune_error:
                    # Housekeeping must never turn a shipped build into a failed
                    # one: this block sits inside the envelope, which would
                    # report a shipped build as a failure and tell the GUI the
                    # install never happened.
                    _warn(emitter, f"archive retention failed: {prune_error}")
            # No build-end and no log.record here: both belong to the envelope,
            # which emits the terminal event last and exactly once. Emitting it
            # here — ahead of the history write — is what let a locked database
            # turn a shipped build into "✗ database is locked" via a second,
            # contradicting build-end.
        finally:
            if tmp_root is not None:
                if keep_tmp:
                    console.print(f"  [dim]temp kept at {tmp_root}[/dim]")
                else:
                    shutil.rmtree(tmp_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(app(prog_name="graft"))
