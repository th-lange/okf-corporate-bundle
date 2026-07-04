"""Google Drive source connector (issue #17).

Second `Source` implementation, proving the connector seam: a configured
Drive folder is enumerated, native Google Docs are exported as markdown,
plain markdown files are downloaded as-is, and everything else is skipped.
The revision id is Drive's `headRevisionId` (falling back to `modifiedTime`),
so the ingest ledger's new/modified/removed classification works unchanged.

The Drive REST API is wrapped behind the tiny `DriveApi` protocol so tests
run against a fake with no network; the real client (`RestDriveApi`) reads
its bearer token from the GOOGLE_DRIVE_TOKEN environment variable only —
credentials never live in ingest config files.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from okf_mcp.ingest.sources import SourceDocument, SourceError

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
_API_BASE = "https://www.googleapis.com/drive/v3"
_TOKEN_ENV = "GOOGLE_DRIVE_TOKEN"


class DriveApi(Protocol):
    """The minimal surface of the Drive v3 API the connector needs."""

    def list_folder(self, folder_id: str) -> list[dict]:
        """File metadata dicts: id, name, mimeType, headRevisionId, modifiedTime."""
        ...

    def export(self, file_id: str, mime_type: str) -> str:
        """Export a native Google Doc to the given MIME type."""
        ...

    def download(self, file_id: str) -> str:
        """Download a regular file's content."""
        ...


@dataclass(frozen=True)
class DriveSource:
    """Pull markdown documents from one Google Drive folder.

    Native Google Docs are exported as markdown (`<name>.md`); files already
    named `*.md` are taken as-is; anything else is skipped.
    """

    name: str
    folder_id: str
    api: DriveApi | None = None  # injectable for tests; None → real REST client

    def documents(self) -> Iterator[SourceDocument]:
        api = self.api or RestDriveApi.from_env()
        for file in api.list_folder(self.folder_id):
            file_id, file_name = file["id"], file["name"]
            if file.get("mimeType") == GOOGLE_DOC_MIME:
                content = api.export(file_id, "text/markdown")
                relative_path = file_name if file_name.endswith(".md") else f"{file_name}.md"
            elif file_name.endswith(".md"):
                content = api.download(file_id)
                relative_path = file_name
            else:
                continue  # not knowledge-shaped; skip binaries, sheets, ...
            revision = file.get("headRevisionId") or file.get("modifiedTime")
            if not revision:
                raise SourceError(f"Drive file {file_name!r} has no usable revision id")
            yield SourceDocument(
                source_uri=f"gdrive://{file_id}",
                relative_path=relative_path,
                revision=str(revision),
                content=content,
            )


class RestDriveApi:
    """Thin Drive v3 REST client authenticated by a bearer token."""

    def __init__(self, token: str) -> None:
        self._token = token

    @classmethod
    def from_env(cls) -> RestDriveApi:
        token = os.environ.get(_TOKEN_ENV)
        if not token:
            raise SourceError(
                f"Google Drive sources need the {_TOKEN_ENV} environment variable "
                "(an OAuth bearer token with drive.readonly scope)."
            )
        return cls(token)

    def list_folder(self, folder_id: str) -> list[dict]:
        query = urllib.parse.urlencode(
            {
                "q": f"'{folder_id}' in parents and trashed = false",
                "fields": "files(id, name, mimeType, headRevisionId, modifiedTime)",
            }
        )
        return json.loads(self._get(f"{_API_BASE}/files?{query}")).get("files", [])

    def export(self, file_id: str, mime_type: str) -> str:
        query = urllib.parse.urlencode({"mimeType": mime_type})
        return self._get(f"{_API_BASE}/files/{file_id}/export?{query}")

    def download(self, file_id: str) -> str:
        return self._get(f"{_API_BASE}/files/{file_id}?alt=media")

    def _get(self, url: str) -> str:
        request = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._token}"}
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.read().decode("utf-8")
        except OSError as exc:
            raise SourceError(f"Drive API request failed: {exc}") from exc
