"""Background sync worker (issue #48): cadence, tick isolation, the overlap
lock, and graceful shutdown — all driven by an injected fake clock, never a
real sleep."""

from __future__ import annotations

import logging
import os
import signal
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import git

from okf_mcp.ingest.cli import _sync, _sync_generation
from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.ingest.scheduler import (
    LockHeld,
    ShutdownRequested,
    SyncLock,
    due_specs,
    is_due,
    lock_path,
    run_tick,
    run_watch,
)
from okf_mcp.ingest.sources import SourceDocument, SourceError
from okf_mcp.ingest.transform import PassthroughTransformer


@dataclass(frozen=True)
class FakeSource:
    """A `Source` double: yields fixed documents, or raises on enumeration."""

    name: str
    docs: tuple[SourceDocument, ...] = field(default_factory=tuple)
    error: Exception | None = None

    def documents(self) -> Iterator[SourceDocument]:
        if self.error is not None:
            raise self.error
        yield from self.docs


def doc(uri: str, rel: str, revision: str) -> SourceDocument:
    return SourceDocument(source_uri=uri, relative_path=rel, revision=revision, content=PLAIN)


PLAIN = "# Just prose\n\nNo frontmatter at all.\n"


@pytest.fixture()
def kroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A knowledge root (its own git repo) with an on-demand `bundle()` factory."""
    root = tmp_path / "knowledge"
    root.mkdir(parents=True)
    git(root, "init", "--quiet")
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)

    def bundle(name: str) -> Path:
        b = root / "bundles" / name
        if not (b / "index.md").exists():
            b.mkdir(parents=True, exist_ok=True)
            (b / "index.md").write_text(
                f"---\ntype: Index\nscope_default: [public]\n---\n# {name}\n"
            )
        return b

    bundle("kb")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "init knowledge repo")
    return {
        "root": root,
        "bundle": bundle,
        "ledger_path": root / "ingest" / "ledger.yaml",
        "quarantine": root / "ingest" / "quarantine",
    }


# (a) due-time computation ---------------------------------------------------


def test_per_source_schedule_honored() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last = {"fast": now - timedelta(minutes=20), "slow": now - timedelta(minutes=20)}
    per_source = {"fast": timedelta(minutes=15), "slow": timedelta(hours=1)}
    assert is_due(
        "fast",
        now,
        last,
        per_source=per_source,
        global_default=None,
        loop_interval=timedelta(hours=1),
    )
    assert not is_due(
        "slow",
        now,
        last,
        per_source=per_source,
        global_default=None,
        loop_interval=timedelta(hours=1),
    )


def test_global_default_applies_without_per_source_entry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last = {"src": now - timedelta(minutes=30)}
    assert is_due(
        "src",
        now,
        last,
        per_source={},
        global_default=timedelta(minutes=15),
        loop_interval=timedelta(hours=1),
    )
    assert not is_due(
        "src",
        now,
        last,
        per_source={},
        global_default=timedelta(hours=2),
        loop_interval=timedelta(minutes=1),
    )


def test_per_source_override_wins_over_global_default() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last = {"src": now - timedelta(minutes=30)}
    # global says "not due yet" (2h), per-source override says "due" (15m)
    assert is_due(
        "src",
        now,
        last,
        per_source={"src": timedelta(minutes=15)},
        global_default=timedelta(hours=2),
        loop_interval=timedelta(hours=1),
    )


def test_unscheduled_source_falls_back_to_loop_interval() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last = {"src": now - timedelta(minutes=10)}
    # no per-source, no global: falls back to the loop's own --interval
    assert not is_due(
        "src", now, last, per_source={}, global_default=None, loop_interval=timedelta(minutes=15)
    )
    assert is_due(
        "src", now, last, per_source={}, global_default=None, loop_interval=timedelta(minutes=5)
    )


def test_never_synced_source_is_due_immediately() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert is_due(
        "new", now, {}, per_source={}, global_default=None, loop_interval=timedelta(hours=1)
    )


def test_due_specs_filters_by_per_source_schedule_field() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fast = FakeSource("fast")
    slow = FakeSource("slow")
    specs = [
        (fast, "passthrough", "kb", timedelta(minutes=15)),
        (slow, "passthrough", "kb", timedelta(hours=2)),
    ]
    last = {"fast": now - timedelta(minutes=20), "slow": now - timedelta(minutes=20)}
    due = due_specs(specs, now, last, global_default=None, loop_interval=timedelta(hours=1))
    assert [s[0].name for s in due] == ["fast"]


# (b) a tick syncs only due sources ------------------------------------------


def test_tick_syncs_only_due_sources(kroot: dict) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    due_source = FakeSource("due-src", docs=(doc("fake://kb/1", "one.md", "r1"),))
    not_due_source = FakeSource("later-src", docs=(doc("fake://kb/2", "two.md", "r1"),))
    specs = [
        (due_source, "passthrough", "kb", None),
        (not_due_source, "passthrough", "kb", None),
    ]
    last_synced = {"later-src": now - timedelta(minutes=1)}  # not due at 1h cadence
    transformers = {"due-src": PassthroughTransformer(), "later-src": PassthroughTransformer()}
    lock = SyncLock(lock_path(kroot["root"]))

    result = run_tick(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers=transformers,
        generations_on=False,
        generations_keep=5,
        global_default=None,
        loop_interval=timedelta(hours=1),
        allow_empty=False,
        now=now,
        last_synced=last_synced,
        lock=lock,
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
    )

    assert result.due == ["due-src"]
    assert result.ran is True
    assert (kroot["root"] / "bundles" / "kb" / "one.md").is_file()
    assert not (kroot["root"] / "bundles" / "kb" / "two.md").is_file()
    assert last_synced["due-src"] == now
    assert last_synced["later-src"] == now - timedelta(minutes=1)  # untouched


def test_tick_with_nothing_due_does_not_sync(kroot: dict, caplog) -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    source = FakeSource("src", docs=(doc("fake://kb/1", "one.md", "r1"),))
    specs = [(source, "passthrough", "kb", None)]
    last_synced = {"src": now}
    lock = SyncLock(lock_path(kroot["root"]))

    with caplog.at_level(logging.INFO):
        result = run_tick(
            root=kroot["root"],
            ledger_path=kroot["ledger_path"],
            quarantine_dir=kroot["quarantine"],
            specs=specs,
            transformers={"src": PassthroughTransformer()},
            generations_on=False,
            generations_keep=5,
            global_default=None,
            loop_interval=timedelta(hours=1),
            allow_empty=False,
            now=now,
            last_synced=last_synced,
            lock=lock,
            sync_fn=_sync,
            sync_generation_fn=_sync_generation,
        )

    assert result.ran is False
    assert result.due == []
    assert not (kroot["root"] / "bundles" / "kb" / "one.md").is_file()
    assert "no sources due" in caplog.text


# (c) a FAILED source never stops the loop and stays scheduled --------------


def test_failed_source_does_not_stop_the_loop_and_stays_scheduled(kroot: dict) -> None:
    healthy = FakeSource("healthy", docs=(doc("fake://kb/1", "one.md", "r1"),))
    broken = FakeSource("broken", error=SourceError("upstream unreachable"))
    specs = [
        (healthy, "passthrough", "kb", timedelta(hours=1)),
        (broken, "passthrough", "kb", timedelta(hours=1)),
    ]
    transformers = {"healthy": PassthroughTransformer(), "broken": PassthroughTransformer()}
    lock = SyncLock(lock_path(kroot["root"]))
    last_synced: dict[str, datetime] = {}
    tick_1 = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)

    result_1 = run_tick(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers=transformers,
        generations_on=False,
        generations_keep=5,
        global_default=None,
        loop_interval=timedelta(hours=1),
        allow_empty=False,
        now=tick_1,
        last_synced=last_synced,
        lock=lock,
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
    )
    assert sorted(result_1.due) == ["broken", "healthy"]
    assert result_1.ran is True  # tick completed despite the FAILED source
    assert last_synced["broken"] == tick_1  # rescheduled, not abandoned
    assert last_synced["healthy"] == tick_1

    # 30 minutes later: neither is due yet (1h cadence) — the loop just
    # keeps ticking, it never exited because "broken" failed.
    tick_2 = tick_1 + timedelta(minutes=30)
    result_2 = run_tick(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers=transformers,
        generations_on=False,
        generations_keep=5,
        global_default=None,
        loop_interval=timedelta(hours=1),
        allow_empty=False,
        now=tick_2,
        last_synced=last_synced,
        lock=lock,
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
    )
    assert result_2.ran is False
    assert result_2.due == []

    # 1h after tick_1: both are due again for their next cadence.
    tick_3 = tick_1 + timedelta(hours=1)
    result_3 = run_tick(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers=transformers,
        generations_on=False,
        generations_keep=5,
        global_default=None,
        loop_interval=timedelta(hours=1),
        allow_empty=False,
        now=tick_3,
        last_synced=last_synced,
        lock=lock,
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
    )
    assert sorted(result_3.due) == ["broken", "healthy"]
    assert result_3.ran is True


# (d) overlap guard -----------------------------------------------------------


def test_tick_skips_when_lock_already_held(kroot: dict, caplog) -> None:
    source = FakeSource("src", docs=(doc("fake://kb/1", "one.md", "r1"),))
    specs = [(source, "passthrough", "kb", None)]
    lock = SyncLock(lock_path(kroot["root"]))
    lock.acquire()  # simulate a concurrent sync already in progress
    try:
        with caplog.at_level(logging.WARNING):
            result = run_tick(
                root=kroot["root"],
                ledger_path=kroot["ledger_path"],
                quarantine_dir=kroot["quarantine"],
                specs=specs,
                transformers={"src": PassthroughTransformer()},
                generations_on=False,
                generations_keep=5,
                global_default=None,
                loop_interval=timedelta(hours=1),
                allow_empty=False,
                now=datetime(2026, 1, 1, tzinfo=UTC),
                last_synced={},
                lock=lock,
                sync_fn=_sync,
                sync_generation_fn=_sync_generation,
            )
    finally:
        lock.release()

    assert result.ran is False
    assert result.skipped_lock is True
    assert "tick skipped" in caplog.text
    assert not (kroot["root"] / "bundles" / "kb" / "one.md").is_file()


def test_plain_sync_fails_fast_when_lock_held(kroot: dict, capsys) -> None:
    kroot["bundle"]("kb")
    (kroot["root"] / "ingest.yaml").write_text("sources: []\n")
    lock = SyncLock(lock_path(kroot["root"]))
    lock.acquire()
    try:
        code = ingest_main(["sync"])
    finally:
        lock.release()

    assert code == 2
    err = capsys.readouterr().err
    assert "already in progress" in err


def test_stale_lock_is_reclaimed(kroot: dict) -> None:
    path = lock_path(kroot["root"])
    path.parent.mkdir(parents=True, exist_ok=True)
    dead_pid = 2**30  # astronomically unlikely to be a live pid
    stamp = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    path.write_text(f"{dead_pid} {stamp}\n", encoding="utf-8")

    lock = SyncLock(path)
    lock.acquire()  # must not raise LockHeld: the dead pid marks it stale
    try:
        held = path.read_text(encoding="utf-8")
        assert str(os.getpid()) in held
    finally:
        lock.release()
    assert not path.exists()


def test_old_lock_reclaimed_by_age_even_with_live_pid(kroot: dict) -> None:
    path = lock_path(kroot["root"])
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = (datetime.now(UTC) - timedelta(hours=7)).isoformat()  # older than stale_after
    path.write_text(f"{os.getpid()} {stamp}\n", encoding="utf-8")  # our own pid: definitely alive

    lock = SyncLock(path, stale_after=timedelta(hours=6))
    lock.acquire()
    lock.release()  # no LockHeld raised: age alone reclaimed it


def test_lock_held_by_live_process_is_not_reclaimed(kroot: dict) -> None:
    path = lock_path(kroot["root"])
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()  # fresh, and our own (live) pid
    path.write_text(f"{os.getpid()} {stamp}\n", encoding="utf-8")

    with pytest.raises(LockHeld):
        SyncLock(path).acquire()


# (e) --once runs a single tick then exits 0 ---------------------------------


def test_watch_once_runs_a_single_tick_then_exits(kroot: dict) -> None:
    source = FakeSource("src", docs=(doc("fake://kb/1", "one.md", "r1"),))
    specs = [(source, "passthrough", "kb", None)]
    calls = {"now": 0, "sleep": 0}
    fixed_now = datetime(2026, 1, 1, tzinfo=UTC)

    def now() -> datetime:
        calls["now"] += 1
        return fixed_now

    def sleep(_seconds: float) -> None:
        calls["sleep"] += 1  # must never be called under --once

    code = run_watch(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers={"src": PassthroughTransformer()},
        generations_on=False,
        generations_keep=5,
        global_default=None,
        interval=timedelta(minutes=5),
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
        once=True,
        now=now,
        sleep=sleep,
        install_signals=False,
    )

    assert code == 0
    assert calls["now"] == 1
    assert calls["sleep"] == 0
    assert (kroot["root"] / "bundles" / "kb" / "one.md").is_file()


def test_cli_watch_once_flag_wired(kroot: dict) -> None:
    kroot["bundle"]("kb")
    source_repo_config = kroot["root"] / "ingest.yaml"
    source_repo_config.write_text("sources: []\n")
    assert ingest_main(["watch", "--once"]) == 0


# (f) graceful shutdown -------------------------------------------------------


def test_shutdown_handler_sets_requested_flag() -> None:
    shutdown = ShutdownRequested()
    assert shutdown.requested is False
    shutdown.handler(signal.SIGTERM, None)
    assert shutdown.requested is True


def test_loop_finishes_in_flight_tick_then_stops_on_shutdown(kroot: dict) -> None:
    source = FakeSource("src", docs=(doc("fake://kb/1", "one.md", "r1"),))
    specs = [(source, "passthrough", "kb", None)]
    shutdown = ShutdownRequested()
    tick_times = iter(
        [datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(0, 24)]
    )  # far more ticks available than the loop should ever take

    def now() -> datetime:
        return next(tick_times)

    ticks_seen = {"count": 0}

    def sleep(_seconds: float) -> None:
        # simulate a SIGTERM arriving while the loop is "asleep" between
        # ticks 1 and 2 — the handler is called directly, per the shared
        # convention for testing signal-driven code without real signals.
        ticks_seen["count"] += 1
        shutdown.handler(signal.SIGTERM, None)

    code = run_watch(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers={"src": PassthroughTransformer()},
        generations_on=False,
        generations_keep=5,
        global_default=timedelta(hours=1),
        interval=timedelta(minutes=5),
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
        once=False,
        now=now,
        sleep=sleep,
        install_signals=False,
        shutdown=shutdown,
    )

    assert code == 0
    assert ticks_seen["count"] == 1  # slept exactly once, then stopped
    assert (kroot["root"] / "bundles" / "kb" / "one.md").is_file()  # the in-flight tick finished


def test_shutdown_install_signals_restores_previous_handlers(kroot: dict) -> None:
    source = FakeSource("src", docs=())
    specs = [(source, "passthrough", "kb", None)]
    previous_int = signal.getsignal(signal.SIGINT)
    previous_term = signal.getsignal(signal.SIGTERM)

    code = run_watch(
        root=kroot["root"],
        ledger_path=kroot["ledger_path"],
        quarantine_dir=kroot["quarantine"],
        specs=specs,
        transformers={"src": PassthroughTransformer()},
        generations_on=False,
        generations_keep=5,
        global_default=None,
        interval=timedelta(minutes=5),
        sync_fn=_sync,
        sync_generation_fn=_sync_generation,
        once=True,
        now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        sleep=lambda _s: None,
        install_signals=True,
    )

    assert code == 0
    assert signal.getsignal(signal.SIGINT) == previous_int
    assert signal.getsignal(signal.SIGTERM) == previous_term
