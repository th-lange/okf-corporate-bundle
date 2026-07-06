"""AWS S3 connector (issue #18) — mocked API, no network, no SDK needed."""

import sys
from pathlib import Path

import pytest

from okf_mcp.ingest import ingest
from okf_mcp.ingest.ledger import Ledger
from okf_mcp.ingest.s3 import Boto3S3Api, S3Source
from okf_mcp.ingest.sources import SourceError
from okf_mcp.parser import split_frontmatter


class FakeS3Api:
    def __init__(self) -> None:
        self.objects = {
            "kb/runbooks/failover.md": ('"etag-aaa"', "# Failover\n\nSteps.\n"),
            "kb/notes.md": ('"etag-bbb"', "# Notes\n\nPlain notes.\n"),
            "kb/diagram.png": ('"etag-ccc"', "\x89PNG"),
        }

    def list_objects(self, bucket: str, prefix: str) -> list[dict]:
        assert bucket == "acme-kb"
        return [
            {"key": key, "etag": etag}
            for key, (etag, _) in sorted(self.objects.items())
            if key.startswith(prefix)
        ]

    def get_object(self, bucket: str, key: str) -> str:
        return self.objects[key][1]


@pytest.fixture()
def source() -> S3Source:
    return S3Source(name="s3-kb", bucket="acme-kb", prefix="kb/", api=FakeS3Api())


def test_enumerates_bucket_with_etag_revisions(source: S3Source) -> None:
    docs = {d.relative_path: d for d in source.documents()}
    # the png is skipped; the prefix is stripped from draft placement
    assert set(docs) == {"runbooks/failover.md", "notes.md"}

    failover = docs["runbooks/failover.md"]
    assert failover.source_uri == "s3://acme-kb/kb/runbooks/failover.md"
    assert failover.revision == "etag-aaa"  # quotes stripped
    assert "Steps." in failover.content


def test_drafts_and_provenance_via_unchanged_core_loop(
    source: S3Source, tmp_path: Path
) -> None:
    drafts = ingest([source], tmp_path / "drafts")
    assert len(drafts) == 2
    frontmatter, _ = split_frontmatter(
        (tmp_path / "drafts" / "s3-kb" / "notes.md").read_text()
    )
    assert frontmatter["source"] == "s3://acme-kb/kb/notes.md"
    assert frontmatter["source_rev"] == "etag-bbb"
    assert frontmatter["type"] == "Document"


def test_ledger_states_work_for_s3_objects(source: S3Source, tmp_path: Path) -> None:
    ledger = Ledger.load(tmp_path / "ledger.yaml")
    for doc in source.documents():
        ledger.record(
            doc.source_uri, source.name, doc.relative_path, doc.revision, doc.content_sha256
        )

    api = source.api
    assert isinstance(api, FakeS3Api)
    etag, body = api.objects["kb/notes.md"]
    api.objects["kb/notes.md"] = ('"etag-bbb-2"', body + "More.\n")  # edited
    del api.objects["kb/runbooks/failover.md"]  # removed upstream

    current = {d.source_uri: (d.revision, d.content_sha256) for d in source.documents()}
    assert dict(ledger.status(current)) == {
        "s3://acme-kb/kb/notes.md": "modified",
        "s3://acme-kb/kb/runbooks/failover.md": "removed",
    }


def test_object_without_etag_is_an_error() -> None:
    class BrokenApi(FakeS3Api):
        def list_objects(self, bucket: str, prefix: str) -> list[dict]:
            return [{"key": "kb/odd.md"}]

    source = S3Source(name="s3-kb", bucket="acme-kb", api=BrokenApi())
    with pytest.raises(SourceError, match="no ETag"):
        list(source.documents())


def test_missing_boto3_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "boto3", None)  # forces ImportError
    with pytest.raises(SourceError, match="boto3"):
        Boto3S3Api.from_default_chain()
