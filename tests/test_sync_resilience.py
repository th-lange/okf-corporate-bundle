"""Sync resilience (issue #46): per-source isolation, scoped sweep, --since."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from conftest import PLAIN, git

from okf_mcp.ingest import cli
from okf_mcp.ingest.cli import _sync
from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.ingest.ledger import Ledger
from okf_mcp.ingest.sources import SourceDocument, SourceError, SourceUnconfiguredError
from okf_mcp.ingest.transform import PassthroughTransformer


@dataclass(frozen=True)
class FakeSource:
    """A `Source` double: either yields a fixed set of documents, or raises
    on enumeration — whichever a test needs, without touching git/network."""

    name: str
    docs: tuple[SourceDocument, ...] = field(default_factory=tuple)
    error: Exception | None = None

    def documents(self) -> Iterator[SourceDocument]:
        if self.error is not None:
            raise self.error
        yield from self.docs


def doc(uri: str, rel: str, revision: str, content: str = PLAIN) -> SourceDocument:
    return SourceDocument(source_uri=uri, relative_path=rel, revision=revision, content=content)


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


def run(kroot: dict, specs: list, **kwargs) -> tuple[int, Ledger]:
    ledger = Ledger.load(kroot["ledger_path"])
    transformers = {source.name: PassthroughTransformer() for source, *_ in specs}
    code = _sync(
        kroot["root"], ledger, kroot["ledger_path"], specs, transformers, kroot["quarantine"], **kwargs
    )
    return code, ledger


def test_failed_source_does_not_block_healthy_source(kroot: dict, capsys) -> None:
    kroot["bundle"]("a")
    kroot["bundle"]("b")
    healthy = FakeSource("healthy", docs=(doc("fake://healthy/1", "one.md", "r1"),))
    broken = FakeSource("broken", error=SourceError("upstream unreachable"))

    # a prior entry for "broken" — it must survive the run untouched
    seed = Ledger.load(kroot["ledger_path"])
    seed.record("fake://broken/old", "broken", "bundles/b/old.md", "r0", "deadbeef")
    seed.save()

    specs = [(healthy, "passthrough", "a", None), (broken, "passthrough", "b", None)]
    code, ledger = run(kroot, specs)
    out = capsys.readouterr().out

    assert code == 1  # non-zero: a source FAILED
    assert "FAILED" in out and "broken" in out and "upstream unreachable" in out
    assert "OK" in out and "healthy" in out
    assert (kroot["root"] / "bundles" / "a" / "one.md").is_file()  # healthy source published
    assert "removed_at" not in ledger.entry("fake://broken/old")  # exempt from sweep


def test_unconfigured_source_is_skipped_not_failed(kroot: dict, capsys) -> None:
    unconfigured = FakeSource("nocreds", error=SourceUnconfiguredError("missing TOKEN"))
    code, _ = run(kroot, [(unconfigured, "passthrough", "kb", None)])
    out = capsys.readouterr().out

    assert code == 0  # SKIPPED alone never fails the run
    assert "SKIPPED" in out and "nocreds" in out and "missing TOKEN" in out
    assert "FAILED" not in out


def test_empty_source_with_prior_entries_warns_and_skips_sweep(kroot: dict, capsys) -> None:
    kroot["bundle"]("c")
    seed = Ledger.load(kroot["ledger_path"])
    seed.record("fake://c/1", "c-src", "bundles/c/one.md", "r1", "sha1")
    seed.save()

    empty_source = FakeSource("c-src", docs=())
    specs = [(empty_source, "passthrough", "c", None)]

    code, ledger = run(kroot, specs)
    err = capsys.readouterr().err
    assert code == 0
    assert "WARNING" in err and "c-src" in err
    assert "removed_at" not in ledger.entry("fake://c/1")

    code, ledger = run(kroot, specs, allow_empty=True)
    assert code == 0
    assert "removed_at" in ledger.entry("fake://c/1")  # --allow-empty overrides the guard


def test_sweep_is_scoped_per_source(kroot: dict) -> None:
    kroot["bundle"]("a")
    kroot["bundle"]("b")
    doc_a1 = doc("fake://a/1", "one.md", "ra1")
    doc_a2 = doc("fake://a/2", "two.md", "ra2")
    doc_b1 = doc("fake://b/1", "one.md", "rb1")

    source_a = FakeSource("src-a", docs=(doc_a1, doc_a2))
    source_b = FakeSource("src-b", docs=(doc_b1,))
    run(kroot, [(source_a, "passthrough", "a", None), (source_b, "passthrough", "b", None)])

    # upstream removes doc_a1 only; source b is untouched
    source_a2 = FakeSource("src-a", docs=(doc_a2,))
    code, ledger = run(
        kroot, [(source_a2, "passthrough", "a", None), (source_b, "passthrough", "b", None)]
    )

    assert code == 0
    assert "removed_at" in ledger.entry("fake://a/1")
    assert "removed_at" not in ledger.entry("fake://a/2")
    assert "removed_at" not in ledger.entry("fake://b/1")


def test_since_defers_fresh_entries_and_processes_stale_ones(kroot: dict) -> None:
    uri = "fake://kb/1"
    source = FakeSource("since-src", docs=(doc(uri, "one.md", "r1"),))
    seed = Ledger.load(kroot["ledger_path"])
    seed.record(uri, "since-src", "bundles/kb/one.md", "r0", "placeholder-sha")
    seed.entry(uri)["synced_at"] = datetime.now(UTC).isoformat(timespec="seconds")
    seed.save()

    code, ledger = run(kroot, [(source, "passthrough", "kb", None)], since=timedelta(days=1))
    assert code == 0
    assert ledger.entry(uri)["content_sha256"] == "placeholder-sha"  # deferred: untouched

    stale = datetime.now(UTC) - timedelta(days=5)
    fresh_seed = Ledger.load(kroot["ledger_path"])
    fresh_seed.entry(uri)["synced_at"] = stale.isoformat(timespec="seconds")
    fresh_seed.save()

    code, ledger = run(kroot, [(source, "passthrough", "kb", None)], since=timedelta(days=1))
    assert code == 0
    assert ledger.entry(uri)["content_sha256"] != "placeholder-sha"  # stale: processed


def test_mark_seen_refreshes_synced_at(kroot: dict) -> None:
    uri = "fake://kb/unchanged"
    source = FakeSource("u-src", docs=(doc(uri, "one.md", "r1"),))
    run(kroot, [(source, "passthrough", "kb", None)])
    seed = Ledger.load(kroot["ledger_path"])
    old_synced_at = seed.entry(uri)["synced_at"]
    seed.entry(uri)["synced_at"] = "2000-01-01T00:00:00+00:00"
    seed.save()

    # same content, new revision -> "unchanged" path -> mark_seen -> refreshed synced_at
    same_content_new_rev = FakeSource("u-src", docs=(doc(uri, "one.md", "r2"),))
    run(kroot, [(same_content_new_rev, "passthrough", "kb", None)])
    ledger = Ledger.load(kroot["ledger_path"])
    assert ledger.entry(uri)["synced_at"] != "2000-01-01T00:00:00+00:00"
    assert ledger.entry(uri)["synced_at"] >= old_synced_at


def test_since_malformed_value_rejected() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        cli._parse_since("bogus")


def test_since_malformed_value_rejected_by_cli(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "knowledge"
    kb = root / "bundles" / "kb"
    kb.mkdir(parents=True)
    (kb / "index.md").write_text("---\ntype: Index\nscope_default: [public]\n---\n# KB\n")
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "init knowledge repo")
    (root / "ingest.yaml").write_text(
        "sources:\n"
        f"  - name: handbook\n    type: git\n    url: {source_repo}\n    target: kb\n"
    )
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)

    with pytest.raises(SystemExit):
        ingest_main(["sync", "--since", "bogus"])
