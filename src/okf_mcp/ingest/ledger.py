"""Ingest ledger: what has been ingested, from where, at which revision.

A committed, human-readable YAML file mapping each source document's URI to
its draft, revision, and ingest time. Comparing current source revisions
against the ledger classifies every document as new / unchanged / modified /
removed — the ingester regenerates drafts only for new and modified
documents, and *flags* removed ones (it never deletes anything; retiring a
concept is a human decision).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

State = Literal["new", "unchanged", "modified", "removed"]

_HEADER = (
    "# okf-ingest ledger — maintained by `okf-ingest`, committed for visibility.\n"
    "# One entry per source document: where it came from, at which revision,\n"
    "# and which draft it produced. Entries are never auto-deleted; documents\n"
    "# that vanish upstream are flagged with `removed_at`.\n"
)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, path: Path, documents: dict[str, dict] | None = None) -> None:
        self.path = path
        self._documents: dict[str, dict] = documents or {}

    @classmethod
    def load(cls, path: Path) -> Ledger:
        if not path.exists():
            return cls(path)
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        documents = raw.get("documents") or {}
        if not isinstance(documents, dict):
            raise ValueError(f"{path}: ledger `documents` must be a mapping")
        return cls(path, documents)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump({"documents": self._documents}, sort_keys=True, allow_unicode=True)
        self.path.write_text(_HEADER + body, encoding="utf-8")

    def classify(self, source_uri: str, revision: str) -> State:
        """State of one *present* document ("removed" is decided by sweep)."""
        entry = self._documents.get(source_uri)
        if entry is None:
            return "new"
        return "unchanged" if entry.get("revision") == revision else "modified"

    def record(self, source_uri: str, source_name: str, draft: str, revision: str) -> None:
        self._documents[source_uri] = {
            "source": source_name,
            "draft": draft,
            "revision": revision,
            "ingested_at": _now(),
        }

    def mark_seen(self, source_uri: str) -> None:
        """A tracked document is present upstream again; clear any removed flag."""
        entry = self._documents.get(source_uri)
        if entry is not None:
            entry.pop("removed_at", None)

    def sweep_removed(self, seen_uris: set[str]) -> list[str]:
        """Flag tracked documents that vanished upstream; return newly flagged."""
        newly_removed = []
        for uri, entry in self._documents.items():
            if uri not in seen_uris and "removed_at" not in entry:
                entry["removed_at"] = _now()
                newly_removed.append(uri)
        return sorted(newly_removed)

    def status(self, current_revisions: dict[str, str]) -> list[tuple[str, State]]:
        """Classify every known and current document, without mutating."""
        states: dict[str, State] = {}
        for uri, revision in current_revisions.items():
            states[uri] = self.classify(uri, revision)
        for uri in self._documents:
            if uri not in current_revisions:
                states[uri] = "removed"
        return sorted(states.items())

    def entry(self, source_uri: str) -> dict | None:
        return self._documents.get(source_uri)
