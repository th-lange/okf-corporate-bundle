"""Ingest ledger (issue #16): new / unchanged / modified / removed tracking."""

from pathlib import Path

import pytest
import yaml
from conftest import PLAIN, git

from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.ingest.ledger import Ledger


@pytest.fixture()
def workspace(source_repo: Path, tmp_path: Path) -> dict:
    staging = tmp_path / "drafts"
    ledger = tmp_path / "ledger.yaml"
    config = tmp_path / "ingest.yaml"
    config.write_text(
        f"staging_dir: {staging}\n"
        f"ledger: {ledger}\n"
        "sources:\n"
        f"  - name: handbook\n    type: git\n    url: {source_repo}\n"
    )
    return {
        "repo": source_repo,
        "staging": staging,
        "ledger": ledger,
        "config": ["--config", str(config)],
        "note_uri": f"{source_repo}#notes/mrr-tips.md",
        "plain_uri": f"{source_repo}#plain.md",
    }


def entries(ledger_path: Path) -> dict:
    return yaml.safe_load(ledger_path.read_text())["documents"]


def test_first_run_records_every_document(workspace: dict, capsys) -> None:
    assert ingest_main(["run", *workspace["config"]]) == 0
    assert "2 new, 0 modified, 0 unchanged, 0 removed" in capsys.readouterr().out

    docs = entries(workspace["ledger"])
    assert set(docs) == {workspace["note_uri"], workspace["plain_uri"]}
    plain = docs[workspace["plain_uri"]]
    assert plain["source"] == "handbook"
    assert plain["draft"] == "handbook/plain.md"
    assert len(plain["revision"]) == 40
    assert "ingested_at" in plain


def test_status_classifies_all_four_states(workspace: dict, capsys) -> None:
    ingest_main(["run", *workspace["config"]])
    repo = workspace["repo"]
    (repo / "plain.md").write_text(PLAIN + "\nEdited.\n")
    (repo / "fresh.md").write_text("# Fresh\n\nBrand new doc.\n")
    git(repo, "rm", "--quiet", "notes/mrr-tips.md")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "edit, add, remove")
    capsys.readouterr()

    assert ingest_main(["status", *workspace["config"]]) == 0
    out = capsys.readouterr().out
    assert f"MODIFIED   {workspace['plain_uri']}" in out
    assert f"NEW        {repo}#fresh.md" in out
    assert f"REMOVED    {workspace['note_uri']}" in out
    assert "1 new, 1 modified, 0 unchanged, 1 removed" in out
    # status never mutates the ledger
    assert "removed_at" not in entries(workspace["ledger"])[workspace["note_uri"]]


def test_reingest_regenerates_only_modified(workspace: dict, capsys) -> None:
    ingest_main(["run", *workspace["config"]])
    note_draft = workspace["staging"] / "handbook" / "notes" / "mrr-tips.md"
    plain_draft = workspace["staging"] / "handbook" / "plain.md"
    note_before = note_draft.read_bytes()
    plain_before = plain_draft.read_bytes()
    old_revision = entries(workspace["ledger"])[workspace["plain_uri"]]["revision"]

    repo = workspace["repo"]
    (repo / "plain.md").write_text(PLAIN + "\nEdited.\n")
    git(repo, "commit", "--quiet", "-am", "edit plain")
    capsys.readouterr()

    assert ingest_main(["run", *workspace["config"]]) == 0
    assert "0 new, 1 modified, 1 unchanged, 0 removed" in capsys.readouterr().out
    assert note_draft.read_bytes() == note_before  # untouched draft not rewritten
    assert plain_draft.read_bytes() != plain_before
    assert entries(workspace["ledger"])[workspace["plain_uri"]]["revision"] != old_revision


def test_removed_is_flagged_never_deleted(workspace: dict, capsys) -> None:
    ingest_main(["run", *workspace["config"]])
    repo = workspace["repo"]
    git(repo, "rm", "--quiet", "plain.md")
    git(repo, "commit", "--quiet", "-m", "drop plain")
    capsys.readouterr()

    assert ingest_main(["run", *workspace["config"]]) == 0
    captured = capsys.readouterr()
    assert "REMOVED upstream" in captured.err
    assert workspace["plain_uri"] in captured.err

    entry = entries(workspace["ledger"])[workspace["plain_uri"]]
    assert "removed_at" in entry  # flagged in the ledger …
    assert (workspace["staging"] / "handbook" / "plain.md").exists()  # … draft kept

    # restoring the document upstream clears the flag on the next run
    git(repo, "revert", "--quiet", "--no-edit", "HEAD")
    ingest_main(["run", *workspace["config"]])
    assert "removed_at" not in entries(workspace["ledger"])[workspace["plain_uri"]]


def test_ledger_survives_reload(tmp_path: Path) -> None:
    path = tmp_path / "ledger.yaml"
    ledger = Ledger.load(path)
    ledger.record("uri://x", "src", "src/x.md", "rev1")
    ledger.save()

    reloaded = Ledger.load(path)
    assert reloaded.classify("uri://x", "rev1") == "unchanged"
    assert reloaded.classify("uri://x", "rev2") == "modified"
    assert reloaded.classify("uri://y", "rev1") == "new"
