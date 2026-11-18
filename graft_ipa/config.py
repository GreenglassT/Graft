"""Config loader and validation.

Reads ~/.config/graft/config.yaml (XDG-compliant) into typed dataclasses.
"""

from __future__ import annotations

import contextlib
import fcntl
import functools
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml

from . import data_dirs
from .errors import ConfigError

T = TypeVar("T")


def _atomic_write_yaml(path: Path, raw: Any) -> None:
    """Serialize ``raw`` to YAML and replace ``path`` atomically.

    config.yaml holds every app entry, every repo, and the signing/cert
    configuration, and nothing backs it up. ``path.write_text()`` truncates
    the file and *then* writes, so anything that interrupts the write — a
    crash, a SIGKILL'd GUI, power loss — can leave an empty or half-written
    config that no longer parses. Serializing first (so a dump failure never
    touches the file), writing to a sibling temp file, fsync'ing it, and then
    ``os.replace()``-ing onto the target means a reader always sees either the
    complete old file or the complete new one. ``os.replace`` is atomic within
    a filesystem, which the sibling temp file guarantees.

    The temp file inherits the target's permissions when the target already
    exists; a brand-new config keeps mkstemp's owner-only mode.
    """
    _atomic_write_text(path, yaml.safe_dump(raw, sort_keys=False))


def _atomic_write_text(path: Path, text: str) -> None:
    """The write half of `_atomic_write_yaml`, for text already serialized —
    the starter config, whose commented example `yaml.safe_dump` cannot
    produce.

    Resolved first, so a symlinked config.yaml (a dotfiles repo, say) is
    rewritten where it really lives: `os.replace` onto the link itself would
    swap the link for a regular file and quietly fork the config away from the
    one the user edits. The temp file goes beside the real target too, which
    is what keeps the replace on one filesystem.

    UTF-8 and newline-exact, so a config's own non-ASCII text and line endings
    come back as they went in rather than as the locale decides.
    """
    path = Path(os.path.realpath(path))
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=directory)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass  # target doesn't exist yet (or chmod unsupported) — keep 0600
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return  # can't fsync the directory; the replace itself is still atomic
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)


# Which config locks this thread already holds, by lock-file path. `flock` is
# per open file description, so taking the same lock again through a second
# `open` in one process would wait on itself forever.
_locks_held = threading.local()


def _lock_file(path: Path) -> Path:
    """The sidecar lock for `path`: beside the real file, so two symlinks to
    one config share one lock."""
    real = Path(os.path.realpath(path))
    return real.with_name(f"{real.name}.lock")


def _config_lock_held(path: Path) -> bool:
    return str(_lock_file(path)) in getattr(_locks_held, "paths", set())


@contextlib.contextmanager
def _config_lock(path: Path) -> Iterator[None]:
    """Hold an exclusive `flock` on `config.yaml.lock` for a read-modify-write.

    Every mutator in this module takes it around the whole of its read,
    change and write, so two of them — the GUI's `--set-app-options` racing an
    `--add-app`, say — cannot each read the same file and have the second
    write erase the first's change. Advisory, so it binds only code that asks.

    A sidecar rather than the config itself, because every write *replaces*
    config.yaml with a new inode and a lock on the old one would guard
    nothing. Re-entrant within a thread.

    Proceeds unlocked when locking is impossible rather than refusing the
    edit: the lock file cannot be created (a read-only directory — nothing
    can write the config there either), or the filesystem does not do `flock`
    (NFS without a lock daemon: ENOTSUP, ENOLCK).

    Never waits forever. A holder that is stuck — SIGSTOP'd, wedged — would
    otherwise hang every caller behind it, the GUI's panes included, so the
    wait is non-blocking attempts for `_LOCK_TIMEOUT` seconds and then a
    ConfigError that names the lock and who is likely holding it.
    """
    lock = _lock_file(path)
    held: set[str] = getattr(_locks_held, "paths", None) or set()
    _locks_held.paths = held
    if str(lock) in held:
        yield
        return
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        fd = None
    if fd is None:
        yield
        return
    try:
        if _take_flock(fd, lock):
            held.add(str(lock))
        try:
            yield
        finally:
            held.discard(str(lock))
    finally:
        os.close(fd)  # closing the descriptor releases the flock


#: How long a config writer waits for another to finish, and how often it
#: looks. Ten seconds is far longer than any real edit holds the lock (a
#: read, a small dump, an fsync) and short enough that a stuck holder reads
#: as an error rather than as the GUI freezing.
_LOCK_TIMEOUT = 10.0
_LOCK_POLL = 0.05


def _take_flock(fd: int, lock: Path) -> bool:
    """`LOCK_EX` on `fd`, bounded. True when held, False when this filesystem
    cannot lock at all; ConfigError when someone else holds it too long."""
    deadline = time.monotonic() + _LOCK_TIMEOUT
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            # Held elsewhere. Before the generic OSError below, which it is a
            # subclass of: this one means "wait", that one "cannot lock here".
            if time.monotonic() >= deadline:
                raise ConfigError(
                    f"another graft process is editing the config (lock held: {lock})",
                    hint="let it finish and try again; if no graft is running, "
                    "the process holding the lock is stopped or stuck",
                ) from None
            time.sleep(_LOCK_POLL)
        except OSError:
            return False



def _locked(fn: Callable[..., T]) -> Callable[..., T]:
    """Run a config mutator under `_config_lock(path)`, `path` being its first
    argument — the whole read, change and write, so no other writer's change
    can land between the read and the write and be erased by it."""

    @functools.wraps(fn)
    def wrapper(path: Path, *args: Any, **kwargs: Any) -> T:
        with _config_lock(path):
            return fn(path, *args, **kwargs)

    return wrapper

# Where each directory lives, and the move from the pre-rename names, belong
# to `data_dirs`; these name what goes inside them.


def default_config_path() -> Path:
    return data_dirs.config_dir() / "config.yaml"


def default_cache_dir() -> Path:
    return data_dirs.cache_dir() / "ipas"


def default_built_dir() -> Path:
    return data_dirs.cache_dir() / "built"


def default_tweaks_dir() -> Path:
    return data_dirs.config_dir() / "tweaks"


def default_state_dir() -> Path:
    return data_dirs.state_dir()


def _expand(p: str | Path) -> Path:
    return Path(os.path.expandvars(str(p))).expanduser()


# How many archived IPAs to keep per (app, version) in built_dir. Every
# successful build appends one (~270 MB), so without a cap a single debugging
# session leaves a dozen near-identical copies behind.
#
# Two, and the count now means something different than it did under the old
# per-app grouping. There, N was "rebuilds of one version to keep" *and* the
# app's entire history budget, so 3 was a compromise between two unrelated
# jobs. Per (app, version), version history is no longer competing for these
# slots — each version keeps its own — so N answers one question: how far back
# within a single version is worth keeping?
#
# One step. Rebuilds of the same version differ only in the tweaks injected,
# and the sweep runs at the end of a *successful* build, which means keep=1
# would trash the last build that worked the moment a build with a new tweak
# succeeds-but-misbehaves-on-device. Keeping 2 costs ~270 MB per version and
# buys exactly the rollback that failure mode needs; a third rebuild of the
# same version has no story attached to it.
DEFAULT_ARCHIVE_KEEP = 2


def _archive_keep(raw: Any) -> int:
    """Validate the optional top-level `archive_keep:` setting.

    Unlike the other optional settings this one drives a *destructive* action,
    so a value we can't read has to be an error rather than a silent fallback:
    `archive_keep: "ten"` quietly becoming the default would trash the builds
    the user meant to keep. `bool` is rejected explicitly because it is an
    `int` subclass, and `archive_keep: true` meaning "keep 1" helps nobody.
    """
    if raw is None:
        return DEFAULT_ARCHIVE_KEEP
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigError(
            f"archive_keep must be a non-negative integer, got {raw!r}",
            hint=f"remove the key to use the default of {DEFAULT_ARCHIVE_KEEP}",
        )
    if raw < 0:
        raise ConfigError(
            f"archive_keep must be a non-negative integer, got {raw!r}",
            hint="use 0 to keep only the archive the current build just produced",
        )
    return raw


@dataclass
class SigningConfig:
    cert: Path
    cert_password_env: str
    profile: Path
    team_id: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SigningConfig:
        missing = [k for k in ("cert", "cert_password_env", "profile", "team_id") if k not in data]
        if missing:
            raise ConfigError(f"signing section missing keys: {', '.join(missing)}")
        return cls(
            cert=_expand(data["cert"]),
            cert_password_env=str(data["cert_password_env"]),
            profile=_expand(data["profile"]),
            team_id=str(data["team_id"]),
        )


@dataclass
class AppStoreConnectConfig:
    """Optional. When present, enables auto-provisioning per app."""

    key_id: str
    issuer_id: str
    key_file: Path
    device_udids: list[str]
    profile_cache_dir: Path

    @classmethod
    def from_dict(
        cls, data: dict[str, Any] | None, *, default_cache_dir: Path
    ) -> AppStoreConnectConfig | None:
        if not data:
            return None
        missing = [k for k in ("key_id", "issuer_id", "key_file") if k not in data]
        if missing:
            raise ConfigError(
                f"app_store_connect section missing keys: {', '.join(missing)}"
            )
        return cls(
            key_id=str(data["key_id"]),
            issuer_id=str(data["issuer_id"]),
            key_file=_expand(data["key_file"]),
            device_udids=[str(u) for u in (data.get("device_udids") or [])],
            profile_cache_dir=_expand(data.get("profile_cache_dir") or default_cache_dir),
        )


def _default_unfairplay_models() -> Path:
    """UnfairPlay's own default, honouring its own override.

    `UNFAIRPLAY_MODEL_DIR` is the tool's variable, not ours: a user who set it
    for `unfairplay` by hand gets the same directory here without saying it
    twice. Read at load time because a GUI-launched CLI inherits the launchd
    environment, where it is never set — the config key is the way to say it
    there.
    """
    env = os.environ.get("UNFAIRPLAY_MODEL_DIR")
    if env:
        return _expand(env)
    return Path.home() / "Library" / "Application Support" / "unfairplay" / "models"


@dataclass
class AppStoreToolsConfig:
    """Optional `appstore:` section: where the user's own ipatool and UnfairPlay live.

    Both are installed by hand, outside this project, and only ever *run* by
    it — the Apple ID login and the FairPlay enrollment stay Terminal commands
    the user types (see `appstore.py`). The defaults are the paths they were
    installed at on 2026-11-17, so a missing section means "where they are".

    Every field is a path, `~`-expanded, and nothing else is accepted: a typo'd
    `ipatool: 2.8.0` (the version where the path should be) would otherwise
    become `Path("2.8.0")`, resolved against whatever directory the CLI happened
    to start in.
    """

    ipatool: Path = field(default_factory=lambda: Path.home() / ".local" / "bin" / "ipatool")
    unfairplay: Path = field(
        default_factory=lambda: data_dirs.data_dir() / "unfairplay" / "unfairplay"
    )
    unfairplay_models: Path = field(default_factory=_default_unfairplay_models)

    @classmethod
    def from_dict(cls, data: Any) -> AppStoreToolsConfig:
        if data is None:
            return cls()
        if not isinstance(data, dict):
            raise ConfigError("'appstore' must be a mapping of tool name -> path")
        paths: dict[str, Path] = {}
        for key in ("ipatool", "unfairplay", "unfairplay_models"):
            value = data.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value.strip():
                raise ConfigError(
                    f"appstore.{key} must be a path, got {value!r}",
                    hint="remove the key to use the default location",
                )
            paths[key] = _expand(value)
        return cls(**paths)


@dataclass
class CyanOptions:
    display_name: str | None = None
    keep_bundle_id: bool = True
    new_bundle_id: str | None = None
    strip_extensions: bool = False
    strip_supported_devices: bool = True
    compression_level: int = 6
    extra_args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> CyanOptions:
        data = data or {}
        return cls(
            display_name=data.get("display_name"),
            keep_bundle_id=bool(data.get("keep_bundle_id", True)),
            new_bundle_id=data.get("new_bundle_id"),
            strip_extensions=bool(data.get("strip_extensions", False)),
            strip_supported_devices=bool(data.get("strip_supported_devices", True)),
            compression_level=int(data.get("compression_level", 6)),
            extra_args=list(data.get("extra_args", []) or []),
        )


@dataclass
class AppConfig:
    name: str
    bundle_id: str
    preferred_version: str
    tweaks: list[str]
    cyan: CyanOptions
    auto_profile: bool = False
    exact_app_id: bool = False  # if true, register/use bundle_id directly, not its wildcard
    profile_override: Path | None = None  # explicit per-app profile path
    delivery: str = "sideload"  # "sideload" or "livecontainer"
    lc_bundle_id: str | None = None  # pin which LiveContainer to push into (livecontainer delivery)

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> AppConfig:
        if "bundle_id" not in data:
            raise ConfigError(f"app '{name}': missing bundle_id")
        profile_override = data.get("profile")
        delivery = str(data.get("delivery", "sideload")).lower()
        if delivery not in ("sideload", "livecontainer"):
            raise ConfigError(
                f"app '{name}': invalid delivery '{delivery}' (must be 'sideload' or 'livecontainer')"
            )
        return cls(
            name=name,
            bundle_id=str(data["bundle_id"]),
            preferred_version=str(data.get("preferred_version", "latest")),
            tweaks=list(data.get("tweaks", []) or []),
            cyan=CyanOptions.from_dict(data.get("cyan")),
            auto_profile=bool(data.get("auto_profile", False)),
            exact_app_id=bool(data.get("exact_app_id", False)),
            profile_override=_expand(profile_override) if profile_override else None,
            delivery=delivery,
            lc_bundle_id=(str(data["lc_bundle_id"]) if data.get("lc_bundle_id") else None),
        )


@dataclass
class ConfiguredRepo:
    url: str
    name: str

    @classmethod
    def from_entry(cls, entry: Any) -> ConfiguredRepo:
        # Lenient: accept either a bare URL string or a {url, name} mapping.
        if isinstance(entry, str):
            return cls(url=entry, name=entry)
        if not isinstance(entry, dict):
            raise ConfigError(
                f"repo entry must be a URL string or mapping, got {type(entry).__name__}"
            )
        url = str(entry.get("url", "")).strip()
        if not url:
            raise ConfigError("repo entry is missing a 'url'")
        return cls(url=url, name=str(entry.get("name") or url))


@dataclass
class Config:
    path: Path
    signing: SigningConfig
    cache_dir: Path
    built_dir: Path
    tweaks_dir: Path
    state_dir: Path
    apps: dict[str, AppConfig]
    repos: list[ConfiguredRepo] = field(default_factory=list)
    default_device_udid: str | None = None
    app_store_connect: AppStoreConnectConfig | None = None
    archive_keep: int = DEFAULT_ARCHIVE_KEEP
    appstore: AppStoreToolsConfig = field(default_factory=AppStoreToolsConfig)

    def get_app(self, name: str) -> AppConfig:
        if name not in self.apps:
            available = ", ".join(sorted(self.apps)) or "(none configured)"
            raise ConfigError(
                f"app '{name}' is not configured",
                hint=f"available apps: {available}. Edit {self.path} to add one.",
            )
        return self.apps[name]

    def tweak_path(self, filename: str) -> Path:
        """Resolve a tweak filename to an absolute path under tweaks_dir."""
        # Allow absolute paths in the config (advanced users), but the typical case
        # is just a filename relative to tweaks_dir.
        p = _expand(filename)
        if p.is_absolute():
            return p
        return self.tweaks_dir / filename

    def tweak_key(self, entry: str) -> tuple[int, int] | str:
        """Stable identity for a tweak reference.

        Inode identity when the file exists, which makes case-insensitive
        volumes, symlinks and hardlinks all resolve to the same tweak; a
        normalized path string when it doesn't, so dangling config entries
        still compare equal to themselves.
        """
        p = self.tweak_path(entry)
        try:
            st = p.stat()
            return (st.st_dev, st.st_ino)
        except OSError:
            return os.path.normcase(os.path.normpath(str(p)))


def load_config(path: Path | None = None) -> Config:
    # Through `_load_raw`, which `read_settings` also uses: the exists/parse/
    # mapping checks were open-coded here with the same three messages, so the
    # strict loader and the lenient reader are now visibly one read with two
    # interpretations rather than two readers that happen to agree.
    cfg_path = path or default_config_path()
    raw = _load_raw(cfg_path)

    if "signing" not in raw:
        raise ConfigError("config missing 'signing' section")
    signing = SigningConfig.from_dict(raw["signing"])

    cache_dir = _expand(raw.get("cache_dir") or default_cache_dir())
    built_dir = _expand(raw.get("built_dir") or default_built_dir())
    tweaks_dir = _expand(raw.get("tweaks_dir") or default_tweaks_dir())
    state_dir = _expand(raw.get("state_dir") or default_state_dir())

    apps_raw = raw.get("apps") or {}
    if not isinstance(apps_raw, dict):
        raise ConfigError("'apps' must be a mapping of name -> app config")
    apps = {name: AppConfig.from_dict(name, data) for name, data in apps_raw.items()}

    asc = AppStoreConnectConfig.from_dict(
        raw.get("app_store_connect"),
        default_cache_dir=cfg_path.parent / "profiles",
    )

    repos_raw = raw.get("repos") or []
    if not isinstance(repos_raw, list):
        raise ConfigError("'repos' must be a list")
    repos = [ConfiguredRepo.from_entry(e) for e in repos_raw if e]

    return Config(
        path=cfg_path,
        signing=signing,
        cache_dir=cache_dir,
        built_dir=built_dir,
        tweaks_dir=tweaks_dir,
        state_dir=state_dir,
        apps=apps,
        repos=repos,
        default_device_udid=raw.get("default_device_udid"),
        app_store_connect=asc,
        archive_keep=_archive_keep(raw.get("archive_keep")),
        appstore=AppStoreToolsConfig.from_dict(raw.get("appstore")),
    )


@_locked
def write_default_config(path: Path, **overrides: Any) -> None:
    """Write a starter config file. Used by --init."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg_dir = path.parent
    template: dict[str, Any] = {
        "signing": {
            "cert": str(overrides.get("cert", cfg_dir / "AppleDevelopment.p12")),
            "cert_password_env": "GRAFT_P12_PASS",
            "profile": str(overrides.get("profile", cfg_dir / "wildcard.mobileprovision")),
            "team_id": overrides.get("team_id", "REPLACEME"),
        },
        "cache_dir": str(default_cache_dir()),
        "tweaks_dir": str(default_tweaks_dir()),
    }
    if "default_device_udid" in overrides:
        template["default_device_udid"] = overrides["default_device_udid"]

    # Written as text so the example can stay a comment: the file configures
    # no apps until the user adds one, and `yaml.safe_dump` has no way to say
    # "here is what one looks like" without configuring it.
    _atomic_write_text(path, yaml.safe_dump(template, sort_keys=False) + _STARTER_APPS)


#: The tail of the starter config: no apps, and one commented-out entry made of
#: placeholders. `tests/test_starter_config.py` uncomments it and loads it, so
#: it cannot drift out of the shape `AppConfig.from_dict` accepts.
_STARTER_APPS = """\

# Apps to build. Add one with `graft --add-app <App Store URL>` (or
# --add-app-manual), or by hand — an entry looks like this:
#
# apps:
#   example:
#     bundle_id: com.example.app
#     preferred_version: latest
#     delivery: sideload          # or livecontainer
#     tweaks:
#       - some-tweak.deb          # a file in tweaks_dir
#     cyan:
#       display_name: Example
#       keep_bundle_id: true      # some apps tie sign-in to their bundle id
#       strip_extensions: false
#       strip_supported_devices: true
#       compression_level: 6
apps: {}
"""


def _load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(
            f"config file not found at {path}",
            hint="run `graft --init` to create one",
        )
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"failed to parse {path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping at the top level")
    return raw


def _entry_url(entry: Any) -> str | None:
    """URL of a repos: list entry — either a bare string or a {url, ...} mapping."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
        return entry.get("url")
    return None


@_locked
def add_repo(path: Path, url: str, name: str) -> None:
    """Append a repo to config.yaml's `repos:` list. No-op if `url` is present.

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not preserved.
    """
    raw = _load_raw(path)
    repos = raw.get("repos") or []
    if not isinstance(repos, list):
        raise ConfigError("'repos' must be a list")
    if any(_entry_url(entry) == url for entry in repos):
        return
    repos.append({"url": url, "name": name})
    raw["repos"] = repos
    _atomic_write_yaml(path, raw)


@_locked
def remove_repo(path: Path, url: str) -> None:
    """Remove the repo whose URL matches `url` from config.yaml. No-op if absent.

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not preserved.
    """
    raw = _load_raw(path)
    repos = raw.get("repos") or []
    if not isinstance(repos, list):
        raise ConfigError("'repos' must be a list")
    raw["repos"] = [e for e in repos if _entry_url(e) != url]
    _atomic_write_yaml(path, raw)


@_locked
def set_app_store_connect(path: Path, section: dict[str, Any]) -> None:
    """Replace config.yaml's `app_store_connect:` section (`--setup-portal`).

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not preserved.
    """
    raw = _load_raw(path)
    raw["app_store_connect"] = section
    _atomic_write_yaml(path, raw)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify_name(text: str) -> str:
    """Reduce a display name to a YAML-key-safe slug.

    Lowercases, strips non-alphanumerics into single underscores, trims leading
    and trailing underscores. Falls back to ``"app"`` if the result is empty.
    """
    lowered = (text or "").lower()
    slug = _SLUG_RE.sub("_", lowered).strip("_")
    return slug or "app"


# --- apps: mutator envelope ------------------------------------------------
#
# Every app mutator below is a load / guard / mutate / write sandwich over the
# same `apps:` mapping. These four helpers hold the guard text and the reattach
# in one place so the copies can't drift apart again (they already had: the
# "not found" error grew a `hint` in two of the three call sites that raise it).


def _load_apps(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Load config.yaml and return ``(raw, apps)`` with ``apps`` guarded.

    Pairs with :func:`_commit_apps`. Deliberately *not* a context manager that
    writes on ``__exit__``: ``set_app_options`` has to be able to decide it has
    nothing to do and leave the file alone, and ``add_repo`` relies on the same
    property on the repos side. Since ``_atomic_write_yaml`` round-trips
    through ``yaml.safe_dump``, a write-on-exit envelope would reformat the
    user's config and drop its comments on a call that changed nothing. Keeping
    the write an explicit call makes a no-op a plain ``return``.
    """
    raw = _load_raw(path)
    apps = raw.get("apps") or {}
    if not isinstance(apps, dict):
        raise ConfigError("'apps' must be a mapping of name -> app config")
    return raw, apps


def _commit_apps(path: Path, raw: dict[str, Any], apps: dict[str, Any]) -> None:
    """Reattach ``apps`` to ``raw`` and write config.yaml atomically.

    The reattach is load-bearing: ``_load_apps`` substitutes a fresh dict when
    the ``apps:`` key is absent or null, so mutating that dict alone would
    write a config with no apps in it.
    """
    raw["apps"] = apps
    _atomic_write_yaml(path, raw)


def _require_app(apps: dict[str, Any], name: str, path: Path) -> Any:
    """Return the raw ``apps[name]`` entry, or raise naming what *is* configured."""
    if name not in apps:
        raise ConfigError(
            f"app {name!r} not found in {path}",
            hint=f"available apps: {', '.join(sorted(apps)) or '(none configured)'}",
        )
    return apps[name]


def _require_app_mapping(apps: dict[str, Any], name: str, path: Path) -> dict[str, Any]:
    """:func:`_require_app` plus the guard that the entry is a patchable mapping.

    ``remove_app`` deliberately stays on the looser :func:`_require_app`:
    deleting a corrupt entry (a bare ``example:`` with no body) has to keep
    working, since deletion is how you'd fix one.
    """
    entry = _require_app(apps, name, path)
    if not isinstance(entry, dict):
        raise ConfigError(f"app {name!r} entry is not a mapping")
    return entry


@_locked
def add_app(
    path: Path,
    name: str,
    app_config: dict[str, Any],
    *,
    dedup: bool = False,
) -> str:
    """Append a new app entry to ``config.yaml``'s ``apps:`` mapping.

    Returns the actual key written. When ``dedup`` is False (the default), a
    collision raises ``ConfigError``. When True, the on-disk apps mapping is
    re-checked atomically and a ``_2`` / ``_3`` / ... suffix is appended until
    unique — so two concurrent ``--add-app`` calls can't race past each other
    (the previous in-memory ``_unique_app_name`` snapshot was TOCTOU-prone).

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not
    preserved.
    """
    raw, apps = _load_apps(path)
    final_name = name
    if final_name in apps:
        if not dedup:
            raise ConfigError(
                f"an app named {final_name!r} already exists in {path}"
            )
        i = 2
        while f"{name}_{i}" in apps:
            i += 1
        final_name = f"{name}_{i}"
    apps[final_name] = app_config
    _commit_apps(path, raw, apps)
    return final_name


@_locked
def set_app_tweaks(path: Path, name: str, tweaks: list[str]) -> None:
    """Replace the `tweaks:` list of an existing app entry.

    Raises ConfigError if the app name doesn't exist on disk. The caller is
    expected to have resolved via cfg.get_app() first; this is the on-disk
    safety net for the rare race where the config was edited externally.

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not
    preserved (same documented limitation as add_app / add_repo).
    """
    raw, apps = _load_apps(path)
    entry = _require_app_mapping(apps, name, path)
    entry["tweaks"] = list(tweaks)
    _commit_apps(path, raw, apps)


@_locked
def remove_app(path: Path, name: str) -> None:
    """Delete an app entry from ``config.yaml``'s ``apps:`` mapping.

    Touches config only — cached IPAs, archived builds, and the sqlite build
    history are deliberately left alone.

    Raises ConfigError if the app doesn't exist on disk.

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not
    preserved (same documented limitation as add_app / add_repo).
    """
    raw, apps = _load_apps(path)
    _require_app(apps, name, path)
    del apps[name]
    _commit_apps(path, raw, apps)


@_locked
def set_app_options(
    path: Path,
    name: str,
    *,
    preferred_version: str | None = None,
    delivery: str | None = None,
    cyan: dict[str, Any] | None = None,
) -> None:
    """Patch an existing app entry's options.

    This is a PATCH, not a replace: only the arguments actually passed are
    written. Passing just ``preferred_version`` leaves ``tweaks``,
    ``lc_bundle_id``, and every cyan key untouched. The
    ``cyan`` mapping merges key-by-key into whatever cyan block already exists.

    Calling it with no options (or with an empty ``cyan={}``) is a no-op that
    doesn't rewrite the file — and performs no validation, so an unknown
    ``name`` will not raise in that case.

    ``cyan={"display_name": ""}`` (or whitespace-only) is deliberately NOT the
    same shape of "empty" as ``preferred_version=""`` above: an empty display
    name has an obvious meaning (unset the custom name, fall back to the
    app's key everywhere) rather than a silent footgun, so it DELETES the
    `display_name` key from the merged cyan block instead of writing an empty
    string. Deleting a key that was already absent is a no-op, not an error.

    Raises ConfigError if the app doesn't exist, ``preferred_version`` is
    empty/whitespace, or ``delivery`` is invalid.

    Note: rewrites config.yaml via yaml.safe_dump — YAML comments are not
    preserved (same documented limitation as add_app / add_repo).
    """
    if preferred_version is None and delivery is None and not cyan:
        return
    if preferred_version is not None:
        preferred_version = preferred_version.strip()
        if not preferred_version:
            raise ConfigError(
                "preferred_version can't be empty",
                hint="use 'latest' to always fetch the newest release, or pin a version like '21.16.2'",
            )
    if delivery is not None and delivery not in ("sideload", "livecontainer"):
        raise ConfigError(
            f"invalid delivery {delivery!r} (must be 'sideload' or 'livecontainer')"
        )
    raw, apps = _load_apps(path)
    entry = _require_app_mapping(apps, name, path)

    if preferred_version is not None:
        entry["preferred_version"] = preferred_version
    if delivery is not None:
        entry["delivery"] = delivery
    if cyan:
        existing = entry.get("cyan")
        merged = dict(existing) if isinstance(existing, dict) else {}
        merged.update(cyan)
        # An emptied display_name unsets it rather than blanking the app: every
        # GUI call site falls back to `app.displayName ?? app.name`, and ""
        # is non-nil, so writing "" would win that coalesce and render blank
        # everywhere with no way to restore "unset" from the GUI. Only act
        # when THIS update explicitly touched display_name — an update to
        # unrelated cyan keys must never reach in and delete an existing,
        # non-empty display name that wasn't part of the request.
        if "display_name" in cyan:
            dn = cyan["display_name"]
            if dn is None or (isinstance(dn, str) and not dn.strip()):
                merged.pop("display_name", None)
        entry["cyan"] = merged

    _commit_apps(path, raw, apps)


@dataclass(frozen=True)
class _Setting:
    """One globally-editable setting: where it lives, and what its value means.

    `kind` and `empty` live here rather than as branches inside `set_settings`
    because both questions have to be answered for *every* setting, and an
    if-chain answers them only for the keys it names — a sixth setting added
    below would inherit whatever the last branch did by falling off the end.
    That is not hypothetical: the next candidate is
    `signing.cert_password_env`, which holds an env-var *name*, and the chain
    this replaced would have `~`-expanded it into a filesystem path without a
    word. Registering a setting now means choosing both answers.
    """

    #: Where the value lives in config.yaml. Nested for `signing.*`.
    path: tuple[str, ...]
    #: How the value is typed: a non-negative count validated by
    #: `_archive_keep`, a filesystem path (`~` expanded on write), or free text
    #: stored exactly as given.
    kind: Literal["count", "path", "text"]
    #: What an empty string means on write: remove the key, or refuse. Not
    #: consulted for `count`, which can never receive one.
    empty: Literal["unset", "reject"]


# Global settings the GUI can write. Declared as data rather than an if-chain
# so the CLI, the reader, the writer and the event's `changed` list cannot
# drift from each other — the same reason APP_OPTION_FIELDS exists.
SETTINGS: dict[str, _Setting] = {
    "default_device_udid": _Setting(("default_device_udid",), "text", "unset"),
    "archive_keep": _Setting(("archive_keep",), "count", "reject"),
    "signing_cert": _Setting(("signing", "cert"), "path", "reject"),
    "signing_profile": _Setting(("signing", "profile"), "path", "reject"),
    "signing_team_id": _Setting(("signing", "team_id"), "text", "reject"),
}


def _dig(raw: dict[str, Any], path: tuple[str, ...]) -> Any:
    """The value at `path`, or None if any level is absent or not a mapping."""
    node: Any = raw
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def read_settings(path: Path) -> dict[str, Any]:
    """The settings `set_settings` can write, read straight out of the YAML.

    Deliberately NOT via `load_config`. That validates the whole document and
    raises when `signing:` is missing a key — which is exactly the state the
    editor exists to repair, so routing the read through it made the pane inert
    on every config that needed it. Reading raw means a half-filled signing
    block reports the fields it has and `None` for the ones it doesn't, which
    is what a repair surface should show.

    Values are coerced to `str` (except the count) because YAML types what it
    finds: `default_device_udid: no` is a bool under YAML 1.1, and an all-digit
    legacy UDID is an int. Both used to reach the GUI as a non-string, fail to
    decode, and be dropped by a `try?` that turns the entire editor into
    "Couldn't read your settings." with nothing said about why.
    """
    raw = _load_raw(path)
    payload: dict[str, Any] = {}
    for key, setting in SETTINGS.items():
        value = _dig(raw, setting.path)
        if setting.kind == "count":
            payload[key] = _readable_count(value)
        else:
            payload[key] = None if value is None else str(value)
    return payload


def _readable_count(value: Any) -> int | None:
    """`archive_keep` as the pane should show it, or None when it is unreadable.

    Deliberately *not* a fallback to `DEFAULT_ARCHIVE_KEEP`. `_archive_keep`
    raises on the same document — on purpose, because the value drives a
    destructive prune — so quietly substituting the default here displayed a
    confident `2` for a config that every build refuses to run. Showing a value
    that is not on disk is the exact failure the `changed` reconciliation was
    built to prevent, so this reports "can't read it" and lets the pane say so.

    Sharing `_archive_keep` rather than re-deriving the rule is the other half:
    the lenient reader and the strict loader now disagree only about what to do
    with a bad value, never about which values are bad.
    """
    try:
        return _archive_keep(value)
    except ConfigError:
        return None


@_locked
def set_settings(path: Path, **values: Any) -> list[str]:
    """Patch global settings in config.yaml. Returns the keys actually written.

    A PATCH like `set_app_options`: only the names passed are touched, and a
    call with nothing to do rewrites no file. The return value is what lets the
    GUI reconcile — it asks for a set of fields and refuses to report success
    for any it requested that didn't come back, the same guard
    `ConfigStore.updateAppOptions` already applies to `app-updated`.

    Empty strings are meaningful and differ by field — `SETTINGS` says which is
    which. `default_device_udid=""` UNSETS it, because "no default, ask the CLI
    to pick" is a real choice a user makes from a picker with a blank row. The
    signing fields refuse it: a build cannot sign without them, and letting the
    GUI blank one from a text field that lost focus would turn a working config
    into a broken one.

    Paths are expanded (`~`) but deliberately NOT required to exist. A user
    pointing at a cert they are about to create, or one on a volume that is not
    mounted right now, is not an error to make at write time — `--check` is
    what reports it, and it already does.
    """
    unknown = sorted(set(values) - set(SETTINGS))
    if unknown:
        raise ConfigError(
            f"unknown setting(s): {', '.join(unknown)}",
            hint=f"known settings: {', '.join(sorted(SETTINGS))}",
        )

    cleaned: dict[str, Any] = {}
    for key, value in values.items():
        if value is None:
            continue
        setting = SETTINGS[key]
        if setting.kind == "count":
            try:
                count = int(value)
            except (TypeError, ValueError):
                raise ConfigError(f"{key} must be a whole number, got {value!r}") from None
            # Through `_archive_keep`, so the write is validated by the same
            # rule the loader applies on the way back in — including its hint
            # that 0 is a real choice. Refusing 0 meant a user already running
            # 0 could nudge the stepper once and never get back to it.
            cleaned[key] = _archive_keep(count)
            continue
        text = str(value).strip()
        if not text:
            if setting.empty == "unset":
                # Written as an explicit None so the writer below removes the
                # key rather than storing "", which `select_device` would then
                # treat as a udid.
                cleaned[key] = None
                continue
            raise ConfigError(
                f"{key} can't be empty",
                hint="a build can't sign without it; run --check to see what's missing",
            )
        cleaned[key] = str(_expand(text)) if setting.kind == "path" else text

    if not cleaned:
        return []

    raw = _load_raw(path)
    for key, value in cleaned.items():
        *parents, leaf = SETTINGS[key].path
        target = raw
        for parent in parents:
            existing = target.get(parent)
            target[parent] = existing if isinstance(existing, dict) else {}
            target = target[parent]
        if value is None:
            target.pop(leaf, None)
        else:
            target[leaf] = value
    _atomic_write_yaml(path, raw)
    return sorted(cleaned)
