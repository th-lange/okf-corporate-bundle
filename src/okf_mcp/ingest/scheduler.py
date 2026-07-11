"""Background sync scheduling (issue #48): config-driven per-source cadence,
a long-running worker loop, and an overlap-guarding lockfile — built on top
of #46 (per-source isolation) and #47 (generational atomic publish) so a
scheduled run never disturbs a live session and one source's failure never
stalls the schedule.

Cadence grammar: `Nm|Nh|Nd` (minutes/hours/days), e.g. `15m`, `1h`, `1d`.
Cron syntax is deliberately out of scope for this demo: the two deployment
recipes documented in docs/usage.md (a systemd timer calling `okf-ingest
watch --once`, or an `okf-ingest watch` sidecar container) only need "every
N minutes/hours/days" — pulling in a cron-expression parser plus timezone
and calendar handling (weekdays, month boundaries, DST) buys nothing for a
background poller and is real complexity to maintain and test.

Due-time semantics: `effective_interval(source)` = the source's own
`schedule:` override, else the config's global `schedule:` default, else
the watch loop's own `--interval`. That last fallback is deliberate: a
source with *no* schedule anywhere still gets synced, at the loop's own
tick cadence — the "worker's full-run interval" — rather than never running
under `watch` at all (it always still runs on a plain, manually-invoked
`okf-ingest sync`, which has no notion of cadence). A source with no prior
recorded run is due on the very first tick that sees it.

Overlap guard: a single lockfile at `<root>/ingest/sync.lock`, taken by
both `okf-ingest sync` and every `watch` tick, using
`os.open(..., O_CREAT | O_EXCL)` rather than `fcntl.flock`:

  1. Advisory `flock` is unreliable on network filesystems, which are a
     realistic knowledge-root mount for a container sidecar deployment;
     create-exclusive is a plain POSIX filesystem primitive that behaves
     the same everywhere.
  2. The two processes racing the same root (`sync` and `watch`) are
     independent — neither can hold a shared in-memory lock or a
     dup'd file descriptor across the other. A plain file stamped
     `<pid> <acquired_at>` is inspectable with `ls`/`cat`, and a stale lock
     (holder's pid is dead, or the lock is simply older than
     `stale_after`) can be reclaimed deterministically instead of relying
     on the kernel to notice a crashed holder and release its `flock`.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from okf_mcp.ingest.ledger import Ledger

if TYPE_CHECKING:
    from okf_mcp.ingest.cli import SourceSpec
    from okf_mcp.ingest.transform import Transformer

logger = logging.getLogger(__name__)

_UNITS_MINUTES = {"m": 1, "h": 60, "d": 60 * 24}
_INTERVAL_RE = re.compile(r"(\d+)([mhd])")

STATE_FILENAME = "schedule_state.json"
LOCK_FILENAME = "sync.lock"
DEFAULT_STALE_AFTER = timedelta(hours=6)


class ScheduleConfigError(ValueError):
    """Raised for a malformed `schedule:` interval, from config or the CLI."""


class LockHeld(RuntimeError):
    """Raised when the sync lock is already held by a live process."""


def parse_interval(value: str) -> timedelta:
    """Parse `Nm|Nh|Nd` into a `timedelta`. See the module docstring for why
    cron syntax isn't supported."""
    match = _INTERVAL_RE.fullmatch(value.strip())
    if not match:
        raise ScheduleConfigError(
            f"invalid schedule interval {value!r}; expected Nm, Nh, or Nd "
            "(e.g. 15m, 1h, 1d) — cron syntax isn't supported by this demo scheduler"
        )
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(minutes=amount * _UNITS_MINUTES[unit])


def global_schedule_from_file(config_path: Path) -> timedelta | None:
    """The top-level `schedule:` default, or None when absent/malformed.

    Read independently of `cli.load_config` (which doesn't surface this
    optional block), best-effort like the other optional-block accessors
    (`embeddings_config_from_file`, `generations_enabled_from_file`): a
    missing file, bad YAML, or a malformed interval just means "no global
    default", never a crash.
    """
    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    value = raw.get("schedule")
    if not isinstance(value, str):
        return None
    try:
        return parse_interval(value)
    except ScheduleConfigError:
        return None


def effective_interval(
    name: str,
    *,
    per_source: dict[str, timedelta],
    global_default: timedelta | None,
    loop_interval: timedelta,
) -> timedelta:
    """Per-source override > global default > the watch loop's own tick
    interval (the fallback "full-run interval" for an unscheduled source)."""
    return per_source.get(name) or global_default or loop_interval


def is_due(
    name: str,
    now: datetime,
    last_synced: dict[str, datetime],
    *,
    per_source: dict[str, timedelta],
    global_default: timedelta | None,
    loop_interval: timedelta,
) -> bool:
    """True when `name` has never been synced by the watcher, or its
    effective interval has elapsed since it last was."""
    last = last_synced.get(name)
    if last is None:
        return True
    interval = effective_interval(
        name, per_source=per_source, global_default=global_default, loop_interval=loop_interval
    )
    return now >= last + interval


def due_specs(
    specs: list[SourceSpec],
    now: datetime,
    last_synced: dict[str, datetime],
    *,
    global_default: timedelta | None,
    loop_interval: timedelta,
) -> list[SourceSpec]:
    """The subset of `specs` due for a sync this tick. Per-source `schedule`
    overrides travel with each spec's 4th element."""
    per_source = {source.name: schedule for source, _, _, schedule in specs if schedule is not None}
    return [
        spec
        for spec in specs
        if is_due(
            spec[0].name,
            now,
            last_synced,
            per_source=per_source,
            global_default=global_default,
            loop_interval=loop_interval,
        )
    ]


# --- persisted "last synced at" state, per source -------------------------


def state_path(root: Path) -> Path:
    return root / "ingest" / STATE_FILENAME


def load_state(path: Path) -> dict[str, datetime]:
    """Best-effort load: a missing or corrupt state file just means every
    source looks never-synced (due immediately), never a crash."""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    state: dict[str, datetime] = {}
    for name, stamp in raw.items():
        try:
            state[name] = datetime.fromisoformat(stamp)
        except (TypeError, ValueError):
            continue
    return state


def save_state(path: Path, state: dict[str, datetime]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {name: stamp.isoformat() for name, stamp in state.items()}
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


# --- overlap guard ----------------------------------------------------------


def lock_path(root: Path) -> Path:
    return root / "ingest" / LOCK_FILENAME


@dataclass
class SyncLock:
    """Overlap guard for one knowledge root. See the module docstring for
    why this uses O_EXCL create-exclusive rather than `fcntl.flock`."""

    path: Path
    stale_after: timedelta = DEFAULT_STALE_AFTER

    def _read(self) -> tuple[int, datetime] | None:
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        pid_str, _, stamp_str = raw.partition(" ")
        try:
            return int(pid_str), datetime.fromisoformat(stamp_str)
        except ValueError:
            return None

    def _is_stale(self) -> bool:
        held = self._read()
        if held is None:
            return True  # unreadable/corrupt lockfile: safe to reclaim
        pid, acquired_at = held
        if datetime.now(UTC) - acquired_at > self.stale_after:
            return True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True  # holder's pid is dead
        except PermissionError:
            return False  # alive, owned by another user
        return False

    def acquire(self) -> None:
        """Raises `LockHeld` if another live process holds the lock. A
        stale lock is reclaimed automatically, once, before giving up."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        reclaimed = False
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if not reclaimed and self._is_stale():
                    self.path.unlink(missing_ok=True)
                    reclaimed = True
                    continue
                raise LockHeld(
                    f"a sync is already in progress ({self.path}); if the holder "
                    f"crashed, the lock is reclaimed automatically once it is older "
                    f"than {self.stale_after}"
                ) from None
            else:
                with os.fdopen(fd, "w") as fh:
                    fh.write(f"{os.getpid()} {datetime.now(UTC).isoformat()}\n")
                return

    def release(self) -> None:
        self.path.unlink(missing_ok=True)

    @contextmanager
    def held(self):
        self.acquire()
        try:
            yield
        finally:
            self.release()


# --- graceful shutdown -------------------------------------------------------


class ShutdownRequested:
    """Set by SIGINT/SIGTERM; the watch loop checks it only between ticks,
    so the in-flight tick always finishes before the process exits(0)."""

    def __init__(self) -> None:
        self.requested = False

    def handler(self, signum: int, frame: object) -> None:
        del frame
        logger.info(
            "watch: received signal %s — finishing the in-flight tick, then exiting", signum
        )
        self.requested = True

    @contextmanager
    def installed(self, signums: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM)):
        previous = {sig: signal.signal(sig, self.handler) for sig in signums}
        try:
            yield self
        finally:
            for sig, prior_handler in previous.items():
                signal.signal(sig, prior_handler)


# --- one tick, and the loop around it ---------------------------------------


@dataclass
class TickResult:
    due: list[str]
    ran: bool
    exit_code: int | None
    skipped_lock: bool = False


def run_tick(
    *,
    root: Path,
    ledger_path: Path,
    quarantine_dir: Path,
    specs: list[SourceSpec],
    transformers: dict[str, Transformer],
    generations_on: bool,
    generations_keep: int,
    global_default: timedelta | None,
    loop_interval: timedelta,
    allow_empty: bool,
    now: datetime,
    last_synced: dict[str, datetime],
    lock: SyncLock,
    sync_fn: Callable[..., int],
    sync_generation_fn: Callable[..., int],
    dry_run: bool = False,
) -> TickResult:
    """Sync exactly the sources due at `now`, reusing `sync_fn`/
    `sync_generation_fn` (`cli._sync` / `cli._sync_generation`) unmodified —
    both already take an arbitrary `specs` subset, so restricting to the due
    sources needs no separate sync path. A held lock is reported, not
    raised past this call: the caller logs and moves on to the next tick."""
    due = due_specs(
        specs, now, last_synced, global_default=global_default, loop_interval=loop_interval
    )
    if not due:
        logger.info("watch: no sources due at %s", now.isoformat())
        return TickResult(due=[], ran=False, exit_code=None)

    names = [spec[0].name for spec in due]
    if dry_run:
        logger.info("watch: dry-run — due this tick: %s", ", ".join(names))
        return TickResult(due=names, ran=False, exit_code=None)

    try:
        with lock.held():
            if generations_on:
                exit_code = sync_generation_fn(
                    root,
                    ledger_path,
                    due,
                    transformers,
                    quarantine_dir,
                    since=None,
                    allow_empty=allow_empty,
                    keep=generations_keep,
                )
            else:
                ledger = Ledger.load(ledger_path)
                exit_code = sync_fn(
                    root,
                    ledger,
                    ledger_path,
                    due,
                    transformers,
                    quarantine_dir,
                    since=None,
                    allow_empty=allow_empty,
                )
    except LockHeld as exc:
        logger.warning("watch: tick skipped — %s", exc)
        return TickResult(due=names, ran=False, exit_code=None, skipped_lock=True)

    # Every attempted source — OK or FAILED — is rescheduled for its next
    # cadence; a FAILED source must not be retried every tick until fixed.
    for name in names:
        last_synced[name] = now
    return TickResult(due=names, ran=True, exit_code=exit_code)


def run_watch(
    *,
    root: Path,
    ledger_path: Path,
    quarantine_dir: Path,
    specs: list[SourceSpec],
    transformers: dict[str, Transformer],
    generations_on: bool,
    generations_keep: int,
    global_default: timedelta | None,
    interval: timedelta,
    sync_fn: Callable[..., int],
    sync_generation_fn: Callable[..., int],
    once: bool = False,
    dry_run: bool = False,
    allow_empty: bool = False,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    install_signals: bool = True,
    shutdown: ShutdownRequested | None = None,
) -> int:
    """The long-running (or `--once`) worker loop. `now`/`sleep` are
    injectable so tests never really sleep or depend on wall-clock time.

    Each tick: compute due sources, sync just that subset (lock-guarded),
    persist "last synced at" for every source attempted, then sleep until
    the next tick. `--once` (what a systemd timer invokes) runs exactly one
    tick and returns. SIGINT/SIGTERM (when `install_signals`) request a
    graceful stop that takes effect only between ticks — see
    `ShutdownRequested`. Always returns 0: a failing source is isolated
    (#46) and reported, never fatal to the loop."""
    lock = SyncLock(lock_path(root))
    path = state_path(root)
    last_synced = load_state(path)
    shutdown = shutdown or ShutdownRequested()

    def _tick() -> TickResult:
        result = run_tick(
            root=root,
            ledger_path=ledger_path,
            quarantine_dir=quarantine_dir,
            specs=specs,
            transformers=transformers,
            generations_on=generations_on,
            generations_keep=generations_keep,
            global_default=global_default,
            loop_interval=interval,
            allow_empty=allow_empty,
            now=now(),
            last_synced=last_synced,
            lock=lock,
            sync_fn=sync_fn,
            sync_generation_fn=sync_generation_fn,
            dry_run=dry_run,
        )
        if result.ran:
            save_state(path, last_synced)
        return result

    def _loop() -> int:
        while True:
            _tick()
            if once or shutdown.requested:
                return 0
            sleep(interval.total_seconds())
            if shutdown.requested:
                return 0

    if install_signals:
        with shutdown.installed():
            return _loop()
    return _loop()
