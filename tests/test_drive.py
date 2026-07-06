"""Google Drive connector (issue #17) — mocked API, no network."""

from pathlib import Path

import pytest

from okf_mcp.ingest import ingest
from okf_mcp.ingest.drive import GOOGLE_DOC_MIME, DriveSource
from okf_mcp.ingest.ledger import Ledger
from okf_mcp.ingest.sources import SourceError
from okf_mcp.parser import split_frontmatter

DOC = {
    "id": "doc-1",
    "name": "Pricing FAQ",
    "mimeType": GOOGLE_DOC_MIME,
    "headRevisionId": "rev-doc-7",
}
NOTE_MD = {
    "id": "file-2",
    "name": "runbook-notes.md",
    "mimeType": "text/markdown",
    "modifiedTime": "2026-07-01T10:00:00Z",  # no headRevisionId → fallback
}
IMAGE = {"id": "img-3", "name": "diagram.png", "mimeType": "image/png"}


class FakeDriveApi:
    def __init__(self) -> None:
        self.files = [dict(DOC), dict(NOTE_MD), dict(IMAGE)]

    def list_folder(self, folder_id: str) -> list[dict]:
        assert folder_id == "folder-x"
        return [dict(f) for f in self.files]

    def export(self, file_id: str, mime_type: str) -> str:
        assert (file_id, mime_type) == ("doc-1", "text/markdown")
        # content varies with the revision so hash-based change detection
        # sees an edit, not just revision churn
        rev = self.files[0]["headRevisionId"]
        return f"# Pricing FAQ\n\nExported from a Google Doc. ({rev})\n"

    def download(self, file_id: str) -> str:
        assert file_id == "file-2"
        return "# Runbook notes\n\nPlain markdown file.\n"


@pytest.fixture()
def source() -> DriveSource:
    return DriveSource(name="drive-docs", folder_id="folder-x", api=FakeDriveApi())


def test_enumerates_folder_with_stable_revisions(source: DriveSource) -> None:
    docs = {d.relative_path: d for d in source.documents()}
    # the png is skipped; the Google Doc gains a .md suffix
    assert set(docs) == {"Pricing FAQ.md", "runbook-notes.md"}

    exported = docs["Pricing FAQ.md"]
    assert exported.source_uri == "gdrive://doc-1"
    assert exported.revision == "rev-doc-7"  # headRevisionId preferred
    assert "Exported from a Google Doc" in exported.content

    plain = docs["runbook-notes.md"]
    assert plain.revision == "2026-07-01T10:00:00Z"  # modifiedTime fallback


def test_drafts_and_provenance_via_unchanged_core_loop(
    source: DriveSource, tmp_path: Path
) -> None:
    drafts = ingest([source], tmp_path / "drafts")
    assert len(drafts) == 2
    frontmatter, _ = split_frontmatter(
        (tmp_path / "drafts" / "drive-docs" / "Pricing FAQ.md").read_text()
    )
    assert frontmatter["source"] == "gdrive://doc-1"
    assert frontmatter["source_rev"] == "rev-doc-7"
    assert frontmatter["type"] == "Document"


def test_ledger_states_work_for_drive_documents(source: DriveSource, tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.yaml")
    for doc in source.documents():
        assert ledger.classify(doc.source_uri, doc.revision, doc.content_sha256) == "new"
        ledger.record(
            doc.source_uri, source.name, doc.relative_path, doc.revision, doc.content_sha256
        )

    api = source.api
    assert isinstance(api, FakeDriveApi)
    api.files[0]["headRevisionId"] = "rev-doc-8"  # edited upstream (content changes too)
    del api.files[1]  # markdown file removed upstream

    current = {d.source_uri: (d.revision, d.content_sha256) for d in source.documents()}
    assert dict(ledger.status(current)) == {
        "gdrive://doc-1": "modified",
        "gdrive://file-2": "removed",
    }


def test_missing_credentials_fail_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GOOGLE_DRIVE_TOKEN", raising=False)
    source = DriveSource(name="drive-docs", folder_id="folder-x")  # real client path
    with pytest.raises(SourceError, match="GOOGLE_DRIVE_TOKEN"):
        list(source.documents())


def test_file_without_revision_is_an_error(tmp_path: Path) -> None:
    class BrokenApi(FakeDriveApi):
        def list_folder(self, folder_id: str) -> list[dict]:
            return [{"id": "x", "name": "odd.md", "mimeType": "text/markdown"}]

        def download(self, file_id: str) -> str:
            return "content"

    source = DriveSource(name="d", folder_id="folder-x", api=BrokenApi())
    with pytest.raises(SourceError, match="no usable revision"):
        list(source.documents())
