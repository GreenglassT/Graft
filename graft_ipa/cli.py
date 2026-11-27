"""CLI entry point and pipeline orchestration."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import functools
import inspect
import json
import os
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, NoReturn, TextIO

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from . import (
    __version__,
    app_update,
    appstore,
    appstore_search,
    artwork,
    canister,
    capabilities,
    deb,
    decrypt_runner,
    developer_mode,
    device_apps,
    device_session,
    devices_seen,
    host,
    ipatool,
    itunes,
    lc_runner,
    playcover,
    playcover_handoff,
    release_check,
    repo,
    stop_deferral,
    storefronts,
    tweak_sources,
    update_tweaks,
    updates,
)
from . import ipa_inspect as ipa_inspect_module
from . import provide_ipa as provide_ipa_module
from .build_log import BuildLog, _target_of, sha256_of
from .config import (
    ACCOUNT_FREE,
    DEFAULT_ARCHIVE_KEEP,
    SETTINGS,
    AppConfig,
    Config,
    _expand,
    _load_raw,
    add_app,
    app_key,
    add_repo,
    default_built_dir,
    default_cache_dir,
    default_config_path,
    default_state_dir,
    default_tweaks_dir,
    load_config,
    paid_account_required,
    read_app_store_connect,
    read_settings,
    remove_app,
    remove_repo,
    set_app_options,
    set_app_tweaks,
    set_settings,
    swapped_tweaks,
)
from .child_env import child_env
from .cyan_runner import run_cyan
from .data_dirs import (
    migrate_legacy_dirs,
    private_copy,
    secure_app_dirs,
    sweep_stale_partials,
)
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
    DeviceNotConnectedError,
    DeviceNotRegisteredError,
    GraftError,
    NoDeviceConnectedError,
    SeveralDevicesError,
    StagedIPAInUseError,
    Stopped,
    TweakInUseError,
    TweakNotFoundError,
    presented,
)
from .init_flow import run_init
from .installer import InstallError, Target, install, resolve_target, select_device
from .ipa_cache import (
    CachedIPA,
    _cache_candidates,
    _is_cache_candidate,
    acquire_ipa,
    admits,
    find_in_cache,
)
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
from .preflight import (
    deliveries_in_use,
    run_all_checks,
    run_export_checks,
    run_free_account_checks,
)
from .retention import Archive, plan_prune
from .signer import run_zsign
from .trash import move_to_trash

if TYPE_CHECKING:
    from . import update_run

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
# color when stderr isn't a TTY (i.e. when piped).
err_console = _PipeAwareConsole(stderr=True)


@contextlib.contextmanager
def _narration_on_stderr(json_mode: bool) -> Iterator[None]:
    """Under --json, send `console`'s output to stderr for the duration.

    A build narrates itself through `console` — the app heading, each stage,
    the archive path, the summary — and `console` writes to stdout, which
    under --json *is* the event stream. The GUI's line decoder drops anything
    that is not JSON, so it never showed; but the stream was not the one the
    README promises, and any consumer that parses every line (`| jq`) broke
    on the first. Redirected rather than silenced: a person running
    `graft --json …` in a terminal still sees it, on stderr, where the
    warnings already go. One switch around the whole verb, so a `print` added
    to a helper the build calls is covered without anyone remembering to.

    `main` makes the same switch around every verb that answers --json with
    events (`_speaks_json`): only the build and the re-install made it, so
    `--install-to-mac`, reaching the same `_without_account_data` they do,
    printed its "removing Apple ID data" line into the stream.
    """
    if not json_mode:
        yield
        return
    previous = console.stderr
    console.stderr = True
    try:
        yield
    finally:
        console.stderr = previous


def _warn(
    emitter: JsonEmitter | None, message: str, *, carried_by: str | None = None,
    startup: bool = False,
) -> None:
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
    the interpolated half is what keeps the `warning:` prefix colored; the
    `log` event carries the original text because JSON has no markup.

    `startup` marks a notice given before any verb runs — a directory moved
    to its post-rename name, a renamed environment variable still set by its
    old one — whose `log` event has `source: startup` instead (C-NOTICE).
    Those lines go out ahead of whatever the verb emits, and a store reading
    the verb's `log` events as its results cannot tell them apart by
    position: the app's device check showed "TB_SSH_PASS is set but no
    longer read" as the outcome of every Check device. Stores ignore
    `startup` for results; stderr gets it as any other warning.
    """
    if carried_by is not None and carried_by not in GUI_EVENTS:
        # Checked before the suppression it authorizes, and raised rather than
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
        if startup:
            emitter.emit("log", source="startup", line=message)
        else:
            emitter.emit("log", source="warning", line=message)
    err_console.print(f"[yellow]warning:[/yellow] {escape(message)}")


def _warn_ungranted_capabilities(
    emitter: JsonEmitter | None, app_cfg: AppConfig, ipa: Path, profile: Path,
) -> None:
    """Stage 4's warning, before zsign runs: what `ipa`'s own binaries ask
    for that `profile` doesn't grant (`capabilities`). zsign signs the
    profile's entitlements, so those are gone on the device; an app that
    asks for an app group and gets none traps at launch. Warned, not
    refused: the build is the user's call. Extensions are left out when
    cyan removes them (`strip_extensions`): they won't be on the device. So
    is the Watch app's when cyan removes the Watch app (`-w`,
    `cyan_runner.removes_watch`)."""
    from .cyan_runner import removes_watch

    gaps = capabilities.missing_for(ipa, profile, extensions=not app_cfg.cyan.strip_extensions,
                                    watch=not removes_watch(app_cfg.cyan))
    label = app_cfg.cyan.display_name or app_cfg.name
    for line in capabilities.warnings(label, app_cfg.name, gaps):
        _warn(emitter, line)


def _print_checks(results) -> None:
    table = Table(title="pre-flight checks", show_lines=False)
    table.add_column("check", style="bold")
    table.add_column("status")
    table.add_column("detail / hint")
    for r in results:
        color = "cyan" if r.info else "green" if r.ok else "red"
        detail = r.detail if r.ok else (r.detail + ("  " + r.hint if r.hint else ""))
        table.add_row(r.name, f"[{color}]{r.status}[/{color}]", detail)
    console.print(table)


def _do_check(cfg: Config, emitter: JsonEmitter | None = None) -> bool:
    """Run pre-flight checks. Returns True if all checks passed.

    When an emitter is provided, emits structured JSON events instead of Rich output.

    A free Apple ID gets its own set (`run_free_account_checks`): the account
    as an `info` row, no signing rows, and whether a LiveContainer is on the
    device. `preflight-end` carries `account` either way. No device
    connected is an `info` row too: `--check` installs nothing
    (`preflight.check_devices`).
    """
    # Paid: the tools the configured apps' deliveries run, not sideload's for
    # everyone (`preflight.deliveries_in_use`).
    results = (
        run_free_account_checks(cfg) if cfg.free_account
        else run_all_checks(cfg, deliveries=deliveries_in_use(cfg), device_required=False)
    )
    if emitter:
        for r in results:
            # `install` on every row, a command or null: what the app's Copy
            # button copies, so it never has to parse "install with: ".
            payload = {"name": r.name, "ok": r.ok, "detail": r.detail or None,
                       "install": None if r.ok else r.install}
            if not r.ok and r.hint:
                payload["hint"] = r.hint
            if r.action_key:
                payload["action_key"] = r.action_key
            if r.info:
                payload["info"] = True
            if r.source:
                payload["source"] = r.source
            emitter.emit("preflight-check", **payload)
        ok_count = sum(1 for r in results if r.ok)
        emitter.emit(
            "preflight-end", ok_count=ok_count, fail_count=len(results) - ok_count,
            account=cfg.account,
        )
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
            # `sideload` or `livecontainer`, how the archive was built; null
            # for a row from before it was recorded.
            "delivery": r.get("delivery"),
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
    subprocess.run(["open", "-R", ipa_path], check=False, env=child_env())


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
            hint="a log is saved only for builds run with --json, which is how the Mac app runs them; a build started from Terminal without --json leaves none",
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
    delivery: str | None = None,
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
        delivery=delivery,
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
        export_to: Path | None = None,
        waits: Callable[[BaseException | None], str | None] | None = None,
    ) -> None:
        self._emitter = emitter
        self._app_name = app_name
        # Under `graft --update` (`update_run.Built.waits`): the sentence its
        # device waits with when the failure leaves it waiting, which
        # `build-end` then says rather than the failure (T2-B7).
        self._waits = waits
        self._bundle_id = bundle_id
        self._delivery = delivery
        self._dry_run = dry_run
        # `--export-ipa`'s path, for `build-start`: the stream says from its
        # first line that this run writes a file and delivers nothing. Known
        # from argv, like `delivery`; checked inside the block.
        self._export_to = export_to
        # Passed in rather than filled in from inside the block, like `delivery`
        # and `dry_run` and unlike `version`/`ipa_path`: it comes straight from
        # argv, so it is known before `build-start` and nothing about reading it
        # can raise.
        self._for_playcover = for_playcover
        self._started = 0.0
        self._entered = False
        self._tee: TextIO | None = None
        self._trust_to_tee: contextlib.AbstractContextManager[None] | None = None
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
        # This run's share of `retention.lock` (`hold_archives`), and the
        # sweep to run once it is released (`sweep_when_done`).
        self._archive_hold: _RetentionLock | None = None
        self._sweep: tuple[Config, Path | None] | None = None
        # `build-end`'s `reason` for an export that failed, set on the way out.
        self._reason: str | None = None

    @property
    def elapsed(self) -> float:
        return time.time() - self._started

    @property
    def _start_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "app": self._app_name,
            "bundle_id": self._bundle_id,
            "delivery": self._delivery,
        }
        if self._export_to is not None:
            fields["export_to"] = str(self._export_to)
        return fields

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
            # Owner-only: it records the build's paths, device and tweaks.
            handle = os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w")
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
            # The trust prompt's live event comes from the dispatch's own
            # emitter (`_trust_prompts_as_events`), which has no tee, so a
            # build replayed from Builds had no trace of it (re-check #22).
            # Written here to this file alone: stdout already has it.
            emitter = self._emitter
            self._trust_to_tee = device_session.trust_prompt_listener(
                lambda line: emitter.emit_to_tee("log", source="device", line=line)
            )
            self._trust_to_tee.__enter__()

    def hold_archives(self, cfg: Config) -> None:
        """Share `retention.lock` from now until the history row is written.

        A build calls this just before it archives its IPA, `--reinstall-build`
        before it opens the archive it re-ships; `_finish` lets go once the row
        is written (a re-install writes none, so: once it has delivered). In
        between, no sweep in any process can move this run's archive to the
        Trash — not mid-push, and not before the row naming it exists, which
        would leave a Re-install button pointing at the Trash.

        Waits out a sweep that is running, which is a handful of renames; one
        that holds the lock past `_RETENTION_LOCK_TIMEOUT` is stuck, and the
        run fails with this build's `build-end` rather than ship unguarded.
        """
        if self._archive_hold is not None:
            return
        hold = _RetentionLock(cfg)
        if not hold.acquire(exclusive=False, wait=_RETENTION_LOCK_TIMEOUT):
            raise GraftError(
                "a clean-up of old builds is holding the archive folder",
                hint="try again in a moment; if no graft is running, "
                f"the process holding {hold.path} is stopped or stuck",
            )
        self._archive_hold = hold

    def sweep_when_done(self, cfg: Config, *, trash_dir: Path | None) -> None:
        """Run this build's retention sweep once its row is written and its
        share of the lock released. Called at the end of a build that worked;
        `_finish` runs it only if the build ended that way."""
        self._sweep = (cfg, trash_dir)

    def _release_archives(self) -> None:
        hold, self._archive_hold = self._archive_hold, None
        if hold is not None:
            hold.release()

    def _run_sweep(self) -> None:
        """The build's own sweep. Warns rather than raises, always.

        After `_release_archives`, because a share and an exclusive hold of
        the same lock in one process conflict like any other two. Takes the
        lock once, without waiting: another build or a re-install sharing it
        is delivering, maybe for minutes, and this build has already shipped —
        so it skips, says so in a line, and the next build's sweep catches up.
        Its own archive is excluded, so it is never a candidate, even at
        `archive_keep: 0`. Ahead of build-end, so the prune is part of the
        build stream a consumer sees rather than a trailer after "done".
        """
        if self._sweep is None or self.ipa_path is None:
            return
        cfg, trash_dir = self._sweep
        try:
            _prune_archives(
                cfg, keep=cfg.archive_keep, dry_run=False, exclude=[self.ipa_path],
                wait=0, emitter=self._emitter, trash_dir=trash_dir,
            )
        except _RetentionBusy:
            _warn(self._emitter, "another build is delivering; old archives are cleaned up next time")
        except Exception as prune_error:
            # Housekeeping must never turn a shipped build into a failed one.
            _warn(self._emitter, f"archive retention failed: {prune_error}")

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
                # What the archive is: `--reinstall-build` refuses to ship it
                # through the other mode's door (`_archived_delivery`).
                delivery=self._delivery,
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
        # A stop is not a failure (C-STOP): the GUI's Stop arrives as
        # `Stopped`, Ctrl-C as `KeyboardInterrupt`. Neither writes a history
        # row — the user canceled a build, nothing failed — and `build-end`
        # says `stopped`, which the app reads as canceled rather than as
        # "Last build failed / 143".
        stopped = isinstance(exc, (Stopped, KeyboardInterrupt))
        # A full disk, said as one (`presented`); the reason below still
        # reads the exception itself.
        error = None if exc is None or stopped else (str(presented(exc)) or exc.__class__.__name__)
        # Why an export failed, for an agent: `EXPORT_FAILURE_REASONS`.
        self._reason = None if error is None else _export_reason(exc)
        # A second Stop while the terminal records go out would raise from in
        # here and lose `build-end`; held until they are out, it is raised the
        # moment the mask is restored.
        held = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP})
        try:
            waiting = self._waiting_after(exc) if error is not None else None
            self._finish(error, elapsed, stopped=stopped, waiting=waiting)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, held)
        return False

    def _waiting_after(self, exc: BaseException | None) -> str | None:
        """`waits`' answer for this failure, or None: no hook (a plain build),
        or asking failed, which is no reason to lose `build-end`."""
        if self._waits is None:
            return None
        try:
            return self._waits(exc)
        except Exception:
            return None

    def _finish(
        self, error: str | None, elapsed: float, *, stopped: bool, waiting: str | None = None
    ) -> None:
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
        #
        # Between the row and `build-end`: this run lets go of its share of
        # `retention.lock` whatever happened, and only then, on success, runs
        # its own sweep — under the signal mask with the rest of these, which
        # costs a Stop the few renames a sweep is.
        try:
            try:
                if not stopped:
                    self._record_history(error)
            finally:
                self._release_archives()
            if error is None and not stopped:
                self._run_sweep()
        finally:
            try:
                self._emit_end(error, elapsed, stopped=stopped, waiting=waiting)
            finally:
                self._detach_tee()

    def _emit_end(
        self, error: str | None, elapsed: float, *, stopped: bool = False,
        waiting: str | None = None,
    ) -> None:
        """The terminal event. Last, and exactly once, however the block was left.

        `stopped` on every one, true only for a Stop, whose `error` is null:
        `{"type": "build-end", "ok": false, "stopped": true, "error": null}`.

        `waiting` only under `graft --update`, for a device that went away
        during its build or install: `waiting: true`, and its sentence as
        `error` ("iPad mini isn't connected: it'll update when it is"), as the
        update's own events say it. The history row keeps what failed.
        """
        if not self._emitter:
            return
        fields: dict[str, Any] = {
            "app": self._app_name,
            "ok": error is None and not stopped,
            "stopped": stopped,
            "elapsed_seconds": elapsed,
        }
        if stopped:
            fields["error"] = None
        elif waiting is not None:
            fields["error"] = waiting
            fields["waiting"] = True
        elif error is not None:
            fields["error"] = error
        # An export's, always: null unless it failed for one of
        # `EXPORT_FAILURE_REASONS`. A build's has none.
        if self._export_to is not None:
            fields["reason"] = self._reason
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
        listener, self._trust_to_tee = self._trust_to_tee, None
        if listener is not None:
            listener.__exit__(None, None, None)
        handle, self._tee = self._tee, None
        if handle is None:
            return
        if self._emitter is not None and self._emitter.tee is handle:
            self._emitter.tee = None
        try:
            handle.close()
        except OSError:
            pass


def _lc_target(cfg: Config, app_cfg: AppConfig, *, log: BuildLog, udid: str) -> dict[str, Any]:
    """Which LiveContainer an app's IPA must be pushed into, as the push's
    keyword arguments: the app's pin, the team id, the account mode, and what
    Graft's deliveries recorded pushing to `udid` (`ours`), without which the
    push will not overwrite a same-named file it finds there.

    One rule, both push sites. `_do_reinstall_build` used to omit it and take
    `push_ipa_to_livecontainer`'s default, so with two LiveContainers installed
    and `lc_bundle_id:` pinned in config.yaml, Re-install pushed into the wrong
    container and reported success — or, when the pinned id was a suffixed one
    and bare `com.kdt.livecontainer` was not installed, failed by telling the
    user to set the pin they had already set.

    Unpinned, the push finds the container on the device (`lc_discovery`):
    a free Apple ID has no team id to name it by, and needs none.
    """
    return {
        "lc_bundle_id": app_cfg.lc_bundle_id,
        "team": cfg.team_id,
        "free_account": cfg.free_account,
        "ours": log.pushed_by_instance(udid),
        # This app's archived builds: a same-named file on the phone with one
        # of their bytes is ours, recorded or not (a state reset).
        "archives": sorted(cfg.built_dir.glob(f"{app_cfg.name}_*.ipa")),
    }


def _check_lc_target_early(
    cfg: Config, app_cfg: AppConfig, *, log: BuildLog, target: Target | None
) -> None:
    """What the push at stage 5 would refuse, asked before cyan runs.

    A pin that names nothing installed, two LiveContainers and no pin, or a
    stranger's `<app>.ipa` in the container's Documents all used to be found
    after the whole injection had run. `target` is the device stage 5 pushes
    to (`_planned_target`), None only for a build that installs nothing; a
    phone that does not answer is left to stage 5 (`check_push_target`).
    """
    if target is None:
        return
    lc_runner.check_push_target(
        target.udid, inner_filename=f"{app_cfg.name}.ipa",
        **_lc_target(cfg, app_cfg, log=log, udid=target.udid),
    )


def _planned_target(
    cfg: Config, app_cfg: AppConfig, device: str | None, emitter: JsonEmitter | None
) -> Target:
    """Where this build will install (`installer.resolve_target`, spec §5b),
    asked once, before the pre-flight, so the profile check, the Developer
    Mode gate, the LiveContainer check, stage 4's profile and stage 5 all
    mean the same device. `device` is only an explicit `--device`.

    Every "no target" raises here, ahead of minutes of work that could only
    end there: nothing connected, a `--device` that isn't connected, several
    devices and nothing picked. Not left to stage 5: a `--device` plugged in
    while cyan ran would have reached the install without the Developer Mode
    check or stage 4's profile having been asked about it.

    When the table chose for the person (the default isn't connected), the
    log says where and why, now, as it chooses.
    """
    target = resolve_target(cfg, app_cfg, explicit_udid=device)
    if target.reason:
        _say_escalating(emitter, target.reason)
    return target


def _report_lc_choice(
    emitter: JsonEmitter | None,
    *,
    app_name: str,
    result: lc_runner.LCDeliveryResult,
    pinned: bool = False,
) -> None:
    """Say which LiveContainer got the IPA when more than one is installed.

    `pinned`: the app named this container (`lc_bundle_id:`), so the sentence
    says so instead of suggesting a pin it already has.

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
        + (f"{result.bundle_id}, the one it is pinned to, not those" if pinned else
           f"{result.bundle_id}, not those, or pin another with "
           f"`graft --set-app-options --app {app_name} --lc-bundle-id <id>`"),
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
            "SELECT * FROM builds WHERE id = ?",
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
    ) as report:
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

        refuse_legacy_build(app_cfg, free_account=cfg.free_account)
        # A sideload archive is installed as a normal app, under the paid
        # team's profile — the same requirement a sideload build has.
        cfg.require_delivery(app_cfg.delivery, app=app_cfg.name)
        if not row["success"]:
            raise GraftError(
                f"build {build_id} did not produce a signed IPA — nothing to re-install",
                hint="pick a row with a green status",
            )
        # From before the archive is looked at until it is delivered: no sweep
        # in another process can move it to the Trash mid-push, which at
        # `archive_keep: 0` is exactly the archive one would take.
        report.hold_archives(cfg)
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
                f"build {build_id} was built for this Mac, not for an iPhone or iPad",
                hint=f"install it here with `--install-to-mac {Path(ipa_path).name}`. "
                "Its tweak dylib points at a bundled stub Preferences.framework, "
                "which iOS has for real — on an iPhone or iPad the tweak would break silently",
            )
        # Through the door the archive was built for, or not at all: the app's
        # mode may have changed since. A LiveContainer archive is unsigned and
        # the device won't install it; a sideload archive is signed by
        # zsign with the paid profile, which LiveContainer then runs as such.
        _refuse_other_mode_archive(row, build_id, app_cfg, Path(ipa_path))

        udid = row["udid"] or cfg.default_device_udid
        udid, transport = select_device(udid)

        # A 1.0 archive was built from an IPA staged before staging took the
        # buyer's data out (K8): it comes out here too. A sideload archive
        # keeps what its signature seals.
        with tempfile.TemporaryDirectory(prefix="graft-reinstall-") as work:
            shipped = _without_account_data(
                Path(ipa_path), emitter=emitter, workdir=Path(work),
                keep_sealed=app_cfg.delivery == "sideload",
            )
            _reship(cfg, row=row, app_cfg=app_cfg, ipa=shipped, udid=udid,
                    transport=transport, log_path=log_path, emitter=emitter)


def _reship(
    cfg: Config, *, row: Mapping[str, Any], app_cfg: AppConfig, ipa: Path, udid: str | None,
    transport: str, log_path: Path, emitter: JsonEmitter | None,
    for_update: bool = False, inputs: InputsDigest | None = None,
) -> None:
    """`_do_reinstall_build`'s push or install of `ipa`, and its record.

    `for_update` is `graft --update`'s (`_do_update_install`): the archive its
    build just made, recorded with that build's digest (`inputs`) and origin
    `update`, or not at all when the build couldn't take one, as the build's
    own install isn't (`_record_delivery_if_pushed`): never as a Re-install,
    which spec §11.1 would offer as an older build. `row` needs only
    `version` and `bundle_id`."""
    app_name = app_cfg.name

    # What reconcile will need to find this artifact again. Set inside the
    # livecontainer branch only; a sideload leaves both None, which is what
    # tells reconcile there is no LiveContainer to look inside.
    lc_bundle_id: str | None = None
    remote_name: str | None = None

    if app_cfg.delivery == "livecontainer":
        # Escaped: the version is the IPA's own CFBundleShortVersionString,
        # which a dropped IPA can set to `[/x]` (the 2.3.1 review's A6).
        console.print(f"→ re-installing [cyan]{escape(app_name)}[/cyan] {escape(str(row['version']))} "
                      "into LiveContainer")
        if emitter:
            emitter.emit("stage", name="lc-push", status="start")
        lc_result = lc_runner.push_ipa_to_livecontainer(
            ipa,
            udid=udid,
            inner_filename=f"{app_name}.ipa",
            # The app's pin, by the same rule the build path uses: without
            # it a re-install lands in whichever LiveContainer is the
            # default, which is not the one the build went to.
            **_lc_target(cfg, app_cfg, log=BuildLog(log_path), udid=udid),
            dry_run=False,
        )
        if emitter:
            # C-DRYRUN: where it went, and that it really went.
            emitter.emit("stage", name="lc-push", status="end",
                         bundle_id=lc_result.bundle_id, size_bytes=lc_result.size_bytes,
                         documents_path=lc_result.documents_path, dry_run=False)
        console.print(
            f"   [green]✓[/green] {lc_result.size_bytes/1_000_000:.1f} MB into "
            f"{escape(lc_result.bundle_id)} on {escape(str(udid))}"
        )
        _report_lc_choice(emitter, app_name=app_name, result=lc_result,
                          pinned=bool(app_cfg.lc_bundle_id))
        lc_bundle_id = lc_result.bundle_id
        # The name lc_runner reports having written, not a second guess at
        # how it was built — the same rule the build path's push follows.
        remote_name = Path(lc_result.documents_path).name
    else:
        console.print(f"→ re-installing [cyan]{escape(app_name)}[/cyan] {escape(str(row['version']))} "
                      "on the device")
        if emitter:
            emitter.emit("stage", name="install", status="start")
        ir = install(ipa, udid=udid, transport=transport, dry_run=False)
        if emitter:
            emitter.emit("stage", name="install", status="end")
        _emit_log_lines(emitter, "install", ir.stdout)
        _emit_log_lines(emitter, "install", ir.stderr)
        console.print(f"   [green]✓[/green] installed on {escape(str(udid))}")

    # After the push returned, which is the only point at which this
    # artifact is known to have reached the phone — the same placement rule
    # the build path's two push sites follow.
    if for_update:
        from .update_plan import ORIGIN_UPDATE

        _record_delivery_if_pushed(
            BuildLog(log_path), app_name=app_name, inputs=inputs, ipa_path=ipa,
            lc_bundle_id=lc_bundle_id, remote_name=remote_name, udid=udid, dry_run=False,
            emitter=emitter, origin=ORIGIN_UPDATE,
        )
        return
    _record_reinstall_delivery(
        cfg,
        app_name=app_name,
        bundle_id=row["bundle_id"],
        version=row["version"],
        ipa_path=ipa,
        delivery=app_cfg.delivery,
        lc_bundle_id=lc_bundle_id,
        remote_name=remote_name,
        udid=udid,
        emitter=emitter,
    )


def _do_update_install(
    cfg: Config, *, app_name: str, device: str, built: update_run.Built,
    emitter: JsonEmitter | None,
) -> None:
    """`graft --update`'s install on each of the app's devices after the
    first (spec §11.3): the archive its one build just made, through the
    re-install path (`_reship`), recorded with origin `update` and that
    build's inputs digest, so the device reads `current` and spec §11.1
    doesn't take it for a deliberate older install.

    In its own build envelope, as a re-install is: `build-start`, the push's
    or the install's stages, `build-end`, and no history row (the build
    wrote one). The push or install is held against a first Stop
    (`stop_deferral`), as the build's own is; the update releases it.
    """
    app_cfg = cfg.apps.get(app_name)
    with _BuildReport(
        emitter,
        app_name=app_name,
        bundle_id=app_cfg.bundle_id if app_cfg else "",
        delivery=app_cfg.delivery if app_cfg else None,
        waits=built.install_waits,
    ) as report:
        app_cfg = cfg.get_app(app_name)
        from .lc_manage import refuse_legacy_build

        refuse_legacy_build(app_cfg, free_account=cfg.free_account)
        cfg.require_delivery(app_cfg.delivery, app=app_cfg.name)
        report.hold_archives(cfg)
        ipa = built.archive
        if ipa is None or not ipa.exists():
            raise GraftError(
                f"the archive of this update's build is gone: {ipa or '(none)'}",
                hint=f"run `graft --update {app_name}` again",
            )
        report.version = built.version
        if device.lower() in {u.lower() for u in built.not_in_profile}:
            # The build's profile was made for every device (`also`), and App
            # Store Connect left this one out: installd would refuse it.
            label = devices_seen.label(cfg.state_dir, device)
            raise DeviceNotRegisteredError(
                f"{label} isn't registered in your developer account: add it at "
                "developer.apple.com, then try again",
                hint=f"its UDID is {device}. Add it under Certificates, Identifiers & Profiles "
                "› Devices; Graft doesn't register devices itself",
            )
        udid, transport = select_device(device)
        stop_deferral.finish_before_stopping(
            app_name, "lc-push" if app_cfg.delivery == "livecontainer" else "install")
        _reship(cfg, row={"version": built.version or "?", "bundle_id": app_cfg.bundle_id},
                app_cfg=app_cfg, ipa=ipa, udid=udid, transport=transport,
                log_path=cfg.state_dir / "log.sqlite", emitter=emitter, for_update=True,
                inputs=built.inputs)


def _archived_delivery(row: sqlite3.Row, ipa_path: Path) -> str | None:
    """How the archive of a build row was built: `sideload`, `livecontainer`,
    or None when that cannot be told.

    The row says, from the release that recorded it on. A row from before
    that is read off the archive: a zsign build recorded its signing team,
    and zsign embeds the provisioning profile in the app, which an unsigned
    LiveContainer build of a decrypted App Store IPA does not have. An archive
    that will not open as a zip says nothing, and the re-install refuses.
    """
    recorded = row["delivery"] if "delivery" in row.keys() else None
    if recorded in ("sideload", "livecontainer"):
        return recorded
    if row["signing_team"]:
        return "sideload"
    try:
        with zipfile.ZipFile(ipa_path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return None
    embedded = re.compile(r"^Payload/[^/]+\.app/embedded\.mobileprovision$")
    return "sideload" if any(embedded.match(n) for n in names) else "livecontainer"


def _refuse_other_mode_archive(
    row: sqlite3.Row, build_id: int, app_cfg: AppConfig, ipa_path: Path
) -> None:
    """Refuse a re-install whose archive was built for the other delivery mode.

    Design call: a row from before delivery was recorded is not refused
    wholesale. Its archive shows how it was signed (`_archived_delivery`), and
    when that matches the app's mode now the re-install is the one the user
    always had. Only a mismatch, or an archive that cannot be read, sends them
    to rebuild.
    """
    archived = _archived_delivery(row, ipa_path)
    rebuild = f"rebuild it with `graft {app_cfg.name}`, which builds for the app's mode now"
    if archived is None:
        raise GraftError(
            f"can't tell how build {build_id} was built, so it can't be re-installed safely",
            hint=rebuild,
        )
    if archived != app_cfg.delivery:
        what = ("an unsigned LiveContainer archive, which cannot be installed as an app"
                if archived == "livecontainer" else
                "signed by zsign for sideloading, not an archive for LiveContainer")
        raise GraftError(
            f"build {build_id} was built for {archived} delivery and {app_cfg.name} is "
            f"now set to {app_cfg.delivery}: it is {what}",
            hint=rebuild,
        )


def _without_account_data(
    path: Path, *, emitter: JsonEmitter | None, workdir: Path | None, keep_sealed: bool = False
) -> Path:
    """`path`, or a copy of it, without the buyer's Apple ID data: what a
    build, a re-install and a hand-over to PlayCover ship.

    A no-op — one zip listing — for an IPA staged since staging took the data
    out. Otherwise it says so before it starts (a large IPA takes a while),
    then heals the file in place. When that fails — a full or read-only
    volume — the data comes out of a copy in `workdir` instead, which is
    returned, with a warning that the original still has it: privacy kept,
    the build goes on (it ran fine on such a cache in 1.0). Without a
    `workdir`, or when the copy fails too, it is an error: nothing ships with
    the data in it.

    `keep_sealed` is for a zsign-signed archive: the licenses its signature
    seals stay (`ipa_inspect.sealed_account_data`), since removing them breaks
    the install and re-signing is a rebuild.
    """
    keep: set[str] = set()
    if keep_sealed:
        try:
            keep = ipa_inspect_module.sealed_account_data(path)
        except (zipfile.BadZipFile, OSError):
            keep = set()
    if not provide_ipa_module.needs_scrub(path, keep=keep):
        return path
    console.print(f"   removing Apple ID data from [cyan]{escape(path.name)}[/cyan]…")
    _emit_log_lines(emitter, "scrub", f"removing Apple ID data from {path.name}")
    try:
        healed = provide_ipa_module.scrub_staged(path, keep=keep)
    except (GraftError, OSError) as exc:
        reason = exc.message if isinstance(exc, GraftError) else str(exc)
        if workdir is None:
            raise GraftError(
                f"couldn't remove the Apple ID data from {path.name}: {reason}",
                hint=f"nothing was handed over with it; check that {path.parent} is "
                "writable and has free space, then try again",
            ) from exc
        copy = workdir / f"scrubbed-{path.name}"
        try:
            provide_ipa_module.scrubbed_copy(path, copy, keep=keep)
        except (GraftError, OSError) as copy_exc:
            detail = copy_exc.message if isinstance(copy_exc, GraftError) else str(copy_exc)
            raise GraftError(
                f"couldn't remove the Apple ID data from {path.name}, in place ({reason}) "
                f"or in a copy ({detail})",
                hint="nothing was shipped with it; free some space, then try again",
            ) from copy_exc
        _warn(
            emitter,
            f"couldn't remove the Apple ID data from {path.name} in place ({reason}); "
            f"using a copy without it, and {path} still has it",
        )
        return copy
    if healed:
        _warn(
            emitter,
            f"removed the Apple ID metadata and license files ({len(healed)} entries) "
            f"from {path.name}, which was made before Graft took them out on staging",
        )
    if keep:
        _warn(
            emitter,
            f"{path.name} keeps {len(keep)} license file(s) its signature seals; "
            "rebuild it to ship it without them",
        )
    return path


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

    The exception is an archive whose bytes (sha256) a recorded push already
    shipped, usually the newest build re-installed: that push's digest
    describes exactly these bytes, so the row copies it and says
    `origin="reinstall-recorded"`, and is compared like a build.

    Never raises: the artifact already reached the phone, and losing the row
    costs a spurious badge where failing the command would cost the user a
    working install.
    """
    try:
        log = BuildLog(cfg.state_dir / "log.sqlite")
        ipa_sha256 = sha256_of(ipa_path) if ipa_path.exists() else None
        # Unless these are the very bytes a recorded push shipped: then what
        # went into them IS known, and saying "never recorded" made a re-install
        # of the newest build read "out of date" against an unchanged config.
        recorded = log.recorded_delivery_of(app_name, ipa_sha256) if ipa_sha256 else None
        if recorded is not None:
            log.record_delivery(
                app=app_name,
                bundle_id=recorded["bundle_id"],
                inputs_digest=recorded["inputs_digest"],
                digest_version=recorded["digest_version"],
                cyan=json.loads(recorded["cyan"]) if recorded["cyan"] is not None else None,
                tweaks=json.loads(recorded["tweaks"]),
                source_version=recorded["source_version"],
                ipa_sha256=ipa_sha256,
                delivery=delivery,
                lc_bundle_id=lc_bundle_id,
                remote_name=remote_name,
                udid=udid,
                origin="reinstall-recorded",
                # The digest hashed it, so it goes with the digest.
                held_version_id=recorded["held_version_id"],
            )
            return
        log.record_delivery(
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
            ipa_sha256=ipa_sha256,
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


#: How long `--prune-builds` waits for builds to finish with their archives,
#: and how long a build or re-install waits for a sweep to finish. A sweep is a
#: handful of renames into the Trash; a build delivers for as long as a push
#: takes, so `--prune-builds` gives up rather than wait that out.
_RETENTION_LOCK_TIMEOUT = 30.0


class _RetentionBusy(GraftError):
    """A sweep found the retention lock taken for the whole of its wait."""


class _RetentionLock:
    """`retention.lock`: what keeps a sweep off an archive still in flight.

    Two kinds of holder, across processes. A build shares it (`LOCK_SH`) from
    the moment it archives its IPA until its history row is written, and
    `--reinstall-build` from before it opens the archive until it has
    delivered it; any number of those at once (`_BuildReport.hold_archives`).
    A sweep — `--prune-builds`, or the one a build runs once it has let go of
    its own share — holds it alone (`LOCK_EX`, `_prune_archives`). So no sweep
    moves to the Trash an archive a build or re-install is still pushing, or
    one whose history row is not written yet, and two sweeps take turns rather
    than plan the same removals (the second then failed on every file the first
    had moved, exiting 1). This replaced a guess: `--prune-builds` left alone
    anything written in the last ten minutes, which a build's own sweep did not
    do and a re-install of an old archive was not covered by.

    `flock`, beside the device dump lock in the cache's `.locks`, so it dies
    with its holder. Proceeds unlocked where locking is impossible (a lock file
    it cannot create, a filesystem without `flock`), as the config lock does: a
    sweep or a build that refuses to run is worse than one that might race.
    """

    def __init__(self, cfg: Config) -> None:
        self.path = cfg.cache_dir / ".locks" / "retention.lock"
        self._fd: int | None = None

    def acquire(self, *, exclusive: bool, wait: float) -> bool:
        """Take it, trying for up to `wait` seconds (0: once).

        Polled with `LOCK_NB` rather than blocked on, so the wait has an end
        and a Stop is never stuck behind it. False when another holder kept it
        the whole time; True when held, or when it cannot be locked at all.
        """
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return True
        mode = (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return False
                time.sleep(0.05)
            except OSError:
                break
        self._fd = fd
        return True

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)  # releases the flock


def _prune_archives(
    cfg: Config,
    *,
    keep: int,
    dry_run: bool,
    exclude: Iterable[Path] = (),
    wait: float | None = None,
    emitter: JsonEmitter | None = None,
    trash_dir: Path | None = None,
) -> tuple[list[Archive], list[tuple[Archive, str]]]:
    """Trash all but the newest `keep` archives per (app, version) in `built_dir`.

    Returns `(removed, failures)`. Shared by the `--prune-builds` verb and the
    automatic sweep after a successful build so the two can't drift; the only
    difference between them is that the build passes its own fresh archive in
    `exclude`.

    Archives go to the Trash, not `unlink()`. They are the user's rollback
    artifacts — the thing `--reinstall-build` re-ships — so a wrong retention
    setting should cost a trip to the Trash, not the build. The corollary is
    that pruning frees no disk space until the Trash is emptied, which the
    output says out loud.

    Per-file failures are collected, not raised: one unreadable archive must
    not strand the rest, and this runs inside a successful build where an
    exception would be recorded as a build failure. `trash_dir` is injectable
    for the same reason as `_do_delete_tweak`'s — so tests never touch the real
    `~/.Trash`.

    Planned and carried out holding `retention.lock` alone (`_RetentionLock`),
    so no archive a build or re-install still shares it for is touched. `wait`
    is how long to try for it — `_RETENTION_LOCK_TIMEOUT` when None, 0 for a
    build's own sweep, which skips rather than wait on another build's
    delivery — and `_RetentionBusy` says it stayed taken.
    """
    lock = _RetentionLock(cfg)
    if not lock.acquire(exclusive=True, wait=_RETENTION_LOCK_TIMEOUT if wait is None else wait):
        raise _RetentionBusy(
            "a build is delivering an archived IPA, or another clean-up of old builds is running",
            hint="try again when it has finished; if no graft is running, "
            f"the process holding {lock.path} is stopped or stuck",
        )
    try:
        return _prune_archives_locked(
            cfg, keep=keep, dry_run=dry_run, exclude=exclude,
            emitter=emitter, trash_dir=trash_dir,
        )
    finally:
        lock.release()


def _prune_archives_locked(
    cfg: Config,
    *,
    keep: int,
    dry_run: bool,
    exclude: Iterable[Path],
    emitter: JsonEmitter | None,
    trash_dir: Path | None,
) -> tuple[list[Archive], list[tuple[Archive, str]]]:
    import sqlite3 as _sqlite3

    # Deliberately not `BuildLog(db_path)` unconditionally: its constructor
    # creates the file, and pruning before the first build shouldn't leave an
    # empty history database behind. No log means no ipa_path to clear, and no
    # way to tell a re-installable archive from one a failed delivery left.
    db_path = cfg.state_dir / "log.sqlite"
    log = BuildLog(db_path) if db_path.exists() else None
    reinstallable = None
    if log is not None:
        try:
            reinstallable = log.reinstallable_paths()
        except _sqlite3.Error as exc:
            # Planning blind is the bug this list exists to prevent: archives
            # from failed deliveries outranking the last re-installable one.
            raise GraftError(
                f"couldn't read the build history to plan a clean-up: {exc}",
                hint=f"nothing was moved; check {db_path}",
            ) from exc

    try:
        plan = plan_prune(
            cfg.built_dir, keep=keep, exclude=exclude, reinstallable=reinstallable,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

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
                # The file stayed, so its rows get their pointer back: a move
                # that failed must not cost the Re-install button as well.
                if log is not None:
                    with contextlib.suppress(_sqlite3.Error):
                        log.restore_ipa_path(build_ids, archive.path)
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
    # Waits up to `_RETENTION_LOCK_TIMEOUT` for builds in other processes —
    # the app's, a Terminal's — to finish delivering, then refuses
    # (`_RetentionBusy`, exit 1) with nothing moved. A dry run waits too, so
    # it lists what the real run would move rather than an archive in flight.
    _removed, failures = _prune_archives(
        cfg, keep=effective, dry_run=dry_run, emitter=emitter, trash_dir=trash_dir,
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
    # What each device said, kept for when it isn't connected: the app names
    # an unplugged default from the `device-seen` events below (spec §5b).
    elsewhere = _remember_devices(cfg, listing.devices, (f.udid for f in listing.failures))
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
                # Whether a null bundle id means "not installed": false when
                # the phone's apps couldn't be listed (a locked phone).
                livecontainer_known=d.livecontainer_known,
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
        for seen in elsewhere:
            emitter.emit("device-seen", **seen)
        for f in listing.failures:
            emitter.emit("device-error", udid=f.udid, error=f.error)
        emitter.emit("devices-end", count=len(listing.devices), failed=len(listing.failures))
        return
    _print_devices(listing.devices)


def _remember_devices(
    cfg: Config, described: list[DeviceInfo], connected: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Remember what each described device said (`devices_seen`), and return
    one `device-seen` payload per remembered device this listing didn't
    describe: unplugged, or connected and locked. The configured default is
    always among them when it's set and not described, with nothing to say
    of it if Graft never saw it.

    `connected` adds the devices on the bus that couldn't be described (a
    locked phone): they're kept, though not remembered anew, and the rest are
    forgotten (`_forget_strangers`).

    Best effort, and broadly so: names are a convenience, and a state folder
    that can't be read or written costs them, never the listing.
    """
    try:
        for d in described:
            devices_seen.remember(cfg.state_dir, d.udid, **devices_seen.listing_facts(d))
        _forget_strangers(cfg, {d.udid for d in described} | set(connected))
        return _seen_elsewhere(cfg, {d.udid for d in described})
    except Exception:
        return []


def _forget_strangers(cfg: Config, connected: set[str]) -> None:
    """Keep the names of devices Graft has a reason to name, and forget the
    rest, so `devices-seen.json` can't grow without bound: a device stays
    while it's connected (a locked one too), while it's the configured
    default, or while the delivery log records a delivery to it. Nothing is
    forgotten when that log exists and can't be read."""
    from .build_log import delivered_udids

    delivered = delivered_udids(cfg.state_dir / "log.sqlite")
    if delivered is None:
        return
    keep = connected | delivered
    if cfg.default_device_udid:
        keep.add(cfg.default_device_udid)
    devices_seen.forget_others(cfg.state_dir, keep)


def _seen_elsewhere(cfg: Config, described: set[str]) -> list[dict[str, Any]]:
    """`device-seen` payloads: every remembered device not in `described`,
    then the configured default when it's neither described nor remembered."""
    remembered = devices_seen.load(cfg.state_dir)
    default = cfg.default_device_udid
    out = [
        {"udid": udid, "name": facts.get("name"), "model": facts.get("model"),
         "ios_version": facts.get("ios_version"), "is_default": udid == default}
        for udid, facts in sorted(remembered.items()) if udid not in described
    ]
    if default and default not in described and default not in remembered:
        out.append({"udid": default, "name": None, "model": None, "ios_version": None,
                    "is_default": True})
    return out


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
    the right behavior for "no icon" — the letter tile it draws today — and
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
            found = artwork.resolve(
                app_cfg.bundle_id, ipa=ipa, refresh=refresh,
                store_country=app_cfg.store_country,
            )
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

    # Signatures mirror the BuildLog methods they stand in for.
    def latest_delivery(
        self, app: str, *, udid: str | None = None, or_unrecorded: bool = False
    ) -> sqlite3.Row | None:
        raise sqlite3.OperationalError(f"the delivery log could not be opened (reading {app})")

    def delivery_udids(self, app: str) -> dict[str | None, int]:
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
    or_unrecorded: bool = False,
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
    `or_unrecorded` counts the deliveries recorded without a device as that
    phone's (`BuildLog.latest_delivery`), for `graft --update`'s default.
    """
    try:
        if log is None:
            row = None
        elif or_unrecorded:
            row = log.latest_delivery(app_name, udid=udid, or_unrecorded=True)
        else:
            row = log.latest_delivery(app_name, udid=udid)
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
                    else "The device is out of date."
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
            held_version_id=row["held_version_id"],
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


def _update_target_ids(cfg: Config, log: BuildLog | _UnreadableLog, name: str) -> tuple[
    dict[str, int], str | None, bool
]:
    """Every device `name` was delivered to, with its newest delivery's id,
    before any is dropped (spec §11.3): the devices the rows name, and the
    default for the rows that name none, but only when `devices_seen` knows
    the default (a known limit otherwise: they're left out). Also which
    device those unrecorded rows went to, if any, and whether some were left
    out. Raises `sqlite3.Error` when the log can't be read."""
    rows = log.delivery_udids(name)
    targets = {udid: last for udid, last in rows.items() if udid is not None}
    unrecorded = rows.get(None)
    if unrecorded is None:
        return targets, None, False
    default = cfg.default_device_udid or None
    if default is None or default not in devices_seen.load(cfg.state_dir):
        return targets, None, True
    targets[default] = max(targets.get(default, 0), unrecorded)
    return targets, default, False


def _delivery_targets(cfg: Config) -> Callable[[str], Any]:
    """Where `graft --update` installs each app (`update_plan.AppDeliveries`),
    from one delivery log opened once: every device it was delivered to,
    less the ones dropped (`update_drops`) since that app's last delivery
    there, each with the state `--delivery-state --device` reports and its
    reasons, and from the last delivery there the origin (a Re-install's
    `reinstall`) and whether a reconcile found it gone (spec §11.1, §11.3)."""
    from . import update_drops
    from .update_plan import AppDeliveries, DeliveryFacts

    log = _open_delivery_log(cfg)

    def facts(name: str) -> AppDeliveries:
        app_cfg = cfg.get_app(name)
        if log is None:
            return AppDeliveries()
        try:
            ids, unrecorded_to, unplaced = _update_target_ids(cfg, log, name)
        except sqlite3.Error:
            return AppDeliveries(delivered=True, unreadable=True)
        drops = update_drops.dropped(cfg.state_dir, name)
        dropped = sorted(u for u, last in ids.items() if u in drops and last <= drops[u])
        targets: list[DeliveryFacts] = []
        for udid in sorted(u for u in ids if u not in dropped):
            mine = udid == unrecorded_to
            state = _delivery_state_for(cfg, name, app_cfg, log, udid=udid, or_unrecorded=mine)
            try:
                row = log.latest_delivery(name, udid=udid, or_unrecorded=mine)
            except sqlite3.Error:
                row = None
            targets.append(DeliveryFacts(
                kind=state["kind"],
                reasons=list(state.get("reasons") or []),
                origin=row["origin"] if row is not None else None,
                reconcile_missing=row is not None and row["reconcile_ok"] == 0,
                udid=udid,
            ))
        return AppDeliveries(targets=targets, delivered=bool(ids) or unplaced,
                             unplaced=unplaced, dropped=dropped)

    return facts


def _drop_update_target(cfg: Config, app_name: str, udid: str) -> str | None:
    """`--drop-update-target`: stop updating `app_name` on `udid` until that
    app is delivered there again (`update_drops`). Returns the device's name,
    if Graft knows it. Refuses a device that isn't one of the app's targets,
    dropped or not; dropping one again changes nothing. Touches no device."""
    from . import update_drops

    cfg.get_app(app_name)  # the usual unknown-app error
    log = _open_delivery_log(cfg)
    try:
        ids = _update_target_ids(cfg, log, app_name)[0] if log is not None else {}
    except sqlite3.Error as exc:
        raise GraftError(f"the delivery log can't be read: {exc}") from exc
    if udid not in ids:
        raise ConfigError(
            f"{app_name} isn't on that device",
            hint=f"Update installs {app_name} only on the devices it was delivered to",
        )
    drops = update_drops.dropped(cfg.state_dir, app_name)
    # Only the drops still in force are kept: one a later delivery cleared is gone.
    kept = {u: last for u, last in drops.items() if u in ids and ids[u] <= last}
    kept[udid] = ids[udid]
    try:
        update_drops.set_dropped(cfg.state_dir, app_name, kept)
    except OSError as exc:
        raise GraftError(f"couldn't save that in {cfg.state_dir}: {exc}") from exc
    return devices_seen.name_of(cfg.state_dir, udid)


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
    log: BuildLog, app_name: str, *, udid: str | None, emitter: JsonEmitter | None = None,
    device_name: str | None = None,
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

    Once the device answered, `reconcile-checked` says which one it was (by
    `device_name`, what Graft knows it as, else null) and whether the file
    was there, so Check device can name it (2.3.1 §3.5). Nothing else does:
    a check that asked no device must not read as one that did.
    """
    row = log.latest_delivery(app_name)
    if row is None:
        return

    # Listing a LiveContainer's Documents/ says nothing about a sideload, which
    # is a real top-level install. Spec §7.1: the obvious alternative — ask
    # InstallationProxyService whether the bundle id is installed — does not
    # work, because cyan ships `new_bundle_id or f"{bundle_id}.plus"` whenever
    # keep_bundle_id is false (it defaults to true, but a per-app override can set it) while this row records the pre-cyan
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
        _warn(emitter, f"couldn't check {app_name} on {device_name or udid}: {exc}")
        return

    found = row["remote_name"] in names
    log.mark_reconciled(int(row["id"]), seen=found)
    if emitter:
        emitter.emit("reconcile-checked", app=app_name, udid=udid, device_name=device_name, found=found)


def _reconcile_device(
    log: BuildLog, app_name: str, *, device: str | None, emitter: JsonEmitter | None,
    state_dir: Path | None = None,
) -> str | None:
    """The device to check `app_name` on, connected, or None after saying why
    not. Nothing delivered is None in silence: there is nothing to check.

    The device the app was last delivered to (the row's `udid`), not the
    first one connected or the one picked for the next Build & Install:
    `_reconcile_app` leaves a row pushed to another phone alone, so asking any
    other device checked nothing and said "the record still stands". An
    explicit `--device` is used as given, and a row it doesn't match is said.
    Warnings name a device as `state_dir`'s `devices_seen` last saw it,
    else by its UDID.
    """
    def label(udid: str) -> str:
        return devices_seen.label(state_dir, udid) if state_dir is not None else udid

    row = log.latest_delivery(app_name)
    # Only a LiveContainer delivery is checked (`_reconcile_app` says why), so
    # no device is looked for, or warned about, for anything else.
    if row is None or row["delivery"] != "livecontainer":
        return None
    delivered_to = row["udid"]
    if device and delivered_to and device != delivered_to:
        _warn(emitter, f"{app_name} was last delivered to {label(delivered_to)}, not "
                       f"{label(device)}: not checked")
        return None
    want = device or delivered_to
    if not want:
        # A row from before deliveries recorded their device: unmatchable.
        return None
    try:
        udid, _transport = select_device(want)
    except GraftError as exc:
        # `_warn`, like every other warning in this module: stdout is where
        # the JSON events go, so prose can only reach a machine consumer as
        # a `log` event. Not connected, whichever way usbmux said it, is
        # said of the device the check wanted, by name.
        missing = isinstance(exc, (DeviceNotConnectedError, NoDeviceConnectedError))
        why = f"{label(want)} isn't connected" if missing else exc.message
        _warn(emitter, f"no device checked for {app_name}: {why}")
        return None
    return udid


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
            for name in names:
                udid = _reconcile_device(log, name, device=device, emitter=emitter,
                                         state_dir=cfg.state_dir)
                if udid is not None:
                    _reconcile_app(log, name, udid=udid, emitter=emitter,
                                   device_name=devices_seen.name_of(cfg.state_dir, udid))
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
    table.add_column("store")
    for name, a in sorted(cfg.apps.items()):
        kind = states.get(name, {}).get("kind", "unknown")
        table.add_row(
            name,
            a.bundle_id,
            a.preferred_version,
            ", ".join(a.tweaks) or "[dim]none[/dim]",
            f"{_DELIVERY_MARKS.get(kind, '?')} {kind}",
            a.store_country or "[dim]—[/dim]",
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


def _store_note(emitter: JsonEmitter | None, note: str | None) -> None:
    """Say how a store was chosen (`storefronts.Store.note`): a territory, an
    alias, or a region with no store. Through `_warn`, so it reaches stderr
    and, under --json, the event stream."""
    if note:
        _warn(emitter, note)


def _do_appstore_search(
    cfg: Config, term: str, *, country: str | None, limit: int | None,
    emitter: JsonEmitter | None = None,
) -> None:
    """Search the App Store: one event per result and the sentinel, or a table.

    A failed search emits `appstore-search-failed` with its reason just before
    the error goes on to `_fatal`, so the exit status (13, or 2 for a term
    that isn't text) and stderr are what they always are and the app can tell
    offline and rate-limited from the rest. Both the sentinel and the failure
    carry `note`: how the store was chosen, when there is something to say.
    """
    store = appstore_search.resolve_country(country)
    if emitter is None:
        # Under --json the note is the events' own `note` field instead.
        _store_note(None, store.note)
    try:
        found = appstore_search.search(
            cfg, term, country=store, limit=limit, with_icons=emitter is not None
        )
    except (itunes.ITunesRequestError, appstore_search.InvalidTerm) as e:
        if emitter:
            # A lone surrogate can't be written as text, and the app needs none.
            shown = term.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
            emitter.emit("appstore-search-failed", term=shown, reason=e.reason, note=store.note)
        raise
    if emitter:
        for hit in found.hits:
            emitter.emit("appstore-search-result", **appstore_search.event_fields(hit))
        emitter.emit(
            "appstore-search-end", term=found.term, country=found.country,
            count=len(found.hits), note=found.note,
        )
        return
    # Every cell is a listing's own text, so every cell has its control
    # characters and escape sequences stripped (`printable`), then its markup
    # escaped (see the note above `_do_add_repo` on markup in values this CLI
    # did not write).
    if not found.hits:
        console.print(
            f"[yellow]no iPhone apps on the {found.country} App Store match[/yellow] "
            f"{escape(repr(found.term))}"
        )
        return
    table = Table(
        title=f"App Store ({found.country}): {len(found.hits)} match(es) for {escape(repr(found.term))}"
    )
    # Name and developer keep a minimum width, and the bundle id folds rather
    # than being cut with "…": it is the value the footer says to copy.
    table.add_column("name", style="bold", overflow="fold", min_width=8, max_width=28)
    table.add_column("developer", overflow="fold", min_width=6, max_width=20)
    table.add_column("price", no_wrap=True)
    table.add_column("bundle id", style="cyan", overflow="fold")
    table.add_column("added as", no_wrap=True)
    for hit in found.hits:
        r = hit.result
        table.add_row(*(
            escape(appstore_search.printable(cell)) for cell in (
                r.name, r.developer, appstore_search.price_label(r), r.bundle_id,
                hit.configured_as or "",
            )
        ))
    console.print(table)
    console.print(
        "[dim]→ graft --add-app-manual --name NAME --bundle-id BUNDLE_ID "
        f"--store-country {found.country} adds one, as the Mac app's Add App does[/dim]"
    )


def _fetch_into_library(cfg: Config, target: str, expected_sha: str | None,
                        repository_id: str | None = None) -> Path:
    """`--fetch`'s download, into a folder of its own, then into the library
    under Graft's name for it (`tweak_sources.place`: a .deb, of the package
    and version canister listed for a package id, never over a different
    file), with its source recorded for its updates: the canister repository
    it came from, or the configured repo a URL is under."""
    picked: list[tuple[canister.Package, canister.Repository]] = []
    is_url = bool(canister._URL_RE.match(target))
    with update_tweaks.workdir(cfg.tweaks_dir) as work:
        try:
            downloaded = canister.fetch_deb(
                target, work, expected_sha, repository_id=repository_id,
                on_pick=lambda pkg, repository: picked.append((pkg, repository)),
            )
        except canister.SeveralRepositoriesError as e:
            # The person picks the repository, by its id (exit 2: a flag is
            # missing). Graft won't guess one and record it as trusted.
            raise ConfigError(e.message, hint=e.hint) from e
        dest = tweak_sources.place(
            downloaded, cfg.tweaks_dir, package_id=None if is_url else target,
            version=picked[-1][0].version if picked else None,
        )
    if picked:
        pkg, repository = picked[-1]
        source: tweak_sources.Source | None = tweak_sources.Source(
            tweak_sources.CANISTER, pkg.repository_id, repository.uri.rstrip("/"))
    else:
        source = tweak_sources.repo_source_for(cfg.repos, target) if is_url else None
    if source is not None:
        update_tweaks.record_source(cfg, dest, source)
    return dest


def _do_fetch(
    cfg: Config,
    target: str,
    emitter: JsonEmitter | None = None,
    expected_sha: str | None = None,
    repository_id: str | None = None,
) -> None:
    cfg.tweaks_dir.mkdir(parents=True, exist_ok=True)
    if emitter:
        emitter.emit("canister-fetch-start", target=target)
        try:
            dest = _fetch_into_library(cfg, target, expected_sha, repository_id)
        except GraftError as e:
            emitter.emit("canister-fetch-end", ok=False, error=str(e))
            # The event carries the detail for the GUI; the exit code carries
            # the verdict, as it does for every other verb. Returning here made
            # a failed fetch exit 0.
            raise
        emitter.emit(
            "canister-fetch-end",
            ok=True,
            filename=dest.name,
            size_bytes=dest.stat().st_size,
            path=str(dest),
        )
        return
    console.print(f"→ fetching [cyan]{escape(target)}[/cyan] into {escape(str(cfg.tweaks_dir))}")
    dest = _fetch_into_library(cfg, target, expected_sha, repository_id)
    size_kb = dest.stat().st_size / 1024
    console.print(f"[green]✓[/green] {escape(dest.name)}  ({size_kb:,.1f} KB)")
    console.print(
        f"[dim]→ add [bold]{escape(dest.name)}[/bold] to an app's `tweaks:` list in "
        f"{escape(str(cfg.path))} to use it[/dim]"
    )


# The rule the five success lines below follow, and why it is those five.
#
# `console.print` parses `[...]` as markup: on rich 15 a closing-tag-shaped
# token raises MarkupError and an unrecognized opening tag is deleted in
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


def _do_add_app_url(
    cfg: Config,
    url_or_id: str,
    delivery: str = "sideload",
    emitter: JsonEmitter | None = None,
) -> None:
    cfg.require_delivery(delivery)
    # One parse for both: the store the URL names (`apps.apple.com/gb/app/...`,
    # or the legacy `itunes.apple.com/gb/app/...`) is asked, and kept as
    # `store_country` so later lookups ask it too. A URL that names none asks
    # the chain every lookup shares (`host.look_up`: this Mac's region's
    # store, then the US), and keeps nothing.
    link = itunes.parse_app_link(url_or_id)
    country = link.country
    info, asked = host.look_up(country, lambda store: itunes.lookup(link.track_id, country=store))
    desired = app_key(info.name, info.bundle_id)
    app_cfg: dict[str, Any] = {
        "bundle_id": info.bundle_id,
        "preferred_version": "latest",
        "delivery": delivery,
        "tweaks": [],
        "cyan": _default_cyan_block(info.name),
    }
    if country:
        app_cfg["store_country"] = country
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
    if not country:
        console.print(
            f"[dim]  the URL names no country, so it was looked up in this Mac's region's "
            f"App Store, then the US one, and found in the {asked} App Store; its checks "
            "will look the same way[/dim]"
        )


def _do_add_app_manual(
    cfg: Config,
    *,
    name: str,
    bundle_id: str,
    display_name: str | None,
    delivery: str,
    store_country: str | None = None,
    emitter: JsonEmitter | None = None,
) -> None:
    cfg.require_delivery(delivery)
    desired = app_key(name, bundle_id)
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
    if store_country:
        app_cfg["store_country"] = store_country
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
    and the build carried an encrypted binary forward with only a warning at stage 2.5; now the main binary's cryptid decides,
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
        version = _stage_ipa(cfg, app_name=app_name, source_path=usable, emitter=emitter)
    # Staged under the plain name, which a hold by build never uses: the
    # person gave this IPA for that version, so it holds by version now.
    appstore.let_go_of_the_build(
        cfg, app_name, version, emitter=emitter, warn=lambda message: _warn(emitter, message),
    )


def _stage_ipa(
    cfg: Config,
    *,
    app_name: str,
    source_path: Path,
    emitter: JsonEmitter | None,
    external_version_id: str | None = None,
) -> str:
    """Copy a decrypted IPA into the cache under its canonical name, and say so.
    Returns the version staged, which a get of a chosen version holds the app
    at.

    Shared by `--provide-ipa` and `--appstore-get`, so both end in the same
    `ipa-provided` event the GUI already refreshes on.
    """
    app = cfg.get_app(app_name)
    dest, info = provide_ipa_module.provide(
        cache_dir=cfg.cache_dir,
        expected_bundle_id=app.bundle_id,
        source_path=source_path,
        # A get by id: that build's own name (`ipa_cache.staged_name`).
        external_version_id=external_version_id,
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
        return info.version
    console.print(
        f"[green]✓[/green] cached [bold]{dest.name}[/bold]  "
        f"({size // 1024} KB)  → {dest}"
    )
    return info.version


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
    can sit in this directory, and which one a build would use is
    `find_in_cache`'s rule: `max` by `version_key` for `latest`, because
    `21.9.1` sorts after `21.16.2` as a string and before it as a version. A
    second implementation of that version comparison in another language is
    still a second thing to be wrong.

    For a bundle id the config's apps claim, and hold alike, it is that
    app's own answer (`find_in_cache`, so `ipa_cache.admits`): a pinned
    version's file, and for a hold by build that build's own file and no
    other. The Cache pane's "BUILD USES THIS" says so, and on `latest`'s rule
    it sat on the plain file of a held version, which a hold by build never
    uses. No file is preferred when that answer is none (the held build is
    gone: a build would stop). A bundle id no app claims, or that two apps
    hold differently, has no one answer, and keeps `latest`'s.
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
        info_by_path = dict(entries)
        claimants = [a for a in cfg.apps.values() if a.bundle_id == bundle_id]
        if claimants and len({(a.preferred_version, a.preferred_version_id) for a in claimants}) == 1:
            used = find_in_cache(cfg, claimants[0])
            if used is not None and used.path in info_by_path:
                preferred_path[bundle_id] = used.path
            continue
        order = _cache_candidates(cfg.cache_dir, bundle_id)
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
                    # has no stake in this refusal. `admits` is
                    # `find_in_cache`'s own rule: `"latest"` accepts every
                    # version, a pin only its own, and a hold by build only
                    # that build's file.
                    and admits(app, target, info.version)
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
                    # app that `admits` it, the same test `find_in_cache`
                    # applies. `"latest"` accepts any sibling; a pinned app
                    # needs one whose version matches exactly; a hold by build
                    # needs that build's own file, so a twin of the same
                    # version under another name rescues nothing.
                    broken = [
                        name for name in claimed_by
                        if not any(admits(cfg.apps[name], p, sib.version) for p, sib in siblings)
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
                        names = ", ".join(broken)
                        # With other copies staged (a twin of a held build, a
                        # pin's other versions), it isn't the only staged IPA,
                        # only the only one these apps' holds take.
                        message = (
                            f"{filename} is the only staged IPA that {names} can build from"
                            if siblings else f"{filename} is the only staged IPA for: {names}"
                        )
                        held = [
                            (name, cfg.apps[name].preferred_version_id) for name in broken
                            if cfg.apps[name].preferred_version != "latest"
                            and cfg.apps[name].preferred_version_id is not None
                        ]
                        if held:
                            # Its own file is the only one a hold by build
                            # uses, and a get is what puts it back.
                            hint = (
                                "; ".join(
                                    f"{name} is held at this build, and its builds use only this "
                                    f"file (--appstore-get --app {name} --version-id {held_id} "
                                    "stages it again)"
                                    for name, held_id in held
                                )
                                + " — delete it anyway with --force"
                            )
                        else:
                            hint = (
                                "deleting it leaves nothing for the next build to use — "
                                "dump the app again first, or delete it anyway with --force"
                            )
                        raise StagedIPAInUseError(message, hint=hint)
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

#: PlayCover's official releases page, which a failed handoff's hint names.
#: Graft doesn't include PlayCover and never downloads or installs it; the
#: person gets it from here. Graft.app's Get PlayCover opens the same page
#: (`PlayCover.releasesURL`, kept equal by `PlayCoverNoticeTests`).
PLAYCOVER_RELEASES_URL = "https://github.com/PlayCover/PlayCover/releases"


#: Where PlayCover keeps its per-app settings, two of which decide whether a
#: handed-over app is usable at all (`playcover_handoff`).
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
#: because the handoff *was*. And since 2.3 only on an app's first handoff:
#: PlayCover 3.1.0 leaves an existing file alone on a reinstall, so turning
#: both off every time was undoing a person's own choice (spec 5d item 1).
#:
#: None means "under this user's home, read when it's needed"; tests set it to
#: a scratch folder. It used to be computed at import, which pinned it to the
#: real home even for a test that had moved HOME somewhere safe.
PLAYCOVER_SETTINGS_DIR: Path | None = None

#: Where PlayCover installs the apps it's handed, `<bundle id>.app` each,
#: which `--install-to-mac` watches for the install (spec 5d item 5). None
#: means under this user's home, read when it's needed; tests set it.
PLAYCOVER_APPS_DIR: Path | None = None


def _playcover_settings_dir() -> Path:
    return PLAYCOVER_SETTINGS_DIR or (
        Path.home() / "Library/Containers/io.playcover.PlayCover/App Settings"
    )


def _playcover_apps_dir() -> Path:
    return PLAYCOVER_APPS_DIR or (
        Path.home() / "Library/Containers/io.playcover.PlayCover/Applications"
    )


def _refuse_what_macos_lacks(
    ipa: Path, origins: dict[str, str] | None, emitter: JsonEmitter | None, *, name: str
) -> None:
    """Refuse a tweak that needs an iOS framework this Mac doesn't have, and
    warn of the app's own binaries that do (spec 5d items 3 and 7).

    `origins` (`playcover.tweak_origins`) says which binaries the tweaks
    brought; None, an archive whose tweaks can't be told, refuses nothing.
    Raises `GraftError` for the refusal and for any failure of the check
    itself (a tweak binary it can't read, a half-written plist), named by
    `name`: never a traceback after minutes of building.
    """
    try:
        found = playcover.check_ipa(ipa, origins)
    except playcover.PlayCoverPrepError as exc:
        raise GraftError(f"couldn't check {name} for PlayCover: {exc.message}", hint=exc.hint) from exc
    except Exception as exc:
        raise GraftError(f"couldn't check {name} for PlayCover: {exc}") from exc
    refused = [link for link in found if link.refuses]
    if refused:
        raise GraftError(playcover.refusal_message(refused))
    for link in found:
        _warn(emitter, playcover.warning_message(link))


def _prepare_for_playcover(
    tweaked: Path, emitter: JsonEmitter | None, *, tweaks: list[Path]
) -> list[str]:
    """Make `tweaked` loadable on this Mac, translating the one error it raises.

    First refuses a tweak among `tweaks` (the app's attached files) that
    needs an iOS framework macOS lacks (`_refuse_what_macos_lacks`): built
    anyway, it is handed over and crashes at launch.

    Extracted from `_build_one` so it can be tested at all: inline, the whole
    branch — including this translation — was a mutation the suite could not
    detect, and it is the only thing between a stub-build failure and a raw
    traceback after minutes of downloading, decrypting and injecting.
    """
    _refuse_what_macos_lacks(tweaked, playcover.tweak_origins(tweaks), emitter, name=tweaked.name)
    try:
        return playcover.prepare_ipa(tweaked)
    except playcover.PlayCoverPrepError as exc:
        raise GraftError(
            f"couldn't prepare {tweaked.name} for PlayCover: {exc.message}",
            hint=exc.hint,
        ) from exc


#: How long a copy handed to PlayCover is left for it to read. `open` returns
#: before PlayCover opens the file and nothing says when it has, so the copy
#: is never removed by the handoff that made it, only by a later one.
_HANDOFF_COPY_MAX_AGE = 24 * 3600


def _handoff_root() -> Path:
    """Where copies handed to PlayCover go: the per-user temp directory, which
    on macOS is owner-only. A function so tests can point it elsewhere."""
    return Path(tempfile.gettempdir())


def _handoff_copy(cfg: Config, archive: Path) -> Path:
    """A private copy of the archive `archive` in built_dir, for PlayCover.

    `open` returns before PlayCover reads the file, so no lock held here could
    cover the read: a sweep (a phone build of the same version finishing, or
    "Clean up old builds...") could move the archive to the Trash before
    PlayCover opened it. So the copy is made under a share of the retention
    lock — no sweep moves the archive mid-copy — and PlayCover gets the copy,
    which no sweep looks at. The archive itself is left exactly as it was
    built, rather than rewritten in place by `_without_account_data`.

    Earlier copies older than `_HANDOFF_COPY_MAX_AGE` are removed first.
    """
    root = _handoff_root()
    now = time.time()
    for old in root.glob("graft-playcover-*"):
        try:
            if now - old.stat().st_mtime > _HANDOFF_COPY_MAX_AGE:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            continue

    hold = _RetentionLock(cfg)
    if not hold.acquire(exclusive=False, wait=_RETENTION_LOCK_TIMEOUT):
        raise GraftError(
            "a clean-up of old builds is holding the archive folder",
            hint="try again in a moment; if no graft is running, "
            f"the process holding {hold.path} is stopped or stuck",
        )
    try:
        # Asked again under the lock: a sweep may have moved it since.
        if not archive.is_file():
            raise ConfigError(
                f"{archive.name} was moved to the Trash by a clean-up of old builds",
                hint="build it again with --for-playcover --skip-install",
            )
        private = Path(tempfile.mkdtemp(prefix="graft-playcover-", dir=root))
        try:
            copy = private / archive.name
            shutil.copyfile(archive, copy)
        except OSError as exc:
            shutil.rmtree(private, ignore_errors=True)
            raise GraftError(
                f"couldn't copy {archive.name} to hand it to PlayCover: {exc}",
                hint=f"check the free space in {root}",
            ) from exc
        except BaseException:
            # A Stop mid-copy: the half-made copy goes too.
            shutil.rmtree(private, ignore_errors=True)
            raise
    finally:
        hold.release()
    return copy


def _tweak_origins_of(cfg: Config, archive: Path) -> dict[str, str] | None:
    """Which binaries in `archive`'s Frameworks/ its tweaks brought
    (`playcover.tweak_origins`), from the build row that archived it, read
    against the tweak files on disk now. None when that can't be told: a
    staged IPA, or an archive no build row names."""
    if archive.parent != cfg.built_dir:
        return None
    db = cfg.state_dir / "log.sqlite"
    if not db.is_file():
        return None
    tweaks = BuildLog(db).tweaks_of(archive)
    if tweaks is None:
        return None
    return playcover.tweak_origins(cfg.tweak_path(t) for t in tweaks)


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

    Before the handoff, a tweak needing an iOS framework macOS lacks refuses
    it (spec 5d items 3 and 7). After it, `handed-to-playcover` goes out at
    once, then this waits, up to two minutes, for PlayCover's copy of the app
    (`playcover_handoff.await_install`), keeps or applies the app's two
    PlayCover settings (`playcover_handoff.settle`) and ends with
    `playcover-installed` or `playcover-install-unconfirmed`, exit 0 either
    way. A Stop (SIGTERM) ends the wait, never the handoff.
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
    origins = _tweak_origins_of(cfg, target)
    from . import lc_manage

    # A Stop (SIGTERM, the app's) during the copy, the scrub or the checks is
    # a Stop, exit 143, not a kill. And a private copy is a whole IPA in the
    # temp folder: unless `open` handed it to PlayCover, it goes, however
    # this ends (the 2.3.1 review's T2-B4). Handed over, it stays for
    # PlayCover to read, until a later handoff's sweep (`_handoff_copy`).
    private: Path | None = None
    handed = False
    with lc_manage.exit_on_termination():
        try:
            if target.parent == cfg.built_dir:
                # Retention can move a built archive at any moment after the
                # lock is let go, PlayCover's read included (`_handoff_copy`).
                target = _handoff_copy(cfg, target)
                private = target.parent

            # PlayCover re-signs it, so all of the buyer's data can go, and in place:
            # `open` returns before PlayCover reads the file, so a temporary copy
            # removed here would be gone by then (K8). A built archive's private copy
            # is the one rewritten; the copy itself stays until a later handoff.
            target = _without_account_data(target, emitter=emitter, workdir=None)
            info = read_info_plist(target)
            _refuse_what_macos_lacks(target, origins, emitter, name=filename)
            version, build = playcover_handoff.archive_versions(target)
            # Before the handoff: the app's PlayCover settings as the person left them.
            before = _settings_before(cfg, info.bundle_id)
            began = time.time()
            result = subprocess.run(
                ["open", "-b", PLAYCOVER_BUNDLE_ID, str(target)],
                capture_output=True, text=True, env=child_env(),
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
                #
                # Except on an Intel Mac, where the release app also runs: there the
                # reason is the processor, and "not installed" would send the user to
                # install something that cannot run. Likewise in the Intel build of
                # Graft on an Apple silicon Mac, which has no PlayCover handover: the
                # fix is the other download. Asked only after the failure, so a
                # working handoff costs nothing extra.
                blocker = host.apple_silicon_blocker()
                if blocker == host.INTEL_MAC:
                    raise ConfigError(
                        f"PlayCover needs an Apple silicon Mac, and {host.INTEL_MAC}",
                        hint=(
                            "PlayCover runs iPhone and iPad apps only on Apple silicon. "
                            "Deliver the build to an iPhone or iPad instead "
                            "(LiveContainer or sideload)"
                        ),
                    )
                if blocker is not None:
                    raise ConfigError(
                        f"PlayCover can't be handed this build here: {blocker}",
                        hint=(
                            "Deliver the build to an iPhone or iPad instead "
                            "(LiveContainer or sideload)"
                        ),
                    )
                # The hint says where PlayCover comes from, which is the fix for the
                # first of the two states, and installing it again is the fix for the
                # second. `open`'s own words follow. Graft.app's Build options says the
                # same before a Mac build starts, when LaunchServices has no app for
                # PLAYCOVER_BUNDLE_ID.
                raise ConfigError(
                    "PlayCover is not installed, or could not launch",
                    hint=(
                        f"get PlayCover from {PLAYCOVER_RELEASES_URL} and install it, "
                        "or install it again if it won't open, then try again. "
                        f"`open -b {PLAYCOVER_BUNDLE_ID}` exited {result.returncode}: "
                        f"{result.stderr.strip() or 'no output'}"
                    ),
                )
            handed = True
        finally:
            if private is not None and not handed:
                shutil.rmtree(private, ignore_errors=True)

    _await_playcover(
        cfg, filename=filename, bundle_id=info.bundle_id, name=info.display_name,
        version=info.version, versions=(version, build), began=began, before=before,
        path=target, emitter=emitter,
    )


def _await_playcover(
    cfg: Config, *, filename: str, bundle_id: str, name: str, version: str,
    versions: tuple[str | None, str | None], began: float,
    before: playcover_handoff.Before, path: Path, emitter: JsonEmitter | None,
) -> None:
    """`--install-to-mac` after a handoff that worked: say so, wait for the
    install, settle the settings, and say how it ended (spec 5d items 1, 2
    and 5)."""
    if emitter:
        # Named for what happened, not for the verb that asked for it:
        # "installed-to-mac" would assert a finished install the `open` can't
        # see. The install is the next event's to say, after the wait.
        emitter.emit("handed-to-playcover", filename=filename, path=str(path),
                     bundle_id=bundle_id, display_name=name)
    else:
        console.print(f"[green]✓[/green] handed {escape(filename)} to PlayCover")
        console.print(f"[dim]  waiting for PlayCover to install {escape(name)}…[/dim]")

    safe = playcover_handoff.safe_bundle_id(bundle_id)
    if not safe:
        # The IPA's own CFBundleIdentifier, which can be anything: no path is
        # built from it (`playcover_handoff.safe_bundle_id`).
        note = (f"can't follow PlayCover's install of {name}: its bundle id "
                f"{bundle_id!r} isn't one Graft looks for")
        if emitter:
            emitter.emit("log", source="playcover", line=note)
        else:
            console.print(f"[dim]  {escape(note)}[/dim]")

    from . import lc_manage

    # A Stop (SIGTERM, the app's) ends the wait as `Stopped`, exit 143. The
    # handoff stands: PlayCover has the file, and only the waiting ends.
    with lc_manage.exit_on_termination():
        try:
            installed = safe and playcover_handoff.await_install(
                _playcover_apps_dir(), bundle_id, version=versions[0], build=versions[1],
                began=began, timeout=playcover_handoff.INSTALL_WAIT_S,
            )
        except Stopped:
            # Best effort, and only what's there already (no waiting): a
            # first handoff whose settings PlayCover has written gets its
            # defaults now, so PlayChain isn't left on until the next one.
            # Nothing here may hide the Stop.
            try:
                stopped = playcover_handoff.settle(
                    _playcover_settings_dir(), cfg.state_dir, bundle_id, before, wait=0,
                )
            except Exception:
                stopped = playcover_handoff.Settled()
            for line in _settings_lines(stopped, name):
                if emitter:
                    emitter.emit("log", source="playcover", line=line)
                else:
                    console.print(escape(line))
            if not emitter:
                console.print("[dim]  stopped waiting; PlayCover may still be installing it[/dim]")
            raise
        try:
            settled = playcover_handoff.settle(
                _playcover_settings_dir(), cfg.state_dir, bundle_id, before,
                wait=playcover_handoff.SETTINGS_WAIT_S,
            )
        except Exception:
            # It says it never raises; should it anyway, the install it
            # follows still stands (the 2.3.1 review's A3).
            settled = playcover_handoff.Settled()

    fields = {"bundle_id": bundle_id, "display_name": name,
              "settings_disabled": settled.disabled, "settings_restored": settled.restored}
    if emitter:
        if installed:
            emitter.emit("playcover-installed", version=version, **fields)
        else:
            waited = int(playcover_handoff.INSTALL_WAIT_S) if safe else 0
            emitter.emit("playcover-install-unconfirmed", waited_s=waited, **fields)
        return
    if installed:
        console.print(f"[green]✓[/green] PlayCover installed {escape(name)} {escape(version)}. "
                      "Open it from PlayCover.")
    else:
        console.print(
            f"PlayCover hasn't finished installing {escape(name)}. Check PlayCover's window; "
            f"if the install failed there, graft --install-to-mac {escape(filename)} tries again."
        )
    if settled.disabled:
        console.print(escape(playcover_handoff.disabled_notice(settled.disabled, name)))
    if settled.restored:
        console.print(f"[dim]{escape(playcover_handoff.restored_line(settled.restored, name))}[/dim]")


def _settings_before(cfg: Config, bundle_id: str) -> playcover_handoff.Before:
    """`playcover_handoff.snapshot`: the app's PlayCover settings before the
    handoff. Should it break (it says it never raises), a `Before` that
    `settle` leaves the settings alone for: they are kept around the handoff,
    never a reason not to make it (the 2.3.1 review's A3)."""
    try:
        return playcover_handoff.snapshot(_playcover_settings_dir(), cfg.state_dir, bundle_id)
    except Exception:
        return playcover_handoff.Before(existed=True, values=None)


def _settings_lines(settled: playcover_handoff.Settled, name: str) -> list[str]:
    """What a settle did, as the sentences the console and the app say."""
    lines = []
    if settled.disabled:
        lines.append(playcover_handoff.disabled_notice(settled.disabled, name))
    if settled.restored:
        lines.append(playcover_handoff.restored_line(settled.restored, name))
    return lines


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
    lc_bundle_id: str | None = None,
    clear_lc_bundle_id: bool = False,
    auto_profile: bool | None = None,
    store_country: str | None = None,
    emitter: JsonEmitter | None = None,
) -> None:
    """Patch an app's options. Only the fields actually passed are written.

    `store_country` is checked and mapped like every country
    (`storefronts.given`): the Edit sheet's store picker, or a hand-run fix
    for a store Search guessed wrong.

    `lc_bundle_id` pins the app to one LiveContainer and `clear_lc_bundle_id`
    removes the pin; either reports `lc_bundle_id` changed. A pin must be an
    id a LiveContainer can have (`lc_discovery.is_livecontainer_id`, any
    case): one naming another app would push the IPA into a Documents folder
    LiveContainer never reads, and the push refuses it anyway.

    `auto_profile` (C-AUTOPROFILE) turns the app's App Store Connect
    provisioning on or off. On is refused on a free config, like everything
    else that signs; off never is. On without an `app_store_connect` key still
    saves — the key can come next — with a warning that builds need it first.
    On for an app delivered by LiveContainer (after this call's own
    `delivery`, if it passes one) also saves, with a warning that it does
    nothing there: a LiveContainer build is never signed, so no profile is
    made or read. That one replaces the no-key warning, whose "its builds
    fail" is not true of a build that does not sign.
    """
    current = cfg.get_app(app_name)  # raises ConfigError if unknown

    if store_country is not None:
        store = storefronts.given(store_country, flag="--store-country")
        _store_note(emitter, store.note)
        store_country = store.code

    if auto_profile and cfg.account == ACCOUNT_FREE:
        raise paid_account_required("--auto-profile", cfg.path)

    if clear_lc_bundle_id and lc_bundle_id is not None:
        raise ConfigError("--lc-bundle-id and --no-lc-bundle-id contradict each other",
                          hint="pass one: an id to pin, or --no-lc-bundle-id to clear the pin")
    if lc_bundle_id is not None:
        from . import lc_discovery

        lc_bundle_id = lc_bundle_id.strip()
        if not lc_discovery.is_livecontainer_id(lc_bundle_id):
            raise ConfigError(
                f"{lc_bundle_id!r} is not a LiveContainer bundle id (they start "
                f"{lc_discovery.FAMILY_PREFIX})",
                hint="`graft --lc-status` lists the LiveContainers on the device; "
                "--no-lc-bundle-id clears the pin",
            )

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
        # "" is the clear: `set_app_options` deletes the key for it, and it is
        # not None, so `changed` still names the field.
        "lc_bundle_id": "" if clear_lc_bundle_id else lc_bundle_id,
        "auto_profile": auto_profile,
        "store_country": store_country,
    }
    if set(supplied) != APP_OPTION_FIELDS:
        raise ValueError(
            "app option fields out of sync with APP_OPTION_FIELDS: "
            f"{sorted(set(supplied) ^ APP_OPTION_FIELDS)}"
        )

    if compression_level is not None and not 0 <= compression_level <= 9:
        raise ConfigError("--compression-level must be between 0 and 9")
    if delivery is not None:
        cfg.require_delivery(delivery)

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
    if auto_profile and (delivery or current.delivery) == "livecontainer":
        _warn(
            emitter,
            f"{app_name} is delivered by LiveContainer, which uses no provisioning "
            "profile: auto_profile has no effect until its delivery is sideload",
        )
    elif auto_profile and current.profile_override is not None:
        # The override wins (`signer._plan_profile`), so without this the
        # save said "changed: auto_profile" and every build went on signing
        # with the hand-set file, until it expired and pre-flight failed.
        _warn(
            emitter,
            f"{app_name} has its own `profile:` in config.yaml "
            f"({current.profile_override}), which signs every build whatever "
            "auto_profile says: remove that line for the profile to be made "
            "automatically",
        )
    elif auto_profile and cfg.app_store_connect is None:
        _warn(
            emitter,
            f"{app_name} will make its own provisioning profile once an App Store "
            "Connect key is set (Settings, or `graft --set-portal`); until then its "
            "builds fail",
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
            "itself came back incomplete. Keep the device unlocked and the app "
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
        "— force-quit it on the device and retry; iOS reaps short-lived extension "
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
                 "fix": c.fix, "blocking": c.blocking, "reason": c.reason}
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
                hint="plug the iPhone or iPad in (or pass --device <udid>)",
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
    if not keychain.secret_fits(password):
        raise _fatal(GraftError(
            f"a root SSH password longer than {keychain.MAX_SECRET_BYTES} bytes isn't supported "
            "(in UTF-8, so a character outside ASCII counts 2 to 4, and a \\ or \" counts 2)"
        ))

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
    cfg.require_paid_account("--set-p12-password")
    # `cfg.team_id`, not the raw field: the Keychain entry is keyed by team
    # id, and `--init` writes the placeholder when it cannot read a profile.
    # Stored under `p12-REPLACEME`, the password was found by `--check` and by
    # no build once the real team id was set.
    team = cfg.team_id
    if team is None:
        raise _fatal(GraftError(
            "set signing.team_id first (`graft --set-settings --signing-team-id <TEAM>`)",
            hint="the password is stored under the team id, so it has to be the real one",
        ))

    if sys.stdin.isatty():
        import getpass
        password = getpass.getpass(f"Enter .p12 password for team {team}: ")
    else:
        # Stdin pipe — read one line, strip the trailing newline only.
        password = sys.stdin.readline().rstrip("\n")

    # Keychain Access can export a .p12 with a blank password, which setup
    # stores as "" and builds read back (combined fix check #17). Empty is
    # taken only when the configured .p12 really opens with it, so a stray
    # Enter still stores nothing.
    if not password and not _opens_with_a_blank_password(cfg.signing.cert):
        raise _fatal(GraftError(
            "no password provided",
            hint="a .p12 exported with a blank password is accepted empty, and this one "
            "doesn't open with an empty password",
        ))
    # Before the write, as the setup does: `security -i` takes one line of
    # 4095 bytes, and what spills past it runs as another command.
    if not keychain.secret_fits(password):
        raise _fatal(GraftError(keychain.P12_PASSWORD_TOO_LONG))

    try:
        keychain.write_p12_password(team, password)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        detail = e.stderr.decode(errors="replace").strip() if isinstance(e, subprocess.CalledProcessError) and e.stderr else str(e)
        raise _fatal(GraftError(f"failed to store password in Keychain: {detail}")) from e
    console.print(f"[green]✓[/green] stored .p12 password in Keychain (service={keychain.SERVICE}, account=p12-{team})")


def _opens_with_a_blank_password(p12_path: Path) -> bool:
    """Whether the .p12 at `p12_path` opens with an empty password, asked of
    `p12.probe`'s child as the setup asks it. No for anything unreadable."""
    from . import p12 as p12_mod
    from .account_setup import read_chosen_file

    try:
        return p12_mod.probe(read_chosen_file(p12_path, ".p12"), b"") == "ok"
    except (GraftError, ValueError, OSError):  # SlowP12Error and P12ProbeError are ValueErrors
        return False


def _read_secret_line(prompt: str) -> str:
    """One secret: typed at a prompt that doesn't echo, or one line of a piped stdin.

    Never a flag or an environment variable. argv is readable by every user
    on the Mac (`ps -axww`), and a variable is inherited by every tool. A
    pipe is how the app sends it (`AccountSetupStore`), and only the trailing
    newline is stripped.
    """
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass(prompt)
    return sys.stdin.readline().rstrip("\n")


def _do_init_account(
    config_path: Path,
    *,
    account: str,
    p12: str | None,
    profile: str | None,
    dry_run: bool,
    emitter: JsonEmitter | None,
    replace: bool = False,
) -> None:
    """`--init --account free|paid` (`account_setup.setup_account`).

    Under --json: one `init-step` per thing done, then one `init-result`.
    Otherwise the same as lines, then the free Apple ID's steps on the device.
    """
    from .account_setup import printable, setup_account

    # Text mode prints to a terminal, and a step names files and a
    # certificate whoever made them chose: no control characters reach it.
    def on_step(step: str, detail: str) -> None:
        if emitter:
            emitter.emit("init-step", step=step, detail=detail)
        else:
            console.print(f"[green]✓[/green] {escape(printable(detail))}")

    try:
        result = setup_account(
            config_path,
            account=account,
            p12_path=Path(p12).expanduser() if p12 else None,
            profile_path=Path(profile).expanduser() if profile else None,
            password=lambda: _read_secret_line("Password for the .p12: "),
            dry_run=dry_run,
            replace=replace,
            on_step=on_step,
        )
    except (Stopped, KeyboardInterrupt) as stop:
        # A Stop that came while the setup's undo ran still stops the run,
        # but what the undo couldn't put back is said first: the stop alone
        # says nothing (the whole-diff review's cli-4). So is why the setup
        # failed, when the undo put everything back (d14-cli-6).
        report = getattr(stop, "undo_incomplete", None) or getattr(stop, "setup_failed", None)
        if report is not None:
            _warn(emitter, f"{report.message}: {report.hint}" if report.hint else report.message)
        raise
    if emitter:
        emitter.emit("init-result", **result.fields())
        return
    console.print(f"[bold]{escape(printable(result.summary))}[/bold]")
    for warning in result.warnings:
        console.print(f"  [yellow]![/yellow] {escape(printable(warning))}")
    if result.pinned:
        console.print(
            f"  delivery: livecontainer written into {escape(', '.join(result.pinned))}, so "
            "they stay in LiveContainer"
        )
    if result.sideloads:
        console.print(
            f"  [yellow]![/yellow] still delivery: sideload, which a free Apple ID cannot "
            f"build: {escape(', '.join(result.sideloads))}. Switch each with `graft "
            "--set-app-options --app <name> --delivery livecontainer`."
        )
    if result.steps:
        console.print("To finish setting up, on the device:")
        for n, step in enumerate(result.steps, 1):
            console.print(f"  {n}. {escape(step)}")


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
        # inside an already-checked one. `events` proves the GUI recognizes a
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
    interpolated span keeps the color and makes only the untrusted part inert.
    """
    err_console.print(f"[red]error:[/red] {escape(e.message)}")
    if e.hint:
        err_console.print(f"  [yellow]hint:[/yellow] {escape(e.hint)}")
    return typer.Exit(code=e.exit_code)


#: pymobiledevice3's "usbmuxd did not answer", by class name: the boundary
#: must not import pymobiledevice3 to recognize it (tests/test_lazy_imports.py).
_USBMUXD_DOWN = "ConnectionFailedToUsbmuxdError"


def _unexpected(e: Exception) -> GraftError:
    """An exception this CLI has no translation for, as a reportable error.

    Named after what it is rather than hidden behind a generic message: the
    class name is often the only diagnosis available (`MuxException`,
    `ModuleNotFoundError`) and the GUI shows this text verbatim.

    One is translated: usbmuxd not answering, which any device verb can meet
    (`--devices`, `--device-apps`, …) and none had a sentence for.
    """
    if any(cls.__name__ == _USBMUXD_DOWN for cls in type(e).__mro__):
        return GraftError(
            "couldn't reach usbmuxd, the macOS service that talks to iPhones and iPads",
            hint="it runs whenever macOS does; unplug and reconnect the iPhone or iPad, and if "
            "this persists restart the Mac. With USBMUXD_SOCKET_ADDRESS set, check that "
            "it points at a running usbmuxd",
        )
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
            # A full disk, said as one, whatever was writing (`presented`).
            raise _fatal(presented(e)) from e
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
            shown = presented(e)
            raise _fatal(shown if isinstance(shown, GraftError) else _unexpected(e)) from e

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


def _verb_check_update(*, current_version: str | None, json_mode: bool, **_: Any) -> None:
    """`--check-update`: one request to GitHub's latest-release API
    (`release_check`), compared with `--current-version` (Graft.app passes its
    own) or this CLI's version.

    Under --json, one `update-check` event whether or not it worked; a failed
    check then exits 1 with its sentence on stderr, like any other failure.
    The app shows that only when someone asked (Help › Check for Updates…).

    A release a download found this Mac's macOS too old for is no update
    for this Mac (`app_update.noted_refusal`): `update_available` false and
    `needs_macos` the macOS it needs.
    """
    current = __version__ if current_version is None else current_version.strip()
    if release_check.parse_version(current) is None:
        raise _fatal(ConfigError(
            f"--current-version takes a version such as 2.0.1, not {current_version!r}"
        ))
    emitter = JsonEmitter() if json_mode else None
    try:
        data = release_check.latest_release_json()
        found = release_check.release_from(data)
    except release_check.UpdateCheckError as e:
        if emitter:
            emitter.emit("update-check", **release_check.event(current, error=e))
        raise
    refused = app_update.noted_refusal(data, found, current)
    fields = release_check.event(current, release=found,
                                 needs_macos=refused.minimum if refused else None)
    if emitter:
        emitter.emit("update-check", **fields)
    elif refused is not None:
        _say_plain(refused.message)
    elif fields["update_available"]:
        _say_plain(f"Graft {found.version} is available (this is {current}): {found.url}")
    else:
        _say_plain(f"Graft {current} is up to date: the latest release is {found.version}.")


def _update_app_path(app_path: str | None, verb: str) -> Path:
    """`--app-path`, or the Graft.app this CLI runs from. A CLI outside an
    app (pipx) has no app of its own to replace, so it must be told."""
    if app_path is not None:
        return Path(app_path)
    from .enrollment import running_app

    found = running_app()
    if found is None:
        raise _fatal(ConfigError(
            f"{verb} needs --app-path: this graft doesn't run from inside a Graft.app"
        ))
    return found


def _verb_download_update(
    *, current_version: str | None, app_path: str | None, json_mode: bool, **_: Any
) -> None:
    """`--download-update`: the latest release's Graft.zip, downloaded into
    Graft's cache and checked (`app_update.download_update`), when it is
    newer than `--current-version` (Graft.app passes its own).

    Under --json, `update-progress` while it works and one
    `update-download-end`, worked or not; a failure then exits 1 with its
    sentence on stderr, like `--check-update`. A Stop (SIGTERM, which is
    what the app's cancel sends) deletes what was downloaded.
    """
    from . import lc_manage

    current = __version__ if current_version is None else current_version.strip()
    if release_check.parse_version(current) is None:
        raise _fatal(ConfigError(
            f"--current-version takes a version such as 2.0.1, not {current_version!r}"
        ))
    app = _update_app_path(app_path, "--download-update")
    emitter = JsonEmitter() if json_mode else None

    def progress(fields: dict[str, Any]) -> None:
        if emitter:
            emitter.emit("update-progress", **fields)

    with lc_manage.exit_on_termination():
        try:
            staged = app_update.download_update(current, app, progress=progress)
        except app_update.UpdateError as e:
            if emitter:
                emitter.emit("update-download-end", **app_update.download_event(current, error=e))
            raise
    if emitter:
        emitter.emit("update-download-end", **app_update.download_event(current, staged=staged))
    else:
        _say_plain(f"Graft {staged.version} is downloaded and checked: {staged.path}")


def _verb_install_update(
    *, install_update: str, app_path: str | None, wait_pid: int | None,
    relaunch: bool | None, json_mode: bool, **_: Any
) -> None:
    """`--install-update PATH`: start the helper that installs PATH over the
    app once `--wait-pid` has quit (`app_update.schedule_install`), and
    return at once. Graft.app runs it as it quits, then quits.

    Under --json, one `update-install`, worked or not; a failure starts
    nothing and exits 1 with its sentence on stderr.
    """
    if wait_pid is None:
        raise _fatal(ConfigError(
            "--install-update needs --wait-pid: the Graft that has to quit first"
        ))
    app = _update_app_path(app_path, "--install-update")
    again = bool(relaunch)
    emitter = JsonEmitter() if json_mode else None
    try:
        scheduled = app_update.schedule_install(
            Path(install_update), app, pid=wait_pid, relaunch=again,
        )
    except app_update.UpdateError as e:
        if emitter:
            emitter.emit("update-install", **app_update.install_event(relaunch=again, error=e))
        raise
    if emitter:
        emitter.emit("update-install", **app_update.install_event(
            version=scheduled.version, log=scheduled.log, relaunch=again,
        ))
    else:
        _say_plain(
            f"Graft {scheduled.version} will be installed once process {wait_pid} quits. "
            f"Its log: {scheduled.log}"
        )


def _verb_init(
    *,
    config_path: Path,
    json_mode: bool,
    dry_run: bool,
    init_account: str | None,
    init_p12: str | None,
    init_profile: str | None,
    init_replace: bool,
    **rest: Any,
) -> None:
    """`--init`. With `--account`, the setup the Mac app runs: no questions,
    the .p12 password on stdin (`_do_init_account`). Without it, the
    interactive setup in Terminal (`init_flow.run_init`). That one can neither
    preview nor answer in events, so `--json`, `--dry-run`, `--p12`,
    `--profile` and `--replace` are refused there rather than dropped.

    `--config` too: an upgrade from free to paid edits the config the refusal
    named, and `config.paid_upgrade` names it that way.
    """
    if init_account is None:
        given = [flag for flag, value in (
            ("--json", json_mode), ("--dry-run", dry_run),
            ("--p12", init_p12), ("--profile", init_profile), ("--replace", init_replace),
        ) if value]
        if given:
            raise ConfigError(
                f"{', '.join(given)} with --init needs --account free or --account paid",
                hint="without --account, --init asks its questions in Terminal",
            )
        run_init(console, cfg_path=config_path)
        return
    # `--init` sits above `_StrayFlagGate`, so the scripted setup refuses a
    # verb-only flag itself: dropped, `--default-device X` or `--force` would
    # read as done (F9). The interactive `--init` keeps ignoring them, as it
    # always has.
    stray = sorted(
        _flag_as_typed(flag, rest[param]) for flag, param in _VERB_ONLY_FLAGS.items()
        if rest.get(param) is not None
    )
    if stray:
        raise ConfigError(
            f"{', '.join(stray)} {'is' if len(stray) == 1 else 'are'} not valid with "
            "--init --account",
            hint="the setup takes --account, --p12, --profile, --replace, --dry-run and "
            "--json; change the rest afterward, with --set-settings or in Graft.app",
        )
    # A Stop, the app quitting (SIGTERM) or a hang-up is a `Stopped`, which
    # the setup's undo catches like any failure: the default disposition
    # would kill the process between two writes. A reader that hung up is a
    # BrokenPipeError at the next event (SIGPIPE is ignored), caught the same.
    from . import lc_manage

    with lc_manage.exit_on_termination():
        _do_init_account(
            config_path, account=init_account, p12=init_p12, profile=init_profile,
            dry_run=dry_run, replace=init_replace, emitter=JsonEmitter() if json_mode else None,
        )


def _verb_setup_portal(*, config_path: Path | None = None, **_: Any) -> None:
    from .setup_portal import run_setup_portal

    # `--config` too, as `--init` takes it: the wizard refuses a free config,
    # and writes the key into the config it was given, not the default one.
    run_setup_portal(console, cfg_path=config_path)


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
        # First, and once, whatever the app count: a fresh free install has no
        # apps, so the `account` on each `app` below cannot tell the GUI its
        # mode, and its Add App sheet would offer sideload to an account that
        # cannot build one.
        em.emit("account", account=cfg.account)
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
                # The per-app `profile:` path, null without one. It outranks
                # auto_profile, so the app shows it rather than "auto".
                profile_override=str(a.profile_override) if a.profile_override else None,
                keep_bundle_id=a.cyan.keep_bundle_id,
                strip_extensions=a.cyan.strip_extensions,
                strip_supported_devices=a.cyan.strip_supported_devices,
                compression_level=a.cyan.compression_level,
                # The LiveContainer pin, null when unpinned. Always present:
                # the app shows its LiveContainer picker only when the key is.
                lc_bundle_id=a.lc_bundle_id,
                # The App Store it was found in (`store_country`), which its
                # lookups ask; null when unset (the US store). The Edit sheet's
                # store picker starts here.
                store_country=a.store_country,
                # `free` or `paid`: the same on every app, and the same as
                # the `account` event above. Kept so a GUI built before that
                # event still learns the mode from a config with apps.
                account=cfg.account,
                # Whether installing it needs Developer Mode on the phone:
                # true when it's signed with a development profile, false for
                # ad hoc, enterprise or LiveContainer, null when its profile
                # can't be read (`developer_mode.needed`, which the build gate
                # asks too). The app can't read a profile, and the device
                # page's Developer Mode row shows only when some app is true.
                needs_developer_mode=developer_mode.needed(cfg, a),
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
    results = updates.statuses(cfg, records)

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
        # Each cell is a .deb's or a repo's own text: escaped (see the note
        # above `_do_add_repo`).
        where = status.available.source
        if not status.available.verified:
            where += f" (needs your OK: {status.available.unverified_reason})"
        table.add_row(*(escape(cell) for cell in (
            status.filename, status.installed or "?", status.available.version, where,
        )))
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
        swapped = swapped_tweaks(app.tweaks, key=cfg.tweak_key, old_key=old_key,
                                 new_filename=new_filename)
        set_app_tweaks(cfg.path, app_name, swapped)
        done.append(app_name)


def _do_update_tweak(
    cfg: Config, filename: str, *, emitter: JsonEmitter | None = None, trusted_key: str | None = None
) -> None:
    """Download the newest build of `filename`'s package and re-point its apps.

    Only from the tweak's own source (`updates.statuses`), and only a
    verified update unless the person confirmed it (`trusted`:
    `--trust-tweak` naming this file): otherwise refused before anything is
    downloaded. The download is checked and named by `tweak_sources.place`,
    and its source recorded for the new file.

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

    status = updates.statuses(cfg, [record])[0]
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

    available = status.available
    if not available.verified:
        key = status.confirm_key or ""
        if trusted_key is None:
            raise tweak_sources.untrusted(filename, available.version, available.source,
                                          available.unverified_reason, key)
        if trusted_key != key:
            # The person said yes to another update of it (re-verify #2).
            raise tweak_sources.trust_changed(filename, available.version, available.source)
    apps = list(record.get("used_by") or [])

    rollback_dir = cfg.state_dir / "tweak-rollback"
    rollback = rollback_dir / filename
    with update_tweaks.workdir(cfg.tweaks_dir) as work:
        # Exactly the builds whose version was compared, from the tweak's own
        # source, each against its own hash (`updates.download`). Resolving
        # the package id afresh here is what downloaded a stale 1.7.20 after
        # the check said 1.7.24, and took the highest version from any repo.
        downloaded = updates.download(available, work)
        tweak_sources.checked_name(downloaded, cfg.tweaks_dir, package_id=status.package_id,
                                   version=available.version)
        # Copied once the download is in hand: a failed download replaced
        # nothing, and a copy then is a rollback of nothing, one per attempt.
        # Kept outside `tweaks_dir` so it is not itself listed as a tweak.
        rollback_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cfg.tweaks_dir / filename, rollback)
        try:
            dest = tweak_sources.place(downloaded, cfg.tweaks_dir, package_id=status.package_id,
                                       version=available.version)
        except Exception:
            rollback.unlink(missing_ok=True)
            raise
    update_tweaks.record_source(cfg, dest, available.origin)

    # The download is byte for byte the file already in use, under Graft's
    # name: every app already points at it, and re-pointing is nothing.
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
    *, cfg: Config, update_tweak: str | None, trust_tweak: list[str] | None, json_mode: bool,
    **_: Any,
) -> None:
    filename = update_tweak or ""
    keys = _trust_keys(trust_tweak)
    if keys and set(keys) != {filename}:
        raise ConfigError(
            f"--trust-tweak with --update-tweak is the confirm_key of the tweak being updated, {filename}",
            hint="graft --tweak-updates --json gives it; nothing was run",
        )
    _do_update_tweak(cfg, filename, emitter=JsonEmitter() if json_mode else None,
                     trusted_key=keys.get(filename))


def _trust_keys(values: list[str] | None) -> dict[str, str]:
    """`--trust-tweak`'s values, each a tweak's `confirm_key` (`FILE@KEY`),
    by filename. A bare filename is refused: it would trust whatever that
    file's update had turned into by the time it ran (re-verify #2)."""
    keys: dict[str, str] = {}
    for value in values or []:
        filename = updates.confirm_key_file(value)
        if filename is None:
            raise ConfigError(
                f"--trust-tweak takes a tweak's confirm_key (FILE@KEY), not {value!r}",
                hint="graft --update … --plan --json and graft --tweak-updates --json give each "
                     "tweak's confirm_key; nothing was run",
            )
        if keys.get(filename, value) != value:
            raise ConfigError(f"--trust-tweak names {filename} twice, with two keys",
                              hint="nothing was run")
        keys[filename] = value
    return keys


def _update_pipeline(emitter: JsonEmitter | None) -> Any:
    """What `update_run` borrows from this module (`update_run.Pipeline`)."""
    from . import update_run

    def build(*, cfg: Config, app_name: str, device: str | None, emitter: JsonEmitter | None,
              built: update_run.Built | None = None) -> None:
        _build_one(cfg=cfg, app_name=app_name, dry_run=False, skip_install=False,
                   skip_decrypt_check=False, verbose=False, device=device, emitter=emitter,
                   outcome=built)

    def install(*, cfg: Config, app_name: str, device: str, built: update_run.Built,
                emitter: JsonEmitter | None) -> None:
        _do_update_install(cfg=cfg, app_name=app_name, device=device, built=built, emitter=emitter)

    return update_run.Pipeline(
        tweak_records=_collect_tweak_records,
        delivery=_delivery_targets,
        stage_ipa=_stage_ipa,
        build=build,
        install=install,
        say=_say_plain,
        warn=lambda message: _warn(emitter, message),
    )


def _verb_update(
    *,
    cfg: Config,
    apps: list[str] | None,
    update_all: bool | None,
    plan_only: bool | None,
    quick_plan: bool | None,
    with_affected: bool | None,
    trust_tweak: list[str] | None,
    json_mode: bool,
    **_: Any,
) -> int:
    """`--update`: the plan (`--plan`), or the plan and the work (`update_run`).

    `--trust-tweak FILE`: the person's OK for that tweak's update although it
    isn't verified (2.3.1 A1). Each must be a library tweak: a typo would
    otherwise trust nothing, without a word."""
    from . import update_readiness, update_run

    names = list(apps or [])
    if update_all and names:
        raise ConfigError(
            "--update takes app names or --all, not both",
            hint="graft --update example, or graft --update --all",
        )
    if not update_all and not names:
        raise ConfigError(
            "--update needs the apps to update, or --all",
            hint="graft --update --all --plan says what would change, and changes nothing",
        )
    if quick_plan and not plan_only:
        raise ConfigError(
            "--quick works only with --plan: an update looks at the device properly before it "
            "changes anything",
        )
    trusted = _trust_keys(trust_tweak)
    if trusted:
        library = {r["filename"] for r in _collect_tweak_records(cfg)}
        unknown = sorted(set(trusted) - library)
        if unknown:
            raise TweakNotFoundError(
                f"--trust-tweak names {', '.join(unknown)}, which isn't in your tweaks library",
                hint="graft --update … --plan --json gives each tweak's confirm_key",
            )
    emitter = JsonEmitter() if json_mode else None
    pipeline = _update_pipeline(emitter)
    targets = sorted(cfg.apps) if update_all else names
    if plan_only:
        probe = (update_readiness.QuickProbe(cfg.state_dir) if quick_plan
                 else update_readiness.FullProbe(cfg.state_dir))
        update_run.show_plan(cfg, targets, quick=bool(quick_plan), with_affected=bool(with_affected),
                             probe=probe, pipeline=pipeline, emitter=emitter, trusted=trusted)
        return 0
    return update_run.run(cfg, targets, all_apps=bool(update_all), with_affected=bool(with_affected),
                          probe=update_readiness.FullProbe(cfg.state_dir), pipeline=pipeline,
                          emitter=emitter, trusted=trusted)


def _verb_drop_update_target(
    *, cfg: Config, drop_update_target: str | None, device: str | None, json_mode: bool, **_: Any
) -> None:
    """`--drop-update-target APP --device UDID`: stop updating APP on that
    device until that app is delivered there again (spec §11.3, "Devices
    that are gone"). Changes no device and reads no keychain."""
    app_name = drop_update_target or ""
    if not device:
        raise ConfigError(
            "--drop-update-target needs --device: the device to stop updating it on",
            hint=f"graft --update {app_name or '<app>'} --plan lists its devices",
        )
    name = _drop_update_target(cfg, app_name, device)
    if json_mode:
        JsonEmitter().emit("update-target-dropped", app=app_name, udid=device, device_name=name)
        return
    console.print(
        f"Graft won't update [bold]{escape(app_name)}[/bold] on {escape(name or device)} "
        f"({escape(device)}) until it's installed there again."
    )


def _verb_search(*, search: str | None, json_mode: bool, **_: Any) -> None:
    _do_search(search, emitter=JsonEmitter() if json_mode else None)


def _verb_fetch(
    *, cfg: Config, fetch: str | None, fetch_sha: str | None, fetch_repository: str | None,
    json_mode: bool, **_: Any
) -> None:
    if fetch_repository and canister._URL_RE.match(fetch or ""):
        raise ConfigError(
            "--fetch-repository goes with a package id, not a URL",
            hint="a URL names its own repo; nothing was run",
        )
    _do_fetch(cfg, fetch, emitter=JsonEmitter() if json_mode else None, expected_sha=fetch_sha,
              repository_id=fetch_repository)


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
        delivery=add_app_delivery or cfg.default_delivery,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_add_app_manual(
    *,
    cfg: Config,
    add_app_name: str | None,
    add_app_bundle_id: str | None,
    add_app_display_name: str | None,
    add_app_delivery: str | None,
    add_app_store_country: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    # Trim before the truthiness check — `--name "   "` would otherwise
    # slip past as a "non-empty" name and slugify to "app".
    trimmed_name = (add_app_name or "").strip()
    trimmed_bundle = (add_app_bundle_id or "").strip()
    if not trimmed_name or not trimmed_bundle:
        raise _fatal(ConfigError("--add-app-manual requires --name and --bundle-id"))
    store_country = None
    if add_app_store_country is not None:
        # Checked and mapped like every country (`storefronts.given`): a code
        # with no store would only make every later lookup of the app a 400.
        try:
            store = storefronts.given(add_app_store_country, flag="--store-country")
        except ConfigError as e:
            raise _fatal(e) from e
        _store_note(JsonEmitter() if json_mode else None, store.note)
        store_country = store.code
    _do_add_app_manual(
        cfg,
        name=trimmed_name,
        bundle_id=trimmed_bundle,
        display_name=add_app_display_name,
        delivery=add_app_delivery or cfg.default_delivery,
        store_country=store_country,
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
    again: bool | None,
    version_id: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    """The latest version, or with `--version-id` the one it names. A get of
    a chosen version holds the app at it (`appstore.hold`); a get of the
    latest lets go (`appstore.release_hold`). Either only once it worked,
    including when the version was already staged: a failed get changes
    nothing. A preferred version changed while it ran (Edit App) is left as
    it is, with a warning."""
    if not provide_ipa_app:
        raise ConfigError("--appstore-get requires --app <name>")
    if version_id is not None and not ipatool.is_external_version_id(version_id.strip()):
        raise ConfigError(
            f"--version-id takes an App Store version id, a number, not {version_id!r}",
            hint=f"graft --appstore-versions --app {provide_ipa_app} lists them",
        )
    # As a number, the way a listing writes it: `0800200` is the row 800200,
    # and the id names that build's file and its hold (`staged_name`).
    chosen = str(int(version_id.strip())) if version_id is not None else None
    emitter = JsonEmitter() if json_mode else None

    def warn(message: str) -> None:
        _warn(emitter, message)

    app = cfg.get_app(provide_ipa_app)
    # The version asked for already staged: nothing to download, unless
    # `--again` asks for it anyway. Any doubt downloads (fails open).
    if again:
        staged = None
    elif chosen is not None:
        staged = appstore.already_staged_version(cfg, app, chosen)
    else:
        staged = appstore.already_staged(cfg, app)
    if staged is not None:
        appstore.report_already_staged(
            provide_ipa_app, staged, emitter=emitter, say=_say_plain, version_id=chosen,
        )
        version = staged.version
        # That build's own file holds by build; the plain file of a version
        # with one build holds by version (`appstore.already_staged_version`).
        held_id = staged.external_version_id
    else:
        with appstore.fetched(
            cfg, provide_ipa_app, emitter=emitter,
            allow_unverified=bool(allow_unverified), again=bool(again), version_id=chosen,
            say=_say_plain, warn=warn,
        ) as decrypted:
            version = _stage_ipa(
                cfg, app_name=provide_ipa_app, source_path=decrypted, emitter=emitter,
                external_version_id=chosen,
            )
        held_id = chosen
    # The App Store gave this Apple ID the app after all: Update offers its
    # gets again (`update_refusals`).
    from . import update_refusals

    update_refusals.forget(cfg.state_dir, provide_ipa_app)
    if chosen is not None:
        appstore.hold(
            cfg, provide_ipa_app, version, version_id=held_id,
            emitter=emitter, say=_say_plain, warn=warn,
        )
    else:
        appstore.release_hold(cfg, provide_ipa_app, emitter=emitter, say=_say_plain, warn=warn)


def _verb_appstore_versions(
    *,
    cfg: Config,
    provide_ipa_app: str | None,
    search_limit: int | None,
    versions_offset: int | None,
    json_mode: bool,
    **_: Any,
) -> None:
    if not provide_ipa_app:
        raise ConfigError("--appstore-versions requires --app <name>")
    limit = appstore.VERSIONS_LIMIT if search_limit is None else search_limit
    if not 0 <= limit <= appstore.VERSIONS_LIMIT_MAX:
        raise ConfigError(
            f"--limit with --appstore-versions is how many versions to read the details of, "
            f"0 to {appstore.VERSIONS_LIMIT_MAX}, not {limit}"
        )
    offset = versions_offset or 0
    if offset < 0:
        raise ConfigError(f"--offset can't be negative ({offset})")
    appstore.list_versions(
        cfg, provide_ipa_app, offset=offset, limit=limit,
        emitter=JsonEmitter() if json_mode else None, say=_say_plain,
    )


def _verb_appstore_claim(
    *, cfg: Config, provide_ipa_app: str | None, json_mode: bool, expect_email: str | None, **_: Any
) -> None:
    if not provide_ipa_app:
        raise ConfigError("--appstore-claim requires --app <name>")
    if expect_email is not None and not expect_email.strip():
        raise ConfigError("--expect-email needs the Apple ID's email")
    appstore.claim(
        cfg, provide_ipa_app, emitter=JsonEmitter() if json_mode else None, say=_say_plain,
        expect_email=expect_email,
    )
    # The Apple ID has the app now: Update offers its gets again
    # (`update_refusals`), as after a successful get. A refused claim raised.
    from . import update_refusals

    update_refusals.forget(cfg.state_dir, provide_ipa_app)


def _stdin_fd() -> int:
    """This process's stdin as a descriptor, for the secret readers, which
    read bytes into wipeable buffers rather than through Python's text layer."""
    try:
        return sys.stdin.fileno()
    except (AttributeError, OSError, ValueError) as e:
        raise ConfigError("--appstore-login reads the Apple ID and password on stdin, and there is none") from e


def _verb_appstore_login(*, cfg: Config, json_mode: bool, expect_ipatool: str | None, **_: Any) -> None:
    from . import appstore_login, lc_manage
    from .secret_input import prompt_line

    fd = _stdin_fd()
    # A Stop raises `Stopped` here instead of killing the process where it
    # stands, so ipatool is stopped and the secrets wiped on the way out.
    with lc_manage.exit_on_termination():
        appstore_login.run(
            cfg,
            emitter=JsonEmitter() if json_mode else None,
            say=_say_plain,
            read_line=lambda prompt, timeout, echo=False: prompt_line(
                fd, prompt, timeout=timeout, echo=echo
            ),
            expect_ipatool=Path(expect_ipatool) if expect_ipatool is not None else None,
        )


def _verb_appstore_logout(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    from . import appstore_login

    appstore_login.sign_out(cfg, emitter=JsonEmitter() if json_mode else None, say=_say_plain)


def _verb_appstore_enroll_candidates(*, cfg: Config, json_mode: bool, **_: Any) -> None:
    from . import enrollment

    found = enrollment.candidates()
    emitter = JsonEmitter() if json_mode else None
    if emitter:
        for c in found:
            emitter.emit(
                "appstore-enroll-candidate",
                path=str(c.path), name=c.name, bundle_id=c.bundle_id, opened=c.opened,
            )
        emitter.emit("appstore-enroll-candidates-end", count=len(found))
        return
    if not found:
        _say_plain(enrollment.NO_CANDIDATES)
    for c in found:
        _say_plain(f"{c.name}  {c.path}" + ("" if c.opened else "  (open it once first)"))


def _verb_appstore_enroll_command(
    *,
    cfg: Config,
    provide_ipa_app: str | None,
    allow_unverified_helper: bool | None,
    json_mode: bool,
    **_: Any,
) -> None:
    from . import enrollment
    from .errors import AppStoreError

    if not provide_ipa_app:
        raise ConfigError("--appstore-enroll-command requires --app <path to the installed app>")
    # Graft.app's own UnfairPlay, checked against its team, when this CLI is
    # Graft.app's; otherwise one nothing can check (`root_helper`).
    helper = enrollment.root_helper(cfg.appstore)
    command = enrollment.enroll_command(helper, Path(provide_ipa_app), cfg.appstore.unfairplay_models)
    emitter = JsonEmitter() if json_mode else None
    if not command.verified and not allow_unverified_helper:
        # Root would run an UnfairPlay nothing checks: only with consent
        # (review #1). The app answers this event with a confirmation that
        # says why, then runs again with the flag.
        if emitter:
            emitter.emit(
                "appstore-refused",
                app=str(command.app), reason=enrollment.REFUSED_UNVERIFIED_HELPER,
                kind=command.unverified_kind, helper=str(command.helper),
                message=command.unverified_reason,
            )
        raise AppStoreError(
            f"Graft won't print this enrollment script on its own: {command.unverified_reason}",
            hint=(
                f"root would copy and run {command.helper}; to accept that, run again with "
                f"{enrollment.ALLOW_FLAG}"
            ),
        )
    if emitter:
        emitter.emit(
            "appstore-enroll-command",
            app=str(command.app), model_dir=str(command.model_dir),
            script=command.script, verified=command.verified,
            helper=str(command.helper), unverified_kind=command.unverified_kind,
            unverified_reason=command.unverified_reason,
            cancel_file=str(command.cancel_file), time_limit_seconds=command.time_limit_seconds,
        )
        return
    # The script, byte for byte, is all that goes to stdout, so `> file`
    # saves exactly what was built: rich would wrap it at the terminal's
    # width (splitting a command in two) and turn `:name:` into an emoji.
    # Everything said about it goes to stderr.
    if not command.verified:
        _warn(None, command.unverified_reason)
    err_console.print(
        "Save the script on stdout to a file (`graft --appstore-enroll-command --app ... > enroll.sh`), "
        "run it as an administrator with `sudo /bin/sh enroll.sh`, then run "
        "`graft --appstore-enroll-finish <the folder it prints>`. It stops by itself after "
        f"{command.time_limit_seconds} seconds.",
        markup=False, highlight=False, emoji=False,
    )
    sys.stdout.write(command.script)
    sys.stdout.flush()


def _verb_appstore_enroll_finish(
    *, cfg: Config, appstore_enroll_finish: str | None, json_mode: bool, **_: Any
) -> None:
    from . import enrollment

    model_dir = cfg.appstore.unfairplay_models
    names = enrollment.finish(Path(appstore_enroll_finish or ""), model_dir)
    if json_mode:
        JsonEmitter().emit("appstore-enrolled", model_dir=str(model_dir), count=len(names))
    else:
        _say_plain(f"this Mac is enrolled for decryption: {len(names)} file(s) in {model_dir}")


def _verb_appstore_search(
    *,
    cfg: Config,
    appstore_search_term: str | None,
    search_country: str | None,
    search_limit: int | None,
    json_mode: bool,
    **_: Any,
) -> None:
    term = (appstore_search_term or "").strip()
    if not term:
        raise _fatal(ConfigError(
            "--appstore-search needs something to search for",
            hint='name the app, e.g. graft --appstore-search "photo editor"',
        ))
    _do_appstore_search(
        cfg, term, country=search_country, limit=search_limit,
        emitter=JsonEmitter() if json_mode else None,
    )


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


def _verb_export_ipa(
    *,
    cfg: Config,
    export_ipa: str | None,
    export_overwrite: bool | None,
    apps: list[str] | None,
    dry_run: bool,
    skip_decrypt_check: bool,
    verbose: bool,
    json_mode: bool,
    **_: Any,
) -> int | None:
    """`--export-ipa PATH APP`: the build of APP as far as cyan, written to PATH unsigned.

    For people who sign on the phone (an on-device signer and a certificate
    service) or install with TrollStore. It runs inside the same envelope and
    stages as a build (`_build_one`, `export_to`), so it reports the same way
    and stops the same way, and it never signs, archives, or looks for a device.

    **No history row, by decision.** The build history is the record of what
    Graft archived and can ship again: Re-install, Install to Mac and Reveal
    act on a row's `ipa_path`, and the `archive_keep` sweep keeps or trashes
    archives by those rows. An export's file is the user's, at a path they
    chose and may move or delete, and it is unsigned: a row naming it would be
    a Re-install that ships an unsigned IPA, or a file the sweep could reach.
    Nor is it a delivery, so no delivery row, and the delivery badges don't
    move. The stream it answers with, ending in `ipa-exported`, is its record;
    and with no row to reach a saved log from, it keeps none.
    """
    if not apps or apps[1:]:
        # A build's usage error, and for its reasons (`_verb_build_apps`): no
        # build was asked for, so nothing is reported as one.
        if json_mode:
            err_console.print("usage: graft APP --export-ipa PATH   (one app: an export writes one IPA)")
        else:
            console.print("usage: graft APP --export-ipa PATH   (one app: an export writes one IPA)")
        return 64
    # As a build is: a Stop (SIGTERM) raises `Stopped`, so the envelope reports
    # it and the write's temp file is removed on the way out.
    from . import lc_manage

    with lc_manage.exit_on_termination():
        _build_one(
            cfg=cfg,
            app_name=apps[0],
            dry_run=dry_run,
            skip_install=True,
            skip_decrypt_check=skip_decrypt_check,
            verbose=verbose,
            device=None,
            emitter=JsonEmitter() if json_mode else None,
            export_to=_export_path(export_ipa or ""),
            overwrite=bool(export_overwrite),
        )


def _export_path(given: str) -> Path:
    """`--export-ipa`'s PATH, absolute when it can be made so here.

    Ahead of the envelope, so `build-start` names the file the export will
    write rather than a relative path. Resolving it can raise (a working
    directory that is gone, `~nobody`); that leaves it as given, and the
    envelope's own check reports it (`_check_export_destination`).
    """
    path = Path(given)
    try:
        return path.expanduser().absolute()
    except (OSError, RuntimeError):
        return path


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


def _resolved_dirs(config_path: Path, raw: dict[str, Any] | None = None) -> dict[str, str]:
    """Where this config puts everything, as `load_config` resolves it.

    Reported on `--settings` so the GUI's Paths section names the directories
    the CLI actually uses, which a hardcoded `~/.config/graft` does not for
    anyone with `XDG_*_HOME` set or their own `cache_dir` / `tweaks_dir`.

    Read from the raw YAML, like `read_settings`, so it answers for a config
    `load_config` refuses. The four lines below are `load_config`'s own, and
    `tests/test_cli_settings_paths.py` compares the two so they cannot drift.
    """
    raw = _load_raw(config_path) if raw is None else raw
    return {
        "config_dir": str(config_path.parent),
        "cache_dir": str(_expand(raw.get("cache_dir") or default_cache_dir())),
        "built_dir": str(_expand(raw.get("built_dir") or default_built_dir())),
        "state_dir": str(_expand(raw.get("state_dir") or default_state_dir())),
        "tweaks_dir": str(_expand(raw.get("tweaks_dir") or default_tweaks_dir())),
    }


def _do_settings(config_path: Path, *, emitter: JsonEmitter | None = None) -> None:
    """Report the settings `--set-settings` can write.

    Takes the path, not a `Config`. Loading one validates the whole document
    and raises when `signing:` is incomplete — which is precisely the state
    this pane exists to repair, so requiring a valid config made the editor
    inert on every config that needed it. A missing field reports as null.

    Under --json, a config.yaml that exists but does not parse (a YAML syntax
    error, or no mapping at the top) still gets its `settings` event: every
    setting null, and the paths a config with nothing in it resolves to, so
    the pane can still name the file to fix. The exit is 0, because the app
    discards a failed run's events. Without --json, and for a config that is
    not there at all, it stays the error it was.

    The parse error rides on the event (C-SETTINGS): `parse_error`, the
    message on one line (null when it parsed), and `paths_are_defaults`, true
    when `paths` are those defaults rather than the config's own. It used to
    go out as a `log` warning, which the pane's store drops: the pane showed
    default paths as the config's and promised "saving replaces it" for a
    file `--set-settings` refuses to write. stderr still gets the warning.

    `app_store_connect` is left out of a parse error's event, not null: the
    app reads a present null as "no key set", which a config it cannot read
    does not say, and an absent key as "this CLI doesn't report it", which
    hides the section while the parse-error banner explains.
    """
    parse_error: str | None = None
    try:
        payload = read_settings(config_path)
        paths = _resolved_dirs(config_path)
        asc = {"app_store_connect": read_app_store_connect(_load_raw(config_path))}
    except ConfigError as e:
        if emitter is None or not config_path.is_file():
            raise
        parse_error = " ".join(e.message.split())
        _warn(emitter, f"{parse_error} — settings show as unset until it parses",
              carried_by="settings")
        payload = dict.fromkeys(SETTINGS)
        paths = _resolved_dirs(config_path, raw={})
        asc = {}
    if emitter:
        emitter.emit("settings", **payload, paths=paths, parse_error=parse_error,
                     paths_are_defaults=parse_error is not None, **asc,
                     graft_version=__version__)
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
        _emit_settings_echo(config_path, emitter)
    else:
        if not changed:
            console.print("[dim]nothing to change[/dim]")
        for key in changed:
            console.print(f"✓ {key}")


def _emit_settings_echo(config_path: Path, emitter: JsonEmitter) -> None:
    """The `settings` a writer emits after its write, so the GUI need not ask.

    `--settings`' shape less `paths`: a write only lands in a config that
    parsed, so there is no parse error to carry. Best-effort, and swallowing
    the error is the point — the write already landed on disk, and a failed
    *read* exiting non-zero would report a save that succeeded as failed. A
    caller that gets no `settings` event runs `--settings` itself.

    `app_store_connect` rides on it for the same reason it is on `--settings`:
    the pane replaces its record with this one, and an echo without the key
    would show "not set up" after every save. `graft_version` too, which the
    pane shows as "graft <version>".
    """
    try:
        emitter.emit(
            "settings", **read_settings(config_path), parse_error=None,
            paths_are_defaults=False,
            app_store_connect=read_app_store_connect(_load_raw(config_path)),
            graft_version=__version__,
        )
    except (ConfigError, OSError):
        pass


def _do_set_portal(
    config_path: Path,
    *,
    key_id: str | None,
    issuer_id: str | None,
    key_file: str | None,
    emitter: JsonEmitter | None = None,
) -> None:
    """`--set-portal`: the `--setup-portal` wizard's work, without its prompts.

    See `setup_portal.set_portal`. Under --json: `portal-set`, then the
    resulting `settings`. A key App Store Connect turns away is its
    `PortalError` (exit 10) with nothing written.
    """
    from .setup_portal import other_key_copies, set_portal

    result = set_portal(config_path, key_id=key_id, issuer_id=issuer_id, key_file=key_file)
    _warn_about_key_copies(other_key_copies(config_path, keep=Path(result["key_file"])), emitter)
    if emitter:
        emitter.emit("portal-set", **result)
        _emit_settings_echo(config_path, emitter)
    else:
        console.print(
            f"[green]✓[/green] App Store Connect key {escape(result['key_id'])} works — "
            f"it sees {result['devices']} iOS device(s)"
        )
        console.print(f"[dim]  key copied to {escape(result['key_file'])}[/dim]")


def _warn_about_key_copies(copies: list[Path], emitter: JsonEmitter | None) -> None:
    """Name the private keys still in the config folder that nothing points
    at (`setup_portal.other_key_copies`), so none sits there unmentioned."""
    from .setup_portal import key_copies_warning

    said = key_copies_warning(copies)
    if said:
        _warn(emitter, said)


def _do_clear_portal(config_path: Path, *, emitter: JsonEmitter | None = None) -> None:
    """`--clear-portal`: remove the App Store Connect setting, keep the .p8.

    Not refused on a free config, unlike setting one: removing a key is never
    harmful. Clearing a config with no key is a no-op that says so, not an
    error. Sideload apps still set to `auto_profile` are named in a warning,
    because their next build fails without a key; a LiveContainer app never
    signs, so it is not (`setup_portal.clear_portal`).
    """
    from .setup_portal import clear_portal, other_key_copies

    removed, auto, unreadable = clear_portal(config_path)
    key_file = None
    if removed and removed.get("key_file"):
        key_file = str(_expand(removed["key_file"]))
    # The removed key's own file is reported below, as before.
    _warn_about_key_copies(
        other_key_copies(config_path, keep=Path(key_file) if key_file else None), emitter
    )
    if unreadable:
        _warn(
            emitter,
            "couldn't tell which apps still create their provisioning profile "
            f"automatically: {unreadable}",
        )
    if auto:
        _warn(
            emitter,
            f"{', '.join(auto)} still create their provisioning profile automatically "
            "(auto_profile); their builds fail until a key is set again or that is turned off",
        )
    if emitter:
        emitter.emit("portal-cleared", removed=removed is not None, key_file=key_file)
        _emit_settings_echo(config_path, emitter)
    elif removed is None:
        console.print("[dim]no App Store Connect key was set[/dim]")
    else:
        console.print("[green]✓[/green] removed the App Store Connect key from config.yaml")
        if key_file:
            console.print(f"[dim]  {escape(key_file)} is still there; delete it yourself if you want it gone[/dim]")


def _verb_set_portal(
    *,
    config_path: Path,
    asc_key_id: str | None,
    asc_issuer_id: str | None,
    asc_key_file: str | None,
    json_mode: bool,
    **_: Any,
) -> None:
    _do_set_portal(
        config_path, key_id=asc_key_id, issuer_id=asc_issuer_id, key_file=asc_key_file,
        emitter=JsonEmitter() if json_mode else None,
    )


def _verb_clear_portal(*, config_path: Path, json_mode: bool, **_: Any) -> None:
    _do_clear_portal(config_path, emitter=JsonEmitter() if json_mode else None)


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
    opt_lc_bundle_id: str | None,
    opt_no_lc_bundle_id: bool | None,
    opt_auto_profile: bool | None,
    add_app_store_country: str | None,
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
        lc_bundle_id=opt_lc_bundle_id,
        clear_lc_bundle_id=bool(opt_no_lc_bundle_id),
        auto_profile=opt_auto_profile,
        store_country=add_app_store_country,
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
    from . import lc_manage

    # A Stop mid-push ends the envelope as stopped, as a build's does.
    with lc_manage.exit_on_termination(), _narration_on_stderr(json_mode):
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


def _verb_remove_data(
    *, config_path: Path, data_items: str | None, dry_run: bool, json_mode: bool, **_: Any
) -> None:
    _do_remove_data(
        config_path, items=data_items, dry_run=dry_run,
        emitter=JsonEmitter() if json_mode else None,
    )


def _do_remove_data(
    config_path: Path, *, items: str | None, dry_run: bool, emitter: JsonEmitter | None
) -> None:
    """`--remove-data`: what Graft keeps outside Graft.app, and, with
    `--items`, removing it (`remove_data`).

    Without `--items` it only lists, `--dry-run` or not: removing anything
    takes naming it. With `--items` and `--dry-run` it says what would go and
    touches nothing. Every item is listed afresh first, so what is removed is
    what is there now, not what an earlier listing saw.
    """
    from . import remove_data

    found = remove_data.survey(config_path)
    if items is None:
        if emitter:
            for item in found.items:
                emitter.emit("data-item", **item.fields())
            emitter.emit(
                "data-items-end",
                count=len(found.items),
                keychain=found.keychain,
                total_bytes=sum(
                    i.size_bytes or 0 for i in found.items if i.removable and i.present
                ),
            )
        else:
            _print_data_items(found)
        return

    chosen = remove_data.choose(found, items)
    outcomes = remove_data.remove(chosen, dry_run=dry_run)
    failed = [o for o in outcomes if not o.ok]
    if emitter:
        for outcome in outcomes:
            emitter.emit("data-removed", **outcome.fields(dry_run=dry_run))
        emitter.emit(
            "data-removal-end",
            ok=not failed,
            dry_run=dry_run,
            removed=[o.id for o in outcomes if o.ok],
            failed=[o.id for o in failed],
            freed_bytes=sum(o.freed_bytes for o in outcomes),
        )
    else:
        _print_data_removal(found, outcomes, dry_run=dry_run)
    if failed:
        raise GraftError(
            f"{len(failed)} of the {len(outcomes)} items couldn't be removed completely",
            hint="what is still there is listed above; run graft --remove-data to see it again",
        )


def _print_data_items(found: Any) -> None:
    """`--remove-data`'s listing in Terminal, by group."""
    from .remove_data import GROUP_TITLES, KEYCHAIN_LOCKED, KEYCHAIN_NO_ANSWER, human_size

    group = None
    for item in found.items:
        if item.present is False:
            continue
        if item.group != group:
            group = item.group
            console.print(f"\n[bold]{escape(GROUP_TITLES[group])}[/bold]")
        if item.present is None:
            size = "couldn't check"
        else:
            # Nothing for a link or keychain items, which have no size to speak of.
            size = human_size(item.size_bytes) if item.size_bytes else ""
        mark = (
            "" if not item.removable
            else ("  [dim](removed with all)[/dim]" if item.default else "  [dim](only by name)[/dim]")
        )
        head = f"  {escape(item.id)}" if item.removable else "  -"
        console.print(f"{head}  {escape(item.title)}  {escape(size)}{mark}")
        console.print(f"      [dim]{escape(item.detail)}[/dim]")
        for path in item.paths:
            console.print(f"      {escape(str(path))}")
        for service, account in item.keychain:
            console.print(f"      keychain: {escape(service)} {escape(account)}")
        if item.note:
            console.print(f"      [yellow]{escape(item.note)}[/yellow]")
        if item.how:
            console.print(f"      [dim]to remove it: {escape(item.how)}[/dim]")
    if found.keychain in (KEYCHAIN_LOCKED, KEYCHAIN_NO_ANSWER):
        console.print(
            "\n[yellow]Your login keychain is locked or didn't answer, so the keychain items "
            "weren't checked. Unlock it in Keychain Access, then run this again.[/yellow]"
        )
    if not any(i.present is not False and i.removable for i in found.items):
        console.print("\nGraft keeps nothing outside Graft.app here.")
        return
    console.print(
        "\nNothing was removed. To remove items, name them (this can't be undone):\n"
        "  graft --remove-data --items all              # every item above marked 'removed with all'\n"
        "  graft --remove-data --items all,passwords    # and the passwords too\n"
        "  graft --remove-data --items cache,temp       # only these\n"
        "Add --dry-run to see what would go first."
    )


def _print_data_removal(found: Any, outcomes: list[Any], *, dry_run: bool) -> None:
    from .remove_data import human_size

    titles = {i.id: i.title for i in found.items}
    for outcome in outcomes:
        title = escape(titles.get(outcome.id, outcome.id))
        if not outcome.removed and outcome.ok:
            console.print(f"[dim]-[/dim] {title}: nothing there")
            continue
        if outcome.ok:
            said = "would be removed" if dry_run else "removed"
        else:
            said = "not all of it was removed"
        if outcome.freed_bytes:
            said += f", {human_size(outcome.freed_bytes)}"
        mark = "[green]✓[/green]" if outcome.ok else "[red]✗[/red]"
        console.print(f"{mark} {title}: {said}")
        for what in outcome.removed:
            console.print(f"    {escape(what)}")
        for what, why in outcome.failed:
            console.print(f"    [red]still there:[/red] {escape(what)}: {escape(why)}")
    total = human_size(sum(o.freed_bytes for o in outcomes))
    if dry_run:
        console.print(f"\n[dim]Dry run: nothing was removed ({total} would be).[/dim]")
    else:
        console.print(
            f"\n{total} removed. To remove Graft itself, quit it and move Graft.app to the Trash."
        )


def _do_decrypt(
    cfg: Config, bundle_id: str, *, device: str | None, backend: str,
    strip_encrypted_extensions: bool = False, strip_extensions_forced: bool = False,
    verbose: bool = False, emitter: JsonEmitter | None = None,
) -> None:
    """Dump a decrypted IPA off the device and stage it for the pipeline.

    Three steps, and the middle one is the point: dump with bagbak, *verify the
    result is actually cryptid 0*, then stage it under the canonical name. A
    dump that silently isn't decrypted would sail through here and be flagged
    only by a warning at stage 2.5 of the next build, long after the cause — so the check
    the build does is done here too, at the moment there is someone to tell.
    """
    if emitter:
        emitter.emit("decrypt-start", bundle_id=bundle_id)

    # No mode to take: the whole app is the only dump that can be staged, so
    # `--decrypt-mode` refuses anything else before this runs
    # (`_require_whole_app_mode`) and `decrypt_runner` fixes `all` itself.
    udid = device or cfg.default_device_udid
    with tempfile.TemporaryDirectory(prefix="graft-decrypt-") as tmp:
        dump_dir = Path(tmp) / "dump"
        if not emitter:
            console.print(f"→ dumping [cyan]{escape(bundle_id)}[/cyan] with {backend}…")
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
            bundle_id, dump_dir, udid=udid, backend=backend, on_line=on_line,
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
        # Staged like a drop or a get, not moved: a dump copies the bundle off
        # the device with its `SC_Info/` licenses in it, and `stage` is what
        # leaves those (and any iTunesMetadata.plist) behind, owner-only.
        provide_ipa_module.stage(result.ipa_path, final)
        # A stripped dump lands under the same canonical name a complete one
        # does, and nothing inside it reads differently — so the fact is
        # recorded beside it, or every later build silently reuses an app
        # missing its extensions while reporting an ordinary cache hit.
        #
        # Cleared as deliberately as it is written. `stage` replaces the
        # IPA but not the note beside it, so a *complete* re-dump of the same
        # version inherited the previous run's sidecar and every later build
        # warned about extensions that were sitting right there — the one
        # warning that exists to tell the truth about a partial app, lying, until
        # someone deleted a file they were never told about.
        if stripped:
            mark_stripped(final, stripped, lost=lost, importance=importance)
        else:
            clear_stripped(final)

    # Staged under the plain name, which a hold by build never uses: every app
    # of this bundle id held by build at this version holds by version now.
    for name, app_cfg in cfg.apps.items():
        if app_cfg.bundle_id == info.bundle_id:
            appstore.let_go_of_the_build(
                cfg, name, info.version, emitter=emitter, warn=lambda message: _warn(emitter, message),
            )

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
                "extensions came back whole on a device under frida-server "
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
    where it hurt: with SSH down the pane labeled every row "will be launched
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
    cfg: Config, bundle_id: str, *, device: str | None,
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
            cfg, bundle_id, udid=udid, backend=backend, strip=strip,
            verbose=verbose, emitter=emitter,
        )


def _decrypt_climbing(
    cfg: Config, bundle_id: str, *, udid: str | None,
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
                cfg, bundle_id, device=udid, backend=current,
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
    a different tool's output is exactly how automatic behavior becomes
    inexplicable behavior.
    """
    if emitter:
        emitter.emit("log", source="graft", line=why)
    else:
        console.print(f"[yellow]→[/yellow] {escape(why)}")


def _require_whole_app_mode(decrypt_mode: str | None) -> None:
    """Refuse every `--decrypt-mode` but `all`, the only dump a decrypt verb can stage.

    Both verbs verify a dump and stage it for a build, and only bagbak's `all`
    dumps an app. `main` writes bare binaries with no `Payload`, so the dump
    failed at reading its Info.plist, naming a temp directory already gone;
    under Automatic a failed bagbak falls back to frida-ios-dump, which has no
    mode and dumps the whole app regardless; and `ext` was never shown to make
    anything a build can use. Asked before the device is, so a refusal costs
    nothing.

    The flag stays, accepting `all`, so scripts that pass it keep working. Its
    value goes no further: with one mode left, `decrypt_runner` fixes `all`
    where the bagbak argv is built, and nothing in between carries a mode.
    """
    if decrypt_mode is None or decrypt_mode == "all":
        return
    raise ConfigError(
        f"--decrypt-mode {decrypt_mode} is not supported: a decrypt is staged for "
        "a build, and only all dumps a whole app"
    )


def _dispatch_decrypt(
    cfg: Config, bundle_id: str, *, device: str | None,
    decrypt_backend: str | None, strip_encrypted_extensions: bool | None,
    verbose: bool, json_mode: bool,
) -> None:
    """The shared tail of both decrypt verbs: pick the backend, then dump.

    `--decrypt` (bundle typed) and `--decrypt-frontmost` (bundle read off the
    foreground app) end identically — default the backend and hand to
    `_do_decrypt`, which verifies cryptid 0 and stages the result. One place, so
    a new `_do_decrypt` argument can't land on one verb and not the other.
    """
    _decrypt_escalating(
        cfg, bundle_id, device=device,
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
    _require_whole_app_mode(decrypt_mode)
    _dispatch_decrypt(
        cfg, decrypt, device=device,
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
    _require_whole_app_mode(decrypt_mode)
    udid = device or cfg.default_device_udid
    bundle = decrypt_runner.frontmost_app(udid)
    if not bundle:
        raise _fatal(decrypt_runner.DecryptError(
            "no app is in the foreground on the device",
            hint="open the app you want to decrypt on the device and leave it on "
            "screen, then retry — this dumps whatever is frontmost",
        ))
    # Pass the *resolved* udid, not the raw `device`: the app we detected as
    # frontmost and the app we dump are then provably the same device, rather
    # than two independent resolutions of `device or default` a future rule
    # change could split.
    _dispatch_decrypt(
        cfg, bundle, device=udid,
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
    # Not `select_device`. When this was written that resolved the udid through
    # `idevice_id` (libimobiledevice), while `list_installed_apps` connects
    # through pymobiledevice3's usbmux — two libraries with two separate views
    # of which Wi-Fi devices are up right now, which differ moment to moment.
    # Resolving through one and connecting through the other made the list fail
    # on a phone the connecting library could see perfectly well. Both are
    # pymobiledevice3 now, but this still has no use for the "is one connected"
    # refusal `select_device` adds, so it goes straight to the device.
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
    if info is not None:
        # For `--update --plan --quick`, which opens no session and so has
        # only the last reading to go on (`developer_mode.status`). A null
        # leaves the last one: "couldn't tell" isn't news.
        devices_seen.remember(
            cfg.state_dir, target, developer_mode=info.developer_mode,
            # What the device is called, for when it isn't connected (spec
            # §5b): Settings and the app pane's bar name it from this.
            name=info.name, model=info.model, ios_version=info.ios_version,
        )
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
            # True or False on iOS 16 and later; null before, or unread.
            developer_mode=info.developer_mode if info is not None else None,
        )
    elif info is not None:
        total = "?" if info.total_bytes is None else str(info.total_bytes)
        free = "?" if info.free_bytes is None else str(info.free_bytes)
        mode = {True: "on", False: "off"}.get(info.developer_mode)
        said = f"; Developer Mode {mode}" if mode else ""
        console.print(f"{info.udid}: {free} free of {total}{said}")


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
        # On stderr under --json, where stdout is the event stream and a usage
        # line is not an event; on stdout otherwise, where it always was.
        if json_mode:
            err_console.print("usage: graft APP [APP ...]   (see --help)")
        else:
            console.print("usage: graft APP [APP ...]   (see --help)")
        return 64

    # Build pipeline for each requested app. Nothing about a build is set up
    # here: `_build_one` opens log.sqlite inside its own envelope, because
    # opening it is a `mkdir` plus an `executescript` in the state directory and
    # a state directory that won't take writes has to be reported as a failed
    # build rather than as a traceback before the first app is attempted.
    from . import lc_manage

    overall_ok = True
    em = JsonEmitter() if json_mode else None
    # SIGTERM (the GUI's Stop) and SIGHUP raise `Stopped` for the whole run
    # (SIGHUP unless it arrived ignored, as under `nohup`),
    # not only while zsign runs: under the default disposition a Stop during
    # cyan or the push killed the process with no `build-end` at all. Each
    # app's envelope reports the stop, and `Stopped` is a `SystemExit`, so it
    # passes the `except Exception` below and no further app is attempted.
    with lc_manage.exit_on_termination(), _narration_on_stderr(json_mode):
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
                # `Exception`, matching what `_build_one` re-raises. Catching
                # only GraftError here meant an OSError during example's
                # archive took bravo and delta with it: neither was attempted,
                # the exit-code summary below never ran, and a --json consumer
                # that asked for three apps got one terminal event and then
                # silence. Every app the user named is attempted; the exit code
                # answers for all of them.
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
    "--store-country": "add_app_store_country",
    "--app": "provide_ipa_app",
    "--preferred-version": "opt_preferred_version",
    "--compression-level": "opt_compression_level",
    "--lc-bundle-id": "opt_lc_bundle_id",
    "--no-lc-bundle-id": "opt_no_lc_bundle_id",
    "--auto-profile": "opt_auto_profile",
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
    "--again": "again",
    "--version-id": "version_id",
    "--allow-unverified-helper": "allow_unverified_helper",
    "--expect-ipatool": "expect_ipatool",
    "--expect-email": "expect_email",
    "--country": "search_country",
    "--limit": "search_limit",
    "--offset": "versions_offset",
    "--instances": "lc_instances",
    "--channel": "lc_channel",
    "--cert-only": "lc_cert_only",
    "--asc-key-id": "asc_key_id",
    "--asc-issuer-id": "asc_issuer_id",
    "--asc-key-file": "asc_key_file",
    "--items": "data_items",
    "--all": "update_all",
    "--plan": "plan_only",
    "--quick": "quick_plan",
    "--with-affected": "with_affected",
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

    @property
    def honours_dry_run(self) -> bool:
        """Whether this verb previews under `--dry-run`: whether its handler
        reads `dry_run`.

        Read off the handler, as `_speaks_json` reads `json_mode`, so the list
        `_DryRunGate` refuses against cannot drift from the code that honours
        the flag: a verb whose handler learns to preview is let through the
        moment it takes the parameter, and one that stops taking it is refused.
        """
        return "dry_run" in inspect.signature(self.run).parameters


def _speaks_json(verb: _Verb | _StrayFlagGate | _DryRunGate | _ModifierGate) -> bool:
    """Whether `verb` answers --json with events: whether its handler reads
    `json_mode`.

    Read off the handler rather than declared beside it, so a verb that grows
    a JSON mode is covered by `main`'s stderr switch the moment it can be
    asked for one. The handlers that do not read it are the interactive ones
    (`--setup-portal`, the password prompts; `--init` reads `json_mode` to
    refuse it without `--account`), the two whose stdout
    is itself the answer (`--version`; `--build-log`, a saved JSON-Lines log
    written straight to `sys.stdout`), `--describe`, which is JSON whatever is
    asked, `--reveal-build`, which prints nothing, and the stray-flag gate,
    which only fails.
    """
    return "json_mode" in inspect.signature(verb.run).parameters


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
#:
#: Filled from `main`'s own declarations (below it), not written out: the
#: hand-kept table had one pair of five, so `--builds --no-auto-profile` was
#: refused as "--auto-profile requires ...".
_NEGATED_FLAGS: dict[str, str] = {}


def _negated_flags(command: Callable[..., Any]) -> dict[str, str]:
    """Every `--x/--no-x` pair `command`'s options declare, positive form first."""
    pairs: dict[str, str] = {}
    for param in inspect.signature(command).parameters.values():
        for decl in getattr(param.default, "param_decls", ()) or ():
            if "/" in decl:
                positive, negative = decl.split("/", 1)
                pairs[positive] = negative
    return pairs


def _flag_as_typed(flag: str, value: Any) -> str:
    """`flag`, or its negative form when that is what set `value`."""
    return _NEGATED_FLAGS[flag] if value is False and flag in _NEGATED_FLAGS else flag


class _StrayFlagGate:
    """Not a verb: the position in the order where verb-only flags are checked.

    It occupies a slot in `_VERBS` because *where* it runs is behavior. Above
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
                "--appstore-check, --appstore-get, --appstore-versions, --appstore-claim, --appstore-login, --appstore-enroll-command, --appstore-search, --update, --attach-tweak, --detach-tweak, --set-app-options, "
                "--delivery-state, --reconcile, --set-settings, --set-portal, --artwork, "
                "--prune-builds, --devices, --device-apps, --decrypt, "
                "--decrypt-frontmost, --delete-staged, --lc-status, --lc-install, --lc-clean, "
                "or --remove-data"
            )
        )
        raise _fatal(ConfigError(f"{', '.join(stray)} {detail}"))


class _DryRunGate:
    """Not a verb: the refusal of `--dry-run` by every verb that ignores it.

    Typer accepts `--dry-run` beside any verb, and only a build and the verbs
    whose handlers take `dry_run` (`_Verb.honours_dry_run`) ever read it.
    Everywhere else it was dropped without a word, which is the worst thing a
    preview flag can do: `graft --lc-clean --dry-run` deleted LiveContainer's
    files for real, on a real device.

    First in `_VERBS`, ahead of the verbs that predate a config, because the
    question is about the verb that will *run* — the first one selected, read
    out of the same order the dispatch uses — and that can be any of them.
    `needs` and `migrates` are what make the refusal come before anything
    else: no config load (which sweeps stale partials out of the cache) and no
    directory migration. Selected only when the invocation is malformed, like
    `_StrayFlagGate`; otherwise the walk passes it.
    """

    flag = "--dry-run"
    needs = _Needs.NOTHING
    migrates = False

    @staticmethod
    def _refusing(opts: dict[str, Any]) -> _Verb | None:
        """The verb that would run and cannot preview, when `--dry-run` was given."""
        if not opts["dry_run"]:
            return None
        verb = next((v for v in _VERBS if isinstance(v, _Verb) and v.selected(opts)), None)
        # No verb is a build, and a build honours it.
        return verb if verb is not None and not verb.honours_dry_run else None

    def selected(self, opts: dict[str, Any]) -> bool:
        return self._refusing(opts) is not None

    def run(self, **opts: Any) -> NoReturn:
        verb = self._refusing(opts)
        assert verb is not None
        previews = [v.flag for v in _VERBS if isinstance(v, _Verb) and v.honours_dry_run]
        raise _fatal(ConfigError(
            f"--dry-run is not valid with {verb.flag}: --dry-run previews only a "
            f"build and {', '.join(previews)}",
            hint="nothing was run",
        ))


#: The global modifiers a verb either reads or must refuse, in the order a
#: refusal names them. Each changes what is done to a device, to the user's
#: files, or how far a download is trusted — so dropping one without a word
#: does something the user did not ask for: `graft example --prune-builds`
#: trashed every app's archives, `--reinstall-build 5 --device B` installed on
#: the phone the build was recorded for, and `--update-tweak X --fetch-sha H`
#: never checked H. Display-only qualifiers (`--refresh`, `--builds-app`,
#: `--verbose`) only change what is printed, and stay inert.
_MODIFIERS: dict[str, str] = {
    "apps": "app names",
    "device": "--device",
    "skip_install": "--skip-install",
    "for_playcover": "--for-playcover",
    "skip_decrypt_check": "--skip-decrypt-check",
    "fetch_sha": "--fetch-sha",
    # A tweak update the person confirmed although its source isn't
    # verified: dropped by another verb, it would read as a trust given.
    "trust_tweak": "--trust-tweak",
    # Which Canister repository a fetch takes: dropped, the fetch would take
    # the newest from any of them.
    "fetch_repository": "--fetch-repository",
    # `--init`'s, beside anything else: a setup flag a verb drops would read
    # as a setup that ran.
    "init_account": "--account",
    "init_p12": "--p12",
    "init_profile": "--profile",
    "init_replace": "--replace",
    # `--check-update`'s: a version compared against nothing would read as a
    # check that ran.
    "current_version": "--current-version",
    # The self-update's: which app is replaced, after which process, and
    # whether it opens again. Dropped by another verb, each would read as
    # an update that was arranged.
    "app_path": "--app-path",
    "wait_pid": "--wait-pid",
    "relaunch": "--relaunch",
    # `--export-ipa`'s: dropped by a build, it would read as a file that was
    # allowed to be replaced and wasn't.
    "export_overwrite": "--overwrite",
}

#: Why a verb ignores a modifier, where there is more to say than "it does".
_MODIFIER_HINTS: dict[tuple[str, str], str] = {
    ("--reinstall-build", "device"): (
        "a re-install ships to the device its build was recorded for"
    ),
    ("--prune-builds", "apps"): (
        "it cleans up every app's archives; there is no per-app clean-up"
    ),
    ("--export-ipa", "device"): "an export installs nothing, so it uses no device",
    ("--export-ipa", "skip_install"): "an export installs nothing anyway",
    ("--export-ipa", "for_playcover"): (
        "an export is the build for an iPhone or iPad, unsigned; Build for this Mac is a build of its own"
    ),
}


class _ModifierGate:
    """Not a verb: the refusal of a global modifier by a verb that ignores it.

    `_DryRunGate`'s rule for the other modifiers in `_MODIFIERS`. Typer
    accepts each beside any verb, and only the handlers that take the
    parameter read it, so everywhere else it was dropped. Which verb reads
    which is read off the handler, as `_Verb.honours_dry_run` is, so the two
    cannot drift; with no verb selected, the build is what runs.

    Second in `_VERBS`, for `_DryRunGate`'s reason: the question is about the
    verb that will run, and the refusal must land before a config load or a
    migration touches anything.
    """

    flag = "(modifiers)"
    needs = _Needs.NOTHING
    migrates = False

    @staticmethod
    def _refused(opts: dict[str, Any]) -> tuple[str, list[str]] | None:
        """The verb that will run and the modifiers it would ignore, or None."""
        verb = next((v for v in _VERBS if isinstance(v, _Verb) and v.selected(opts)), None)
        reads = inspect.signature(verb.run if verb else _verb_build_apps).parameters
        ignored = [p for p in _MODIFIERS if p not in reads and _asked_for(opts[p] or None)]
        if not ignored:
            return None
        return (verb.flag if verb else "a build"), ignored

    def selected(self, opts: dict[str, Any]) -> bool:
        return self._refused(opts) is not None

    def run(self, **opts: Any) -> NoReturn:
        refused = self._refused(opts)
        assert refused is not None
        verb, ignored = refused
        named = [
            f"{_MODIFIERS[p]} ({', '.join(opts[p])})" if p == "apps" else _MODIFIERS[p]
            for p in ignored
        ]
        why = [_MODIFIER_HINTS[(verb, p)] for p in ignored if (verb, p) in _MODIFIER_HINTS]
        raise _fatal(ConfigError(
            f"{', '.join(named)} {'is' if len(named) == 1 else 'are'} not valid with "
            f"{verb}: it would ignore {'it' if len(named) == 1 else 'them'}",
            hint="; ".join([*why, "nothing was run"]),
        ))


# The whole of `main`'s control flow, in source order, which is the order it
# runs in. First match wins — given two verb flags today's behavior is that the
# earlier one here takes it, and that is preserved rather than made an error,
# because the GUI builds argv programmatically and an error would be a new way
# for it to fail.
#
# `_DryRunGate` comes first because it judges whichever verb is about to run,
# and refuses before that verb, a config load or a migration touches anything;
# `_ModifierGate`, beside it, does the same for the other global modifiers.
#
# `_Needs.NOTHING` marks the five verbs that must work on a machine that has
# never run `--init`: the GUI's contract test introspects `--describe` before
# there is a config.yaml to load, the app checks for a newer Graft before setup
# is done, and `--init` is what creates one.
_VERBS: tuple[_Verb | _StrayFlagGate | _DryRunGate | _ModifierGate, ...] = (
    _DryRunGate(),
    _ModifierGate(),
    _Verb("--describe", "describe", _verb_describe, needs=_Needs.NOTHING, migrates=False),
    _Verb("--version", "version", _verb_version, needs=_Needs.NOTHING, migrates=False),
    # About the program, not the user's files, like the two above: no config
    # (the app checks before setup is done) and no migration. Above the
    # stray-flag gate, whose config load it must not need, so its
    # `--current-version` is a modifier `_ModifierGate` refuses elsewhere,
    # as `--init`'s `--account` is.
    _Verb("--check-update", "check_update", _verb_check_update, needs=_Needs.NOTHING, migrates=False),
    # The self-update, beside the check for the same reasons: about the
    # program, not the user's files (what they write is Graft's own cache
    # folder and the old app's move to the Trash), and run before setup is
    # done as readily as after.
    _Verb("--download-update", "download_update", _verb_download_update, needs=_Needs.NOTHING, migrates=False),
    _Verb("--install-update", "install_update", _verb_install_update, needs=_Needs.NOTHING, migrates=False),
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
            "--name", "--bundle-id", "--display-name", "--delivery", "--store-country",
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
        accepts=frozenset({"--app", "--allow-unverified", "--again", "--version-id"}),
    ),
    _Verb(
        "--appstore-versions", "appstore_versions", _verb_appstore_versions,
        accepts=frozenset({"--app", "--limit", "--offset"}),
    ),
    _Verb("--appstore-claim", "appstore_claim", _verb_appstore_claim, accepts=frozenset({"--app", "--expect-email"})),
    _Verb("--appstore-login", "appstore_login", _verb_appstore_login, accepts=frozenset({"--expect-ipatool"})),
    _Verb("--appstore-logout", "appstore_logout", _verb_appstore_logout),
    _Verb("--appstore-enroll-candidates", "appstore_enroll_candidates", _verb_appstore_enroll_candidates),
    _Verb(
        "--appstore-enroll-command", "appstore_enroll_command", _verb_appstore_enroll_command,
        accepts=frozenset({"--app", "--allow-unverified-helper"}),
    ),
    _Verb("--appstore-enroll-finish", "appstore_enroll_finish", _verb_appstore_enroll_finish),
    _Verb(
        "--appstore-search", "appstore_search_term", _verb_appstore_search,
        accepts=frozenset({"--country", "--limit"}),
    ),
    # Update (`update_run`): an app's newer App Store version, its
    # tweaks' newer builds, then the build and install. Below the gate, which
    # checks its four flags; it takes the app names a build takes, and no
    # `--device`: each app goes to every device it was delivered to (spec
    # §11.3, `cli._delivery_targets`).
    _Verb(
        "--update", "update_flag", _verb_update,
        accepts=frozenset({"--all", "--plan", "--quick", "--with-affected"}),
    ),
    # A device an app is on that Update should stop waiting for (sold,
    # retired, reset). `--device` is global, as for `--device-info`: read off
    # the handler by `_ModifierGate`, so it's refused beside every verb that
    # doesn't take it, and app names beside this one.
    _Verb("--drop-update-target", "drop_update_target", _verb_drop_update_target),
    _Verb("--staged-ipas", "staged_ipas", _verb_staged_ipas),
    _Verb("--delete-staged", "delete_staged", _verb_delete_staged,
          accepts=frozenset({"--force"})),
    _Verb("--install-to-mac", "install_to_mac", _verb_install_to_mac),
    # A build up to cyan, then the IPA is written where it was told instead of
    # signed or delivered. A verb rather than a build modifier, so that
    # `_ModifierGate` refuses what an export would ignore (`--device`,
    # `--skip-install`, `--for-playcover`) off its handler, with nothing to
    # keep in step; it reads the app names a build takes. Below the gate, so
    # a verb-only flag beside it (`--app`) is refused rather than dropped.
    _Verb("--export-ipa", "export_ipa", _verb_export_ipa),
    _Verb("--attach-tweak", "attach_tweak", _verb_attach_tweak, accepts=frozenset({"--app"})),
    _Verb("--detach-tweak", "detach_tweak", _verb_detach_tweak, accepts=frozenset({"--app"})),
    _Verb("--delete-tweak", "delete_tweak", _verb_delete_tweak),
    _Verb(
        "--set-app-options", "set_app_options_flag", _verb_set_app_options,
        # Deliberately NOT --name/--bundle-id: _do_set_app_options
        # has no way to rename an app or change its identity, so accepting them
        # would report success for a change that never happened.
        accepts=frozenset({
            "--app", "--display-name", "--delivery", "--preferred-version", "--store-country",
            "--compression-level", "--keep-bundle-id", "--strip-extensions",
            "--strip-supported-devices", "--lc-bundle-id", "--no-lc-bundle-id",
            "--auto-profile",
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
    # The App Store Connect key, set and removed without the wizard's prompts
    # (the Settings pane's "App Store Connect" section). `_Needs.PATH` for the
    # settings verbs' reason, and one more of its own: `load_config` refuses
    # an `app_store_connect:` section missing a key, and that is a section
    # these two exist to replace or remove. Both still need the file to exist.
    #
    # Ahead of `--settings`, for `--set-settings`' reason: both writers echo
    # the settings after they write, so given a writer and the reader, the
    # writer is strictly more. Below it, `--settings --set-portal ...` reported
    # and exited 0 having checked and written nothing.
    _Verb(
        "--set-portal", "set_portal_flag", _verb_set_portal, needs=_Needs.PATH,
        accepts=frozenset({"--asc-key-id", "--asc-issuer-id", "--asc-key-file"}),
    ),
    _Verb("--clear-portal", "clear_portal_flag", _verb_clear_portal, needs=_Needs.PATH),
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
    # What Graft keeps outside Graft.app, and removing it. `_Needs.NOTHING`:
    # it must list and remove on a machine whose setup never finished or
    # whose config.yaml won't load, and reads that file itself, leniently.
    # No migration, which would also make the four folders it is about to
    # measure (`secure_app_dirs`). Below the gate, which checks `--items`.
    _Verb(
        "--remove-data", "remove_data", _verb_remove_data, needs=_Needs.NOTHING,
        accepts=frozenset({"--items"}), migrates=False,
    ),
)


@app.callback()
@_reported
def main(
    ctx: typer.Context,
    apps: list[str] = typer.Argument(None, help="App name(s) from config to build & install."),
    dry_run: bool = typer.Option(False, "--dry-run", help="With a build, --export-ipa, --prune-builds, --init --account or --remove-data --items: show what would happen without doing it. Every other verb refuses it."),
    skip_install: bool = typer.Option(False, "--skip-install", help="Build only (and sign, for a sideload app): don't install or push it to the device."),
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
    update_flag: bool = typer.Option(False, "--update", help="Bring the named apps (or --all) up to date: the App Store's newer version, newer builds of their tweaks, then, one app at a time, one build, installed on every device the app was installed on. Never buys or claims an app, and never lets go of a hold. With --plan, say what would change and change nothing. Run it again to resume after a failure or a Stop."),
    # `bool | None` defaulting to None: verb-only flags, passed when not None
    # (see `force`).
    update_all: bool | None = typer.Option(None, "--all", help="With --update: every app, in the order Graft.app lists them."),
    plan_only: bool | None = typer.Option(None, "--plan", help="With --update: work out what would change, and change nothing. Reads no keychain and runs no ipatool."),
    quick_plan: bool | None = typer.Option(None, "--quick", help="With --update --plan: the background form. Asks usbmux which devices are connected and nothing more, so readiness is only likely. Graft.app runs it once a day."),
    with_affected: bool | None = typer.Option(None, "--with-affected", help="With --update: also update and install the other apps that use a tweak the update changes."),
    trust_tweak: list[str] | None = typer.Option(None, "--trust-tweak", metavar="FILE@KEY", help="With --update or --update-tweak: update a library tweak although its update isn't verified (its source isn't confirmed, isn't HTTPS, or publishes no checksum), because you trust where it comes from. Takes the tweak's confirm_key, from --update --plan --json or --tweak-updates --json, which is for exactly that update: one that changed since (another version, build or source) is refused. Repeat it for each tweak."),
    drop_update_target: str | None = typer.Option(None, "--drop-update-target", metavar="APP", help="With --device UDID: stop updating APP on that device, one it was installed on, until it's installed there again. For a device that's gone (sold, retired or reset). Changes nothing on any device."),
    check: bool = typer.Option(False, "--check", help="Run pre-flight checks and exit."),
    init: bool = typer.Option(False, "--init", help="First-run setup. Asks its questions in Terminal, or none with --account."),
    init_account: str | None = typer.Option(None, "--account", help="With --init: 'free' or 'paid', and no questions. Paid needs --p12 and --profile, and reads the .p12 password from stdin (one line). --dry-run checks everything and writes nothing."),
    init_p12: str | None = typer.Option(None, "--p12", help="With --init --account paid: the .p12 exported from Keychain Access. It is copied into the config folder."),
    init_profile: str | None = typer.Option(None, "--profile", help="With --init --account paid: the development .mobileprovision. It is copied into the config folder."),
    init_replace: bool = typer.Option(False, "--replace", help="With --init --account paid on a paid config: swap in the new .p12 and profile, its Keychain password and the signing: section, keeping apps, repos and settings. A development certificate and profile expire every year."),
    setup_portal: bool = typer.Option(False, "--setup-portal", help="Configure App Store Connect API key for auto-provisioning."),
    set_portal_flag: bool = typer.Option(False, "--set-portal", help="Set the App Store Connect API key without prompts: --asc-key-id, --asc-issuer-id and --asc-key-file. Checks it by listing devices first, and writes nothing if that fails."),
    clear_portal_flag: bool = typer.Option(False, "--clear-portal", help="Remove the App Store Connect API key from config.yaml. The .p8 file is kept."),
    # `str | None` defaulting to None: verb-only flags, detected as passed by
    # `is not None` — see `force` below.
    asc_key_id: str | None = typer.Option(None, "--asc-key-id", help="With --set-portal: the key's Key ID (10 characters)."),
    asc_issuer_id: str | None = typer.Option(None, "--asc-issuer-id", help="With --set-portal: the Issuer ID shown above the keys list."),
    asc_key_file: str | None = typer.Option(None, "--asc-key-file", help="With --set-portal: the AuthKey_<key id>.p8 downloaded from App Store Connect. It is copied beside config.yaml."),
    search: str | None = typer.Option(None, "--search", help="Search canister.me for tweaks matching QUERY."),
    fetch: str | None = typer.Option(None, "--fetch", help="Download a .deb into tweaks_dir (package id or direct URL)."),
    fetch_sha: str | None = typer.Option(None, "--fetch-sha", help="Expected SHA-256 for a --fetch URL download (verified after download)."),
    fetch_repository: str | None = typer.Option(None, "--fetch-repository", metavar="ID", help="With --fetch <package id>: only from this Canister repository (a --search result's repository_id), so another repository's higher version can't win. Its updates then come only from there."),
    add_repo_url: str | None = typer.Option(None, "--add-repo", help="Add an APT repo by URL and cache its package index."),
    remove_repo_url: str | None = typer.Option(None, "--remove-repo", help="Remove a previously added APT repo by URL."),
    add_app_url: str | None = typer.Option(None, "--add-app", help="Add an app to config.yaml from its App Store URL or numeric ID."),
    add_app_manual: bool = typer.Option(False, "--add-app-manual", help="Add an app by explicit fields (no iTunes lookup)."),
    add_app_name: str | None = typer.Option(None, "--name", help="With --add-app-manual: the YAML key / shortname (e.g. 'example')."),
    add_app_bundle_id: str | None = typer.Option(None, "--bundle-id", help="With --add-app-manual: the iOS bundle identifier (e.g. com.example.app)."),
    add_app_display_name: str | None = typer.Option(None, "--display-name", help="With --add-app-manual: display name shown in the launcher."),
    add_app_delivery: str | None = typer.Option(None, "--delivery", help="With --add-app or --add-app-manual: 'sideload' or 'livecontainer' (defaults to sideload with a paid account, livecontainer with a free Apple ID; sideload needs a paid account). With --set-app-options: change the app's delivery mode."),
    add_app_store_country: str | None = typer.Option(None, "--store-country", help="With --add-app-manual or --set-app-options: the App Store the app was found in, as a two-letter code such as GB. --appstore-check and --artwork then look it up there (default: the store of this Mac's region, else the US)."),
    provide_ipa_path: Path | None = typer.Option(None, "--provide-ipa", help="Validate a .ipa and stage it in cache_dir under the canonical filename. Requires --app."),
    provide_ipa_app: str | None = typer.Option(None, "--app", help="With --provide-ipa / --attach-tweak / --detach-tweak / --appstore-get / --appstore-versions / --appstore-claim: the configured app name (YAML key). With --appstore-enroll-command: the installed app's path."),
    cached_ipa_app: str | None = typer.Option(None, "--cached-ipa", help="Report which IPA the build pipeline would use for the given app (YAML key)."),
    # --- App Store acquisition (`appstore.py`) ---
    appstore_status: bool = typer.Option(False, "--appstore-status", help="Report whether ipatool is installed and signed in and UnfairPlay is enrolled, with the Terminal commands for whatever is missing."),
    appstore_check: bool = typer.Option(False, "--appstore-check", help="Compare each app's App Store version with its cached IPA. Narrow it with --app."),
    appstore_get: bool = typer.Option(False, "--appstore-get", help="Download the App Store's latest version of --app with ipatool, decrypt it with UnfairPlay, and stage it. Downloads nothing when that version is already staged (see --again)."),
    appstore_claim: bool = typer.Option(False, "--appstore-claim", help="Get --app's free App Store license for the signed-in Apple ID (ipatool purchase). Refuses unless the App Store lists it at 0."),
    # `bool | None` defaulting to None, like `--force`: a verb-only flag, and
    # the stray-flag gate reads "was it passed" as `opts[param] is not None`.
    allow_unverified: bool | None = typer.Option(None, "--allow-unverified", help="With --appstore-get or --provide-ipa: stage a decrypt UnfairPlay could not verify against Apple's page hashes (older apps), with a warning."),
    # `bool | None` defaulting to None, like `--allow-unverified`: a verb-only flag.
    again: bool | None = typer.Option(None, "--again", help="With --appstore-get: download, decrypt and stage the App Store's current version (or the --version-id one) even when that version is already staged. Without it, a get of a version already staged downloads nothing and says so."),
    # `str | None` defaulting to None: a verb-only flag, passed when not None.
    version_id: str | None = typer.Option(None, "--version-id", help="With --appstore-get: get this version instead of the latest, by the id --appstore-versions lists, and hold the app at it: its builds use that version, and --appstore-check doesn't call a newer one an update, until a get of the latest. Only versions the App Store still serves this Apple ID."),
    appstore_versions: bool = typer.Option(False, "--appstore-versions", help="List the versions the App Store serves this Apple ID for --app, newest first, with each one's id for --appstore-get --version-id. Reads the version number and date of --limit of them from --offset on (one request each, remembered); the rest are listed by id."),
    appstore_login: bool = typer.Option(False, "--appstore-login", help="Sign ipatool in to the App Store. stdin gives the Apple ID, then the password, then the 2FA code if Apple asks (asked for in a terminal, the password without echo). None of them is ever an argument, and Graft saves none of them."),
    # `str | None` defaulting to None: a verb-only flag, passed when not None.
    expect_ipatool: str | None = typer.Option(None, "--expect-ipatool", help="With --appstore-login: sign in only if the ipatool found is PATH (compared as real paths). Any other is refused before stdin is read or ipatool starts: appstore-login {state: failed, reason: ipatool-changed}. Graft.app passes the one its sign-in sheet named."),
    # `str | None` defaulting to None: a verb-only flag, passed when not None.
    expect_email: str | None = typer.Option(None, "--expect-email", help="With --appstore-claim: claim only if ipatool is signed in as this Apple ID (compared ignoring case). Another is refused before anything is bought: appstore-refused {reason: account-changed, email}. Graft.app passes the Apple ID its Get (free) confirmation named."),
    appstore_logout: bool = typer.Option(False, "--appstore-logout", help="Sign ipatool out of the App Store (ipatool auth revoke)."),
    appstore_enroll_candidates: bool = typer.Option(False, "--appstore-enroll-candidates", help="List the installed iPhone and iPad apps that can enroll this Mac for App Store decryption."),
    appstore_enroll_command: bool = typer.Option(False, "--appstore-enroll-command", help="Print the script that enrolls this Mac with --app PATH (one of --appstore-enroll-candidates). It runs as root, in a folder of its own: Graft.app runs it behind the macOS administrator prompt; in Terminal, run it with sudo /bin/sh, then --appstore-enroll-finish with the folder it prints."),
    # `bool | None` defaulting to None, like `--allow-unverified`: a verb-only flag.
    allow_unverified_helper: bool | None = typer.Option(None, "--allow-unverified-helper", help="With --appstore-enroll-command: print the script even though root can't check the UnfairPlay it runs, because this copy of Graft has no developer-team signature, or this graft isn't Graft.app's own. Graft.app asks first."),
    appstore_enroll_finish: str | None = typer.Option(None, "--appstore-enroll-finish", help="After the --appstore-enroll-command script: move the model from the folder it printed into this user's model folder, as this user."),
    appstore_search_term: str | None = typer.Option(None, "--appstore-search", help="Search the App Store for iPhone apps matching TERM (no Apple ID needed). Narrow it with --country and --limit."),
    # `str | None` / `int | None` defaulting to None: verb-only flags, detected
    # as passed by `is not None`, like `--force` below.
    search_country: str | None = typer.Option(None, "--country", help="With --appstore-search: which country's App Store, as a two-letter code such as US or GB (default: this Mac's region, else US)."),
    search_limit: int | None = typer.Option(None, "--limit", help="With --appstore-search: how many results, at most 50 (default 25). With --appstore-versions: how many versions to read the details of, at most 50 (default 10)."),
    versions_offset: int | None = typer.Option(None, "--offset", help="With --appstore-versions: start reading details this many versions in from the newest (default 0)."),
    staged_ipas: bool = typer.Option(False, "--staged-ipas", help="Report every IPA staged in the cache, with what any strip removed."),
    delete_staged: str | None = typer.Option(None, "--delete-staged", help="Delete one staged IPA from the cache, by filename."),
    # `bool | None` defaulting to None, like `--brief` and `--with-icons`
    # below: this is a verb-only flag, and the stray-flag gate detects "was it
    # passed" as `opts[param] is not None`. A default of `False` would read as
    # passed on every invocation.
    force: bool | None = typer.Option(None, "--force", help="With --delete-staged: delete even if it is the only staged IPA an app can build from. With --lc-install: replace LiveContainers AltStore or SideStore manages."),
    install_to_mac: str | None = typer.Option(None, "--install-to-mac", help="Hand one staged IPA or archived build to PlayCover to install on this Mac, by filename."),
    export_ipa: str | None = typer.Option(None, "--export-ipa", help="Build the app named beside it as far as injecting its tweaks, then write that IPA, unsigned, to PATH: for an on-device signer or TrollStore, on your own devices. Nothing is signed or sent to a device, whatever the app's delivery mode, and nothing is added to the build history. Refuses a file already at PATH without --overwrite."),
    # `bool | None` defaulting to None, like `--relaunch`: a modifier
    # `_ModifierGate` refuses beside anything but `--export-ipa`.
    export_overwrite: bool | None = typer.Option(None, "--overwrite", help="With --export-ipa: replace a file already at PATH."),
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
    # Two flags rather than `--lc-bundle-id/--no-lc-bundle-id`: that form is a
    # boolean in Typer, and the pin is a bundle id. Both default to None for
    # the stray-flag gate, like `--force`.
    opt_lc_bundle_id: str | None = typer.Option(None, "--lc-bundle-id", help="With --set-app-options: pin the app to one LiveContainer, by bundle id (any case; `graft --lc-status` lists them)."),
    opt_no_lc_bundle_id: bool | None = typer.Option(None, "--no-lc-bundle-id", help="With --set-app-options: clear the app's LiveContainer pin."),
    opt_auto_profile: bool | None = typer.Option(None, "--auto-profile/--no-auto-profile", help="With --set-app-options: make the app's provisioning profile through App Store Connect on its first build (needs a paid account and an App Store Connect key)."),
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
            "device, cache it, and report it as icon_path. The first run is one "
            "extra pass over the device, roughly doubling what the listing "
            "takes (measured: +4s for 67 apps over Wi-Fi); later runs ask only "
            "about ids the cache has no answer for (+0.5s on the same device). "
            "An app SpringBoard has no icon for simply has none."
        ),
    ),
    device_info_flag: bool = typer.Option(False, "--device-info", help="Report one device's storage and battery. Narrow it with --device."),
    lc_status: bool = typer.Option(False, "--lc-status", help="Report the LiveContainers on a device (--device, else every connected one), who manages them, and the latest upstream version."),
    lc_install: bool = typer.Option(False, "--lc-install", help="Install or update LiveContainer on --device, signed so guest apps keep their logins. Refuses an AltStore-managed device without --force."),
    lc_push_cert: bool = typer.Option(False, "--lc-push-cert", help="Copy the signing .p12 into LiveContainer's Documents, for its Settings -> Import Certificate."),
    lc_clean: bool = typer.Option(False, "--lc-clean", help="Remove from LiveContainer's Documents the IPAs and certificate graft recorded pushing to this device, and nothing else."),
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
    decrypt_mode: str | None = typer.Option(None, "--decrypt-mode", help="bagbak dump mode. Only all (the default: the whole app bundle) is accepted, since --decrypt and --decrypt-frontmost stage the dump for a build and no other mode dumps an app."),
    decrypt_backend: str | None = typer.Option(None, "--decrypt-backend", help="Decrypt backend: frida-ios-dump (reliable, needs the app running + device SSH) or bagbak (spawns the app; bagbak 4.x also pulls over device SSH, so it needs the same SSH password)."),
    decrypt_frontmost: bool = typer.Option(False, "--decrypt-frontmost", help="Dump a decrypted IPA of whatever app is currently on screen on the device — auto-detects the foreground app, no bundle id needed. Open the app and keep it foreground first."),
    strip_encrypted_extensions: bool | None = typer.Option(None, "--strip-encrypted-extensions/--keep-encrypted-extensions", help="A dump drops app extensions it cannot decrypt (no tool can read one built for a newer iOS than the device runs) and stages the rest — the app keeps working, minus its widgets and share sheet, and what went is always reported. `--keep-encrypted-extensions` refuses that trade and fails instead. The app's own binaries are never removed either way."),
    set_p12_password: bool = typer.Option(False, "--set-p12-password", help="Prompt for the .p12 password and store it in the macOS Keychain."),
    set_ssh_password: bool = typer.Option(False, "--set-ssh-password", help="Prompt for the device's root SSH password and store it in the macOS Keychain (needed by the frida-ios-dump decrypt backend, and by bagbak 4.x, which also pulls over SSH)."),
    decrypt_check: bool = typer.Option(False, "--decrypt-check", help="Report whether the device can decrypt an app right now, and what is blocking it."),
    version: bool = typer.Option(False, "--version", help="Print version and exit."),
    check_update: bool = typer.Option(False, "--check-update", help="Ask GitHub whether a newer Graft is published: one request, with a generic User-Agent and nothing that identifies you or this Mac. Downloads nothing. Graft.app runs it once a day while open, unless Settings › Updates turns that off."),
    remove_data: bool = typer.Option(False, "--remove-data", help="List everything Graft keeps outside Graft.app, with sizes: its folders, the App Store sign-in, the decryption model, its passwords in the keychain, the graft link, the update log. Removes nothing unless --items names what to remove."),
    # `str | None` defaulting to None: a verb-only flag, passed when not None.
    data_items: str | None = typer.Option(None, "--items", help="With --remove-data: what to remove, as a comma list of the ids it lists, or all (every item marked 'removed with all'; the .p12 and SSH passwords only by name: all,passwords). This can't be undone. With --dry-run, says what would go and removes nothing."),
    current_version: str | None = typer.Option(None, "--current-version", help="With --check-update or --download-update: the version to compare against (default: this CLI's own). Graft.app passes its own."),
    download_update: bool = typer.Option(False, "--download-update", help="When the latest release is newer, download its zip of this build (Graft.zip, or Graft-Intel.zip for the Intel build) into Graft's cache and check it: GitHub's SHA-256, the build, the signature, Graft's team and bundle id, and Apple's notarization. Deletes it if any check fails. Installs nothing (see --install-update)."),
    # `str | None` / `int | None` / `bool | None` defaulting to None, like
    # `--current-version`: modifiers `_ModifierGate` refuses beside any verb
    # whose handler doesn't read them.
    install_update: str | None = typer.Option(None, "--install-update", help="Start the helper that installs PATH, a Graft.app --download-update checked, over --app-path once the process --wait-pid names has quit. The old app goes to the Trash. Graft.app runs it as it quits."),
    app_path: str | None = typer.Option(None, "--app-path", help="With --download-update or --install-update: the Graft.app to replace (default: the one this CLI runs from)."),
    wait_pid: int | None = typer.Option(None, "--wait-pid", help="With --install-update: the running Graft's process id. Nothing is replaced until it has quit."),
    relaunch: bool | None = typer.Option(None, "--relaunch", help="With --install-update: open Graft again once it is installed (or once the old one is put back)."),
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
            "--install-to-mac. Installing it on an iPhone or iPad would point the tweak "
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
            _warn(JsonEmitter() if json_mode else None, notice, startup=True)
        # After the move, so it is the directories in use that are made (or
        # kept) owner-only — before anything below writes into them.
        secure_app_dirs()
        # Signing identity copies a SIGKILL left in the temp directory (R9).
        from .p12 import sweep_stale_copies

        sweep_stale_copies()
    # Resolved once, here, rather than by every `_Needs.PATH` verb for itself —
    # both were spelling `config_path or default_config_path()` in their own
    # bodies, and a third would have had to remember to.
    opts["config_path"] = config_path = config_path or default_config_path()
    cfg: Config | None = None
    if selected is None or selected.needs is _Needs.LOADED:
        cfg = load_config(config_path)
        # Once per command, ahead of the verb, like the notices above: what
        # config.yaml got wrong but loaded anyway (`Config.notices`).
        for notice in cfg.notices:
            _warn(JsonEmitter() if json_mode else None, notice, startup=True)
        # Hidden partials a killed run left behind (R3): two listings.
        sweep_stale_partials(cfg.cache_dir, cfg.built_dir)
    if selected is not None:
        # Under --json, narration goes to stderr for every verb that answers in
        # events (`_narration_on_stderr`): here, once, rather than in each
        # handler, so a helper's `console.print` cannot reach the stream
        # through a verb that forgot to ask.
        with _trust_prompts_as_events(json_mode), \
                _narration_on_stderr(json_mode and _speaks_json(selected)):
            code = selected.run(cfg=cfg, **opts)
        raise typer.Exit(code=0 if code is None else code)

    # No verb flag. `cfg` is loaded — the walk above passed every verb that
    # needs one — and a build is what this CLI does by default.
    with _trust_prompts_as_events(json_mode):
        code = _verb_build_apps(cfg=cfg, **opts)
    raise typer.Exit(code=code)


@contextlib.contextmanager
def _trust_prompts_as_events(json_mode: bool):
    """Under --json, the 'tap Trust' prompt is also a live `log` event.

    `device_session.say_trust_prompt` writes it to stderr, which the app reads
    only after a non-zero exit: a phone that never trusted this Mac sat behind
    a silent 60 s wait, its instruction appearing once the wait had failed.
    Source "device", which the build log and the LiveContainer pane both show.
    The emitter is made here, outside `_narration_on_stderr`, so it writes to
    the real stdout.
    """
    if not json_mode:
        yield
        return
    emitter = JsonEmitter()
    with device_session.trust_prompt_listener(
        lambda line: emitter.emit("log", source="device", line=line)
    ):
        yield


_NEGATED_FLAGS.update(_negated_flags(main))


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
    exc = presented(exc)
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
    origin: str = "build",
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

    `origin` is `update` for `graft --update`'s installs after the first
    (`_do_update_install`): the same archive, so the same digest, on another
    device. It reads like a build's (spec §11.1 only offers a `reinstall`).
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
            origin=origin,
            held_version_id=inputs.held_version_id,
        )
    except Exception as exc:
        _warn(emitter, f"couldn't record delivery: {exc}")


#: Why an export failed, as its `build-end`'s `reason` says it, for an agent
#: that has to tell the cases apart: "exists" shares exit 2 with every
#: config error, and AGENTS.md says to ask before `--overwrite`. Null on
#: success, on a Stop, and on a failure that isn't the export's own (a
#: missing cyan, cyan failing), which the exit status and `error` describe.
#: The README's CLI section and AGENTS.md list them.
EXPORT_FAILURE_REASONS: dict[str, str] = {
    "exists": "a file is already at PATH, and --overwrite wasn't given",
    "bad-destination": "PATH is a folder, in a folder that isn't there, in one of Graft's "
    "own, or starts with ~ for a user this Mac doesn't have",
    "unwritable": "graft isn't let write in PATH's folder: its permissions, macOS's "
    "privacy controls, or a read-only disk",
    "no-space": "the disk PATH is on is full",
    "write-failed": "writing the file failed another way",
    "no-ipa": "no IPA is staged for the app to export",
}


def _export_failure(error: GraftError, reason: str) -> GraftError:
    """`error`, marked with the `reason` its export's `build-end` gives."""
    assert reason in EXPORT_FAILURE_REASONS, reason
    error.export_reason = reason  # type: ignore[attr-defined]
    return error


def _export_reason(exc: BaseException | None) -> str | None:
    """The `reason` an export's `build-end` gives for `exc`, or None."""
    from .errors import IPANotFoundError

    if isinstance(exc, IPANotFoundError):
        return "no-ipa"
    return getattr(exc, "export_reason", None)


def _inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or inside it, by what the file system says
    they are rather than how they are spelled.

    Each of `path`'s ancestors, as it resolves, is compared with `folder` by
    `(st_dev, st_ino)`. macOS's disks are case-insensitive and don't tell a
    precomposed é from a decomposed one unless formatted to, so
    `…/graft/IPAS` names `cache_dir` too, and a comparison of the strings let
    an export write into it (EX2). `path` must exist; a `folder` that doesn't,
    or can't be looked at, holds nothing.
    """
    try:
        held = os.stat(folder)
    except OSError:
        return False
    wanted = (held.st_dev, held.st_ino)
    here = Path(os.path.realpath(path))
    for each in (here, *here.parents):
        seen = os.stat(each)
        if (seen.st_dev, seen.st_ino) == wanted:
            return True
    return False


def _check_export_destination(
    cfg: Config, dest: Path, *, overwrite: bool, dry_run: bool = False
) -> Path:
    """Where `--export-ipa` will write, made absolute, or the reason it won't.

    Run first in the envelope, before minutes of downloading and injecting
    that a typo would waste. The refusal of a file already there is checked
    again at the moment of the move (`_write_export`), which is the check
    that holds: this one is so the answer comes now rather than at the end.

    Graft's own folders for staged IPAs and archived builds are refused. A
    tweaked IPA in the cache could be taken for a source to build from, and
    the retention sweep moves files in `built_dir` to the Trash; both also
    sweep stale temp files like the one this writes.

    Last, a file is made in the folder and removed, so a folder graft can't
    write to fails now rather than after the build: one whose permissions say
    no, a read-only disk, or one macOS's privacy controls keep graft out of,
    which only an attempt to write finds out (EX1). A dry run writes nothing,
    so it asks the folder's permissions instead. Anything the file system
    says no to on the way is said as `_export_write_error` says a failed
    write, with its hint, rather than escaping raw: `Path.is_dir` raises,
    rather than answering False, for a folder graft can't search.
    """
    try:
        dest = dest.expanduser()
    except RuntimeError:
        # `~name` for a user this Mac doesn't have.
        raise _export_failure(ConfigError(
            f"{dest} starts with {dest.parts[0]}, and graft can't find that user's home folder",
            hint="give the whole path, or start it with ~/ for your own home folder",
        ), "bad-destination") from None
    try:
        if not dest.is_absolute():
            dest = Path.cwd() / dest
        if dest.is_dir():
            raise _export_failure(ConfigError(
                f"{dest} is a folder: --export-ipa takes the file to write",
                hint=f"name the file too, such as {dest / 'Example.ipa'}",
            ), "bad-destination")
        if not dest.parent.is_dir():
            raise _export_failure(ConfigError(
                f"there's no folder at {dest.parent} to export into",
                hint="create it first, or choose another folder",
            ), "bad-destination")
        for folder, holds in ((cfg.cache_dir, "staged IPAs"), (cfg.built_dir, "archived builds")):
            if _inside(dest.parent, folder):
                raise _export_failure(ConfigError(
                    f"{dest} is in Graft's own folder for {holds} ({folder})",
                    hint="choose another folder: Graft manages the files in that one itself",
                ), "bad-destination")
        if os.path.lexists(dest) and not overwrite:
            raise _export_failure(ConfigError(
                f"{dest} already exists",
                hint="pass --overwrite to replace it, or choose another name",
            ), "exists")
        if dry_run:
            if not os.access(dest.parent, os.W_OK | os.X_OK):
                raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(dest.parent))
        else:
            fd, probe = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".partial")
            os.close(fd)
            os.unlink(probe)
    except OSError as exc:
        raise _export_write_error(dest, exc) from exc
    return dest


#: `os.link` failing with one of these means the filesystem has no hard links
#: (FAT and exFAT drives, some network shares), not that the name is taken.
_NO_HARD_LINKS = frozenset({errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EXDEV, errno.EMLINK})


def _export_write_error(dest: Path, exc: OSError) -> GraftError:
    """A failed export write, said with what to do about it."""
    hints = {
        errno.ENOSPC: f"free some space on the disk {dest.parent} is on, or choose another folder",
        errno.EPERM: (
            "macOS didn't let Graft write there: allow Graft (or Terminal, if you ran graft "
            "there) in System Settings › Privacy & Security › Files and Folders, or choose "
            "another folder"
        ),
        errno.EACCES: f"your user can't write to {dest.parent}; choose another folder",
        errno.EROFS: f"{dest.parent} is on a read-only disk; choose another folder",
    }
    reasons = {
        errno.ENOSPC: "no-space",
        errno.EPERM: "unwritable",
        errno.EACCES: "unwritable",
        errno.EROFS: "unwritable",
    }
    code = exc.errno or 0
    return _export_failure(
        GraftError(f"couldn't write {dest}: {exc.strerror or exc}", hint=hints.get(code)),
        reasons.get(code, "write-failed"),
    )


def _read_umask() -> int:
    """The process's umask. Python can only read it by setting another and
    putting it back, so 0o077 stands in for that instant: a file another
    thread made then would be made more private, not less."""
    mask = os.umask(0o077)
    os.umask(mask)
    return mask


#: The umask graft started with. Read once, here at import, before the build
#: starts any thread that could make a file while it is swapped out.
_UMASK = _read_umask()


def _write_export(src: Path, dest: Path, *, overwrite: bool) -> tuple[bool, int]:
    """Put `src` at `dest` whole, or not at all. Returns (replaced, size).

    Copied to a hidden temp file beside `dest`, then moved into place, so a
    Stop, a full disk or an error mid-copy leaves no half-written `dest`, and
    the temp file is removed on the way out. Not on every way out: a SIGKILL,
    a crash or a power cut mid-copy leaves it, hidden, beside `dest`
    (`.Example.ipa.k2j4xq.partial`), the decrypted app and all, and nothing
    sweeps that folder. From Terminal it is the user's to delete (the README
    says so); Graft.app's export writes into a folder macOS empties itself
    (`ExportStaging`), and moves the file to where the user chose (EX1, EX4).

    The file's mode is what the umask makes of 0666, as for any new file:
    `mkstemp` makes it 0600 whatever the umask, which made every export, and
    a file `--overwrite` replaced, readable by its owner alone (EX3).

    Without `overwrite` the move is a hard link, which fails on a name that
    exists: a file saved at `dest` while the build ran is never replaced,
    though the check before the build found nothing there. A filesystem
    without hard links falls back to looking, then renaming, which leaves the
    instant between the two.
    """
    taken = _export_failure(ConfigError(
        f"{dest} appeared while the IPA was being built, and was left alone",
        hint="pass --overwrite to replace it, or choose another name",
    ), "exists")
    tmp: Path | None = None
    try:
        # Inside the `try`, so a Stop the moment it returns finds `tmp` set
        # and the file goes with the rest.
        fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".partial")
        tmp = Path(tmp_name)
        with os.fdopen(fd, "wb") as out:
            os.fchmod(out.fileno(), 0o666 & ~_UMASK)
            with src.open("rb") as source:
                shutil.copyfileobj(source, out, 1 << 20)
            size = out.tell()
        if overwrite:
            replaced = os.path.lexists(dest)
            os.replace(tmp, dest)
            return replaced, size
        try:
            os.link(tmp, dest)
        except FileExistsError:
            raise taken from None
        except OSError as exc:
            if exc.errno not in _NO_HARD_LINKS:
                raise
            if os.path.lexists(dest):
                raise taken from None
            os.replace(tmp, dest)
        return False, size
    except OSError as exc:
        raise _export_write_error(dest, exc) from exc
    finally:
        # After a link, the temp name is the second of two; after a rename,
        # it is already gone.
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def _export_tweaked(
    tweaked: Path,
    dest: Path,
    *,
    overwrite: bool,
    app_name: str,
    app_cfg: AppConfig,
    version: str,
    dry_run: bool,
    emitter: JsonEmitter | None,
) -> None:
    """An export's stage 4, and its last: cyan's output, unsigned, at `dest`.

    `ipa-exported` goes out once the file is in place, and nothing after it
    but `build-end`. A dry run says where the file would go and writes nothing.
    """
    console.print(f"→ stage 4: exporting the unsigned IPA to [cyan]{escape(str(dest))}[/cyan]")
    if emitter:
        emitter.emit("stage", name="export", status="start")
    size: int | None = None
    if dry_run:
        replaced = os.path.lexists(dest)
        console.print(f"   [dim]would write {escape(str(dest))}[/dim]")
    else:
        replaced, size = _write_export(tweaked, dest, overwrite=overwrite)
        console.print(
            f"   [green]✓[/green] wrote {escape(dest.name)} ({_human_bytes(size)}), unsigned"
        )
    if emitter:
        emitter.emit("stage", name="export", status="end")
        emitter.emit(
            "ipa-exported", app=app_name, version=version, path=str(dest),
            filename=dest.name, size_bytes=size, tweaks=list(app_cfg.tweaks),
            replaced=replaced, dry_run=dry_run,
        )


def _require_developer_mode(
    cfg: Config, planned: Target, emitter: JsonEmitter | None, *, reveal: bool
) -> None:
    """Refuse a development-signed build whose phone has Developer Mode off,
    before stage 2 (`developer_mode.require_on`).

    `reveal`, a real build's: before refusing, have the phone show the
    Developer Mode switch, which iOS hides until a development app is
    installed. A reveal that fails is a warning; the refusal is the same.

    The phone is the one stage 5 installs on (`_planned_target`), which a
    build that installs always has: no target stopped it already.

    Said before the read: a Wi-Fi phone asleep can hold it, each step for up
    to `device_info.DEVELOPER_MODE_SECONDS`.
    """
    line = f"checking Developer Mode on {planned.udid}"
    console.print(f"→ {escape(line)}")
    if emitter:
        emitter.emit("log", source="graft", line=line)
    developer_mode.require_on(
        cfg.state_dir, planned.udid, reveal=reveal, warn=lambda message: _warn(emitter, message),
    )


@contextlib.contextmanager
def _refusing_for_the_device(outcome: update_run.Built | None) -> Iterator[None]:
    """A refusal raised in the block is about the device this build installs
    on, and no other (Developer Mode off there, or what the push would refuse
    in its LiveContainer): under `graft --update` it is told to `outcome`
    (`Built.target_refused`), so the app's next device still builds (spec
    §11.3). A plain build fails as before."""
    try:
        yield
    except GraftError:
        if outcome is not None:
            outcome.target_refused = True
        raise


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
    export_to: Path | None = None,
    overwrite: bool = False,
    outcome: update_run.Built | None = None,
) -> None:
    """Build one app, start to finish, entirely inside the report envelope.

    `log` is an injection seam for tests; `main` leaves it None so log.sqlite is
    opened inside the envelope, where a failure to open it is reportable.

    `export_to` makes the run an export (`--export-ipa`, `_verb_export_ipa`):
    the same build as far as cyan, then cyan's output is written, unsigned,
    to that path, and nothing after it runs. It never signs, never archives
    into `built_dir`, never looks for a device, and the app's delivery mode
    plays no part. It writes no history row and keeps no saved log, so the
    retention sweep never runs for it either. `overwrite` lets it replace a
    file already there.

    `outcome` is `graft --update`'s (`update_run.Built`): told the archive
    and the inputs digest as soon as the archive is written, told as stage 5
    begins, and told when that install is recorded, so the update can put
    the same archive on the app's other devices, with the same digest,
    whether or not this build's own install worked (spec §11.3).
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
        # Null for an export, which delivers nothing whatever the app's mode.
        # (`export_to` is a Path or None, and a Path is always true.)
        delivery=None if export_to else (app_cfg.delivery if app_cfg else None),
        dry_run=dry_run,
        for_playcover=for_playcover,
        export_to=export_to,
        # A lookup, as the envelope's header must be (`test_cli_build_envelope`).
        waits=outcome.waits if outcome else None,
    ) as report:
        app_cfg = cfg.get_app(app_name)
        report.tweaks = list(app_cfg.tweaks)
        exporting = export_to is not None
        if export_to is not None:
            # An export installs nothing. Every device step below is gated on
            # this — the pre-flight's device checks, the LiveContainer check
            # before cyan, both deliveries' stage 5 — so none can be reached
            # by an export, even before stage 4 sends it down neither delivery.
            skip_install = True
            # First, before minutes of work: a file it may not replace, a
            # folder that isn't there, or one of Graft's own.
            export_to = _check_export_destination(
                cfg, export_to, overwrite=overwrite, dry_run=dry_run
            )
            # Nothing else here applies. Neither refusal below is about an IPA
            # that is never signed, and `report.log` stays None: an export
            # writes no history row (`_verb_export_ipa` says why).
        else:
            # Inside the envelope so `build-end` carries the refusal: building
            # LiveContainer the sideload way signs it with zsign and breaks
            # every guest app's login. `--lc-install` is the way now.
            from .lc_manage import refuse_legacy_build

            refuse_legacy_build(app_cfg, free_account=cfg.free_account)
            # Sideload signs with the paid .p12 and profile; a free Apple ID
            # has neither. Here rather than at load, so the rest of a free
            # config (and every other app in it) keeps working; inside the
            # envelope, so the refusal is this build's `build-end`.
            cfg.require_delivery(app_cfg.delivery, app=app_cfg.name)
            # Opened here, not by the caller. `BuildLog.__init__` does a
            # `mkdir` and an `executescript` in the same state directory the
            # tee lives in, so a read-only or full volume makes it raise — and
            # constructed in `main` ahead of the loop it raised before any app
            # was attempted, producing no build event for any of them. That is
            # the exact scenario `open_tee` was hardened for, reached through a
            # different door.
            if log is None:
                log = BuildLog(cfg.state_dir / "log.sqlite")
            report.log = log

        console.rule(f"[bold]{escape(app_name)}[/bold] — {escape(app_cfg.bundle_id)}")

        # Tee every JSON event to a per-build .jsonl so the GUI can replay it
        # later. Only meaningful when --json is in use; non-JSON runs leave no
        # machine-readable artifact. Not for an export: with no history row,
        # nothing could ever reach its log.
        if emitter is not None and not dry_run and not exporting:
            report.open_tee(cfg.state_dir / "build-logs")

        # `tmp_root` is read by the finally block, so it is initialized BEFORE
        # the try: a failure ahead of stage 2.5 must not trip an
        # UnboundLocalError on the way out. What a *failed* build reports is no
        # longer read off these — the envelope has it — so nothing here has to
        # stay in step with the failure path.
        cached: CachedIPA | None = None
        tmp_root: Path | None = None
        keep_tmp = verbose
        try:
            # Where stage 5 installs (spec §5b's table), asked once and first,
            # so everything below means the same device (`_planned_target`).
            # Nothing is looked for when nothing installs: an export or Skip
            # install.
            planned = None if skip_install else _planned_target(cfg, app_cfg, device, emitter)

            # Pre-flight — only the checks this app's delivery mode actually needs.
            # (The full, mode-agnostic check set is available via `--check`.)
            # An export needs cyan and nothing else, whatever the mode.
            checks = run_export_checks() if exporting else run_all_checks(
                cfg, delivery=app_cfg.delivery, will_install=not skip_install, app=app_cfg,
                target=planned.udid if planned else None,
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

            # Developer Mode, before minutes of work: on iOS 16 and later a
            # development-signed app installs with it off and then won't open.
            # Only an install signed with a development profile asks
            # (`developer_mode.needed`; ad hoc and enterprise profiles don't,
            # and None, a profile it can't read, is pre-flight's to say), a dry
            # run included (it resolves a device too). An export has
            # `skip_install` set. None from the phone, the answer for iOS 15, a
            # locked phone or no answer in time, never stops a build. A real
            # build refused for it first has the phone show the switch, which
            # iOS hides; a dry run changes nothing on the phone, so it doesn't.
            # The phone it reads is `planned`, the one stage 5 installs on.
            if planned is not None and developer_mode.needed(cfg, app_cfg) is True:
                with _refusing_for_the_device(outcome):
                    _require_developer_mode(cfg, planned, emitter, reveal=not dry_run)

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
            # Staging has removed the buyer's metadata and licenses from every
            # IPA since 1.1 (`provide_ipa.stage`); one staged before that still
            # has them, and would carry them into the build and onto the
            # device. Healed once, in place — or, when the cache won't take
            # the write, built from a scrubbed copy in the work directory
            # (`_without_account_data`). Not on a dry run, which touches
            # nothing. `source` is what verify and cyan read from here on.
            source = cached.path
            tmp_root = Path(tempfile.mkdtemp(prefix=f"graft-{app_name}-"))
            if not dry_run:
                source = _without_account_data(cached.path, emitter=emitter, workdir=tmp_root)
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
            if not skip_decrypt_check:
                console.print("→ stage 2.5: verifying decryption (cryptid 0)")
                if emitter:
                    emitter.emit("stage", name="verify", status="start")
                extract_dir = tmp_root / "verify"
                extract_ipa(source, extract_dir)
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
                # The whole app was unpacked only to read Mach-O headers. Kept
                # until the build ended, a Stop had it to delete as well as
                # cyan's own unpacked copy: 3 s for a real app, past the Mac
                # app's grace before SIGKILL. It is not read again.
                shutil.rmtree(extract_dir, ignore_errors=True)

            # Fail fast: the push's refusals, before the injection runs.
            if app_cfg.delivery == "livecontainer" and not skip_install and not dry_run:
                with _refusing_for_the_device(outcome):
                    _check_lc_target_early(cfg, app_cfg, log=log, target=planned)

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
            cy = run_cyan(cfg, app_cfg, source, tweaked, dry_run=dry_run)
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
                fixed = _prepare_for_playcover(
                    tweaked, emitter, tweaks=[cfg.tweak_path(t) for t in app_cfg.tweaks],
                )
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
            #
            # An export takes neither: cyan's output goes where it was told,
            # unsigned, and that is the run's last step. `report.ipa_path`
            # stays None — the file is the user's, not an archive — so no
            # retention sweep is asked for below.
            if export_to is not None:
                _export_tweaked(
                    tweaked, export_to, overwrite=overwrite, app_name=app_name,
                    app_cfg=app_cfg, version=cached.version, dry_run=dry_run,
                    emitter=emitter,
                )
            elif app_cfg.delivery == "livecontainer":
                # Skip signing entirely; LiveContainer runs the tweaked IPA unsigned.
                console.print("→ stage 4: archiving tweaked IPA (delivery=livecontainer)")
                if emitter:
                    emitter.emit("stage", name="archive", status="start")
                if not dry_run:
                    # Shared until the history row is written (`hold_archives`).
                    report.hold_archives(cfg)
                    cfg.built_dir.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%Y%m%dT%H%M%S")
                    report.ipa_path = cfg.built_dir / f"{app_name}_{cached.version}_{ts}.ipa"
                    private_copy(tweaked, report.ipa_path)
                    console.print(f"   archived to [cyan]{report.ipa_path}[/cyan]")
                    if outcome is not None:
                        outcome.archived(report.ipa_path, inputs, cached.version)
                if emitter:
                    emitter.emit("stage", name="archive", status="end", path=str(report.ipa_path) if report.ipa_path else None)

                if not skip_install:
                    # The transport the target was resolved with, rather
                    # than "USB" asserted regardless. usbmuxd enumerates
                    # Wi-Fi-paired devices too, and this line claimed a cable
                    # while pushing 117 MB over the network — a build log that
                    # misdescribes what it did is the thing this tool is for.
                    # Set whenever a build installs (`_planned_target`).
                    assert planned is not None
                    report.udid, transport = planned.udid, planned.transport
                    console.print(
                        f"→ stage 5: pushing into LiveContainer over {_transport_label(transport)}"
                    )
                    if outcome is not None:
                        outcome.installing()
                    if emitter:
                        emitter.emit("stage", name="lc-push", status="start", transport=transport)
                    # Under `graft --update`, a Stop from here on waits for
                    # this build to end (`stop_deferral`): never a half-pushed
                    # IPA. A no-op for a plain build.
                    if not dry_run:
                        stop_deferral.finish_before_stopping(app_name, "lc-push")
                    lc_result = lc_runner.push_ipa_to_livecontainer(
                        report.ipa_path if report.ipa_path else tweaked,
                        udid=report.udid,
                        inner_filename=f"{app_name}.ipa",
                        **_lc_target(cfg, app_cfg, log=log, udid=report.udid),
                        dry_run=dry_run,
                    )
                    if dry_run:
                        what = ("the IPA" if lc_result.size_bytes is None
                                else _human_bytes(lc_result.size_bytes))
                        console.print(
                            f"   [dim]would push {what} "
                            f"to {lc_result.documents_path} in {lc_result.bundle_id}[/dim]"
                        )
                    else:
                        console.print(
                            f"   [green]✓[/green] {lc_result.size_bytes/1_000_000:.1f} MB into "
                            f"{lc_result.bundle_id} on {report.udid}"
                        )
                        console.print(
                            "   [dim]open LiveContainer on the device, the new IPA appears in its "
                            "Documents tab and can be added as a managed app[/dim]"
                        )
                        _report_lc_choice(emitter, app_name=app_name, result=lc_result,
                                          pinned=bool(app_cfg.lc_bundle_id))
                    if emitter:
                        # C-DRYRUN: `documents_path` is where in that
                        # LiveContainer's sandbox the IPA goes, and `dry_run`
                        # says whether it went. On a dry run `bundle_id` is
                        # the container discovery chose on the phone, or the
                        # pin when the phone did not answer (`lc_runner`).
                        # `udid` and `device_name` (null when Graft was never
                        # told one) are the device it went to, for the app's
                        # "Sent to LiveContainer on <device>" (2.3.1 B9).
                        emitter.emit(
                            "stage", name="lc-push", status="end",
                            bundle_id=lc_result.bundle_id, size_bytes=lc_result.size_bytes,
                            others=lc_result.others,
                            documents_path=lc_result.documents_path, dry_run=dry_run,
                            udid=planned.udid, device_name=planned.name,
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
                    if outcome is not None:
                        outcome.installed = True
            else:
                # Classic sideload: zsign + install over usbmux.
                console.print("→ stage 4: signing with zsign")
                if emitter:
                    emitter.emit("stage", name="sign", status="start")
                signed = tmp_root / f"{app_name}-signed.ipa"
                sg = run_zsign(
                    cfg, tweaked, signed, app=app_cfg, dry_run=dry_run,
                    # The device stage 5 installs on, which an auto-provisioned
                    # profile must include (`signer.target_profile_udids`).
                    target=planned.udid if planned else None,
                    # And, under `graft --update`, the app's other devices the
                    # same archive goes to next: one profile covers them all.
                    also=outcome.also_for if outcome is not None else (),
                    # Read from `source`, the app as it came: a dry run's
                    # cyan made no `tweaked`, and cyan adds no entitlements.
                    before_signing=lambda profile: _warn_ungranted_capabilities(
                        emitter, app_cfg, source, profile
                    ),
                )
                if outcome is not None:
                    # Devices App Store Connect doesn't have, so the profile
                    # doesn't cover them: their installs are refused unpushed.
                    outcome.not_in_profile = tuple(sg.unregistered_devices)
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
                    console.print(f"   [dim]{escape(sg.stdout)}[/dim]")
                else:
                    # Each escaped: the subject and the team (the certificate's
                    # OU) are whatever made the .p12 says (combined fix check #27).
                    console.print(
                        f"   [green]✓[/green] team={escape(sg.team_id or '?')} "
                        f"subject={escape(sg.cert_subject or '?')} expiry={escape(sg.expiry or '?')}"
                    )
                    report.signing_team = sg.team_id
                if emitter:
                    emitter.emit("stage", name="sign", status="end")
                _emit_log_lines(emitter, "zsign", sg.stdout)
                _emit_log_lines(emitter, "zsign", sg.stderr)

                if not dry_run:
                    # Shared until the history row is written (`hold_archives`).
                    report.hold_archives(cfg)
                    cfg.built_dir.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%Y%m%dT%H%M%S")
                    report.ipa_path = cfg.built_dir / f"{app_name}_{cached.version}_{ts}.ipa"
                    private_copy(signed, report.ipa_path)
                    console.print(f"   archived to [cyan]{report.ipa_path}[/cyan]")
                    if outcome is not None:
                        outcome.archived(report.ipa_path, inputs, cached.version)

                if not skip_install:
                    console.print("→ stage 5: installing on the device")
                    if outcome is not None:
                        outcome.installing()
                    if emitter:
                        emitter.emit("stage", name="install", status="start")
                    # As for the push above: never a half-written install.
                    if not dry_run:
                        stop_deferral.finish_before_stopping(app_name, "install")
                    # The phone planned before the pre-flight, which the
                    # Developer Mode check read: not a second resolution
                    # minutes later, which could pick another. Gone since,
                    # the install says so.
                    # Set whenever a build installs (`_planned_target`).
                    assert planned is not None
                    report.udid, transport = planned.udid, planned.transport
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
                    if outcome is not None:
                        outcome.installed = True

            console.print(
                f"[bold green]✓ {app_cfg.cyan.display_name or app_name}[/bold green] "
                f"({app_cfg.bundle_id}) {cached.version} in {report.elapsed:.1f}s"
            )
            # Retention, on the success path only: sweeping after a failure
            # would delete the history a user is mid-way through debugging. A
            # build that fails *after* archiving (the push or install) leaves
            # its archive with no row naming it; the sweep gives such archives
            # no keep slot (`plan_prune`'s `reinstallable`), so they never
            # outrank a build that can be re-installed. Run by the envelope
            # once the history row is written and this build's share of the
            # retention lock released (`_BuildReport.sweep_when_done`), still
            # ahead of build-end.
            if not dry_run and report.ipa_path is not None:
                report.sweep_when_done(cfg, trash_dir=trash_dir)
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
