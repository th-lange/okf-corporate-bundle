"""Ingest tracer bullet (issue #15): git source → provenance-stamped drafts."""

from datetime import datetime
from pathlib import Path

from conftest import PLAIN, git

from okf_mcp.ingest import GitSource, PassthroughTransformer, ingest
from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.ingest.sources import SourceDocument
from okf_mcp.parser import parse_document, split_frontmatter
from okf_mcp.validator import _check_document


def test_git_source_yields_per_document_revisions(source_repo: Path) -> None:
    source = GitSource(name="handbook", url=str(source_repo))
    docs = {d.relative_path: d for d in source.documents()}
    assert set(docs) == {"notes/mrr-tips.md", "plain.md"}
    first_rev = docs["plain.md"].revision
    assert len(first_rev) == 40  # a commit hash

    # touching one file must not change the other file's revision
    (source_repo / "plain.md").write_text(PLAIN + "\nMore prose.\n")
    git(source_repo, "commit", "--quiet", "-am", "edit plain")
    docs2 = {d.relative_path: d for d in source.documents()}
    assert docs2["plain.md"].revision != first_rev
    assert docs2["notes/mrr-tips.md"].revision == docs["notes/mrr-tips.md"].revision


def test_drafts_carry_provenance_and_pass_validation(
    source_repo: Path, tmp_path: Path
) -> None:
    staging = tmp_path / "drafts"
    source = GitSource(name="handbook", url=str(source_repo))
    drafts = ingest([source], staging)
    assert len(drafts) == 2

    by_name = {d.path.name: d for d in drafts}
    note = by_name["mrr-tips.md"]
    assert note.path == staging / "handbook" / "notes" / "mrr-tips.md"

    frontmatter, body = split_frontmatter(note.path.read_text())
    assert frontmatter["type"] == "Note"  # existing frontmatter preserved
    assert frontmatter["title"] == "MRR tips"
    assert frontmatter["source"] == f"{source_repo}#notes/mrr-tips.md"
    assert frontmatter["source_rev"] == note.revision
    datetime.fromisoformat(frontmatter["ingested_at"])  # valid timestamp
    assert "Watch the grain." in body

    plain_fm, _ = split_frontmatter(by_name["plain.md"].path.read_text())
    assert plain_fm["type"] == "Document"  # defaulted, so drafts validate

    for draft in drafts:
        doc = parse_document(staging, draft.path)
        assert _check_document(doc, str(draft.path)) == []


def test_transformer_seam_is_pluggable(source_repo: Path, tmp_path: Path) -> None:
    class ShoutingTransformer:
        def transform(self, doc: SourceDocument) -> str:
            return PassthroughTransformer().transform(doc).replace("prose", "PROSE")

    drafts = ingest(
        [GitSource(name="handbook", url=str(source_repo))],
        tmp_path / "drafts",
        transformer=ShoutingTransformer(),
    )
    plain = next(d for d in drafts if d.path.name == "plain.md")
    assert "PROSE" in plain.path.read_text()


def test_cli_end_to_end(source_repo: Path, tmp_path: Path, capsys) -> None:
    staging = tmp_path / "drafts"
    config = tmp_path / "ingest.yaml"
    config.write_text(
        f"staging_dir: {staging}\n"
        "sources:\n"
        f"  - name: handbook\n    type: git\n    url: {source_repo}\n"
    )
    assert ingest_main(["--config", str(config)]) == 0
    assert (staging / "handbook" / "plain.md").exists()
    assert "2 draft(s)" in capsys.readouterr().out


def test_cli_rejects_unknown_source_type(tmp_path: Path, capsys) -> None:
    config = tmp_path / "ingest.yaml"
    config.write_text("sources:\n  - name: x\n    type: carrier-pigeon\n")
    assert ingest_main(["--config", str(config)]) == 2
    assert "unknown source type" in capsys.readouterr().err
