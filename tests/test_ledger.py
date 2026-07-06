"""Sync ledger (issues #16/#37): hash-keyed classification and identity."""

from pathlib import Path

from okf_mcp.ingest.ledger import Ledger


def make(tmp_path: Path) -> Ledger:
    ledger = Ledger.load(tmp_path / "ledger.yaml")
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", "sha-a")
    return ledger


def test_classification_rolls_on_hashes(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    assert ledger.classify("uri://a", "rev1", "sha-a") == "unchanged"
    # revision churn with identical content is a no-op
    assert ledger.classify("uri://a", "rev2", "sha-a") == "unchanged"
    assert ledger.classify("uri://a", "rev2", "sha-b") == "modified"
    assert ledger.classify("uri://b", "rev1", "sha-a") == "new"


def test_match_and_adopt_preserve_the_concept(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    # rename: uri://a vanished this run, uri://b appeared with the same hash
    assert ledger.match_by_sha("sha-a", current_uris={"uri://b"}) == "uri://a"
    assert ledger.match_by_sha("sha-x", current_uris={"uri://b"}) is None

    entry, was_removed = ledger.adopt("uri://a", "uri://b", "rev2")
    assert entry["concept"] == "bundles/kb/a.md"  # identity carried over
    assert was_removed is False
    assert ledger.entry("uri://a") is None
    assert ledger.entry("uri://b")["revision"] == "rev2"


def test_adopt_after_removal_reports_restoration(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    assert ledger.sweep_removed(set()) == ["uri://a"]
    assert "removed_at" in ledger.entry("uri://a")

    entry, was_removed = ledger.adopt("uri://a", "uri://c", "rev3")
    assert was_removed is True
    assert "removed_at" not in entry


def test_sweep_flags_each_document_once(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    ledger.record("uri://b", "src", "bundles/kb/b.md", "rev1", "sha-b")
    assert ledger.sweep_removed({"uri://a"}) == ["uri://b"]
    assert ledger.sweep_removed({"uri://a"}) == []  # already flagged


def test_mark_seen_refreshes_revision_and_clears_flag(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    ledger.sweep_removed(set())
    ledger.mark_seen("uri://a", revision="rev9")
    entry = ledger.entry("uri://a")
    assert entry["revision"] == "rev9"
    assert "removed_at" not in entry


def test_status_covers_all_states_without_mutating(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    current = {
        "uri://a": ("rev5", "sha-a"),  # unchanged (hash wins over revision)
        "uri://new": ("r", "s"),
    }
    assert dict(ledger.status(current)) == {"uri://a": "unchanged", "uri://new": "new"}
    assert dict(ledger.status({})) == {"uri://a": "removed"}
    assert "removed_at" not in ledger.entry("uri://a")  # status never mutates


def test_ledger_survives_reload(tmp_path: Path) -> None:
    ledger = make(tmp_path)
    ledger.save()
    reloaded = Ledger.load(tmp_path / "ledger.yaml")
    assert reloaded.classify("uri://a", "rev1", "sha-a") == "unchanged"
    assert reloaded.entry("uri://a")["concept"] == "bundles/kb/a.md"
