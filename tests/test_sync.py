"""Source-authoritative sync (issue #37): mirror, hash identity, invalidation."""

from pathlib import Path

import pytest
from conftest import PLAIN, git

from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.parser import split_frontmatter


@pytest.fixture()
def ws(source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A knowledge root (its own git repo) with a `kb` bundle, synced from
    the conftest source repo."""
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
    return {"root": root, "kb": kb, "repo": source_repo}


def commits(root: Path) -> int:
    return len(git(root, "log", "--oneline").splitlines())


def test_first_sync_mirrors_sources_and_commits(ws: dict, capsys) -> None:
    assert ingest_main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "2 new" in out and "committed" in out

    plain = ws["kb"] / "plain.md"
    note = ws["kb"] / "notes" / "mrr-tips.md"
    assert plain.is_file() and note.is_file()
    frontmatter, _ = split_frontmatter(plain.read_text())
    assert frontmatter["type"] == "Document"  # defaulted
    assert "#plain.md" in frontmatter["source"]  # provenance stamped
    assert commits(ws["root"]) == 2  # init + one sync commit
    ledger = (ws["root"] / "ingest" / "ledger.yaml").read_text()
    assert "content_sha256" in ledger


def test_unchanged_content_is_noop_despite_revision_churn(ws: dict, capsys) -> None:
    ingest_main(["sync"])
    repo = ws["repo"]
    # two commits that leave the content byte-identical → new per-file
    # revision, same hash
    (repo / "plain.md").write_text(PLAIN + "x")
    git(repo, "commit", "--quiet", "-am", "touch")
    (repo / "plain.md").write_text(PLAIN)
    git(repo, "commit", "--quiet", "-am", "revert content")
    capsys.readouterr()

    assert ingest_main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "0 new" in out and "0 modified" in out and "2 unchanged" in out
    assert "committed" not in out  # nothing changed in the tree
    assert commits(ws["root"]) == 2


def test_modified_replaces_in_place(ws: dict, capsys) -> None:
    ingest_main(["sync"])
    repo = ws["repo"]
    (repo / "plain.md").write_text(PLAIN + "\nNew paragraph.\n")
    git(repo, "commit", "--quiet", "-am", "edit")
    capsys.readouterr()

    assert ingest_main(["sync"]) == 0
    assert "1 modified" in capsys.readouterr().out
    assert "New paragraph." in (ws["kb"] / "plain.md").read_text()
    assert commits(ws["root"]) == 3


def test_rename_preserves_concept_identity(ws: dict, capsys) -> None:
    ingest_main(["sync"])
    repo = ws["repo"]
    git(repo, "mv", "plain.md", "renamed.md")
    git(repo, "commit", "--quiet", "-m", "rename")
    capsys.readouterr()

    assert ingest_main(["sync"]) == 0
    assert "1 renamed" in capsys.readouterr().out
    # the concept keeps its path (identity, id, inbound links) …
    assert (ws["kb"] / "plain.md").is_file()
    assert not (ws["kb"] / "renamed.md").exists()
    # … only provenance follows the source
    frontmatter, _ = split_frontmatter((ws["kb"] / "plain.md").read_text())
    assert "#renamed.md" in frontmatter["source"]


def test_removed_upstream_is_removed_then_resurrects(ws: dict, capsys) -> None:
    ingest_main(["sync"])
    repo = ws["repo"]
    git(repo, "rm", "--quiet", "plain.md")
    git(repo, "commit", "--quiet", "-m", "drop")
    capsys.readouterr()

    assert ingest_main(["sync"]) == 0
    assert "1 removed" in capsys.readouterr().out
    assert not (ws["kb"] / "plain.md").exists()  # invalidated in the tree
    assert commits(ws["root"]) == 3  # deletion is a commit — git is the tombstone

    git(repo, "revert", "--quiet", "--no-edit", "HEAD")
    assert ingest_main(["sync"]) == 0
    assert "1 restored" in capsys.readouterr().out
    assert (ws["kb"] / "plain.md").is_file()  # back, as itself


def test_failed_replacement_keeps_last_known_good(ws: dict, capsys) -> None:
    ingest_main(["sync"])
    good = (ws["kb"] / "plain.md").read_text()
    repo = ws["repo"]
    (repo / "plain.md").write_text("---\n: bad: [yaml\n---\n\nBroken.\n")
    git(repo, "commit", "--quiet", "-am", "corrupt")
    capsys.readouterr()

    assert ingest_main(["sync"]) == 1
    err = capsys.readouterr().err
    assert "QUARANTINED" in err
    assert (ws["kb"] / "plain.md").read_text() == good  # old knowledge survives
    assert (ws["root"] / "ingest" / "quarantine" / "handbook" / "plain.md").is_file()


def test_scope_fields_never_come_from_source(ws: dict) -> None:
    repo = ws["repo"]
    (repo / "evil.md").write_text(
        "---\ntype: Note\ntitle: E\ndescription: d\nscope: [exco]\nscope_default: [exco]\n---\n\nHi.\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "evil")
    assert ingest_main(["sync"]) == 0
    frontmatter, _ = split_frontmatter((ws["kb"] / "evil.md").read_text())
    assert "scope" not in frontmatter and "scope_default" not in frontmatter


def test_deletion_surfaces_dangling_links_in_integrity_report(ws: dict, capsys) -> None:
    repo = ws["repo"]
    (repo / "pointer.md").write_text(
        "---\ntype: Note\ntitle: P\ndescription: d\n---\n\nSee [plain](/plain).\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "add pointer")
    ingest_main(["sync"])
    git(repo, "rm", "--quiet", "plain.md")
    git(repo, "commit", "--quiet", "-m", "drop plain")
    capsys.readouterr()

    assert ingest_main(["sync"]) == 0
    err = capsys.readouterr().err
    assert "INTEGRITY" in err and "dangling link: /plain" in err


def test_sync_requires_knowledge_root(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("OKF_KNOWLEDGE_ROOT", raising=False)
    config = tmp_path / "ingest.yaml"
    config.write_text(
        f"sources:\n  - name: h\n    type: git\n    url: {source_repo}\n    target: kb\n"
    )
    assert ingest_main(["sync", "--config", str(config)]) == 2
    assert "OKF_KNOWLEDGE_ROOT" in capsys.readouterr().err


def test_sync_rejects_missing_target_bundle(ws: dict, capsys) -> None:
    (ws["root"] / "ingest.yaml").write_text(
        f"sources:\n  - name: h\n    type: git\n    url: {ws['repo']}\n    target: nope\n"
    )
    assert ingest_main(["sync"]) == 2
    assert "target bundle" in capsys.readouterr().err
