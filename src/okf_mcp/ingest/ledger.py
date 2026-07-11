"""Sync ledger: which source document is which concept, keyed by content hash.

A committed, human-readable YAML file mapping each source document's URI to
the concept it projects into the knowledge tree, its connector revision, and
its `content_sha256`. Consistency rolls on the hash, not the revision:

- a revision change with identical content is **unchanged** (no churn from
  ETag re-uploads or touch-only commits);
- a new URI whose hash matches an entry that vanished this run is a
  **rename** — the concept keeps its identity, id, and inbound links;
- a new URI whose hash matches a previously removed entry is a
  **resurrection** — the concept comes back as itself (removed entries are
  retained, with their hashes, precisely for this).

Documents removed upstream are removed from the tree by sync and flagged
here with `removed_at`; git history in the knowledge repo is the tombstone.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml

State = Literal["new", "unchanged", "modified", "removed"]

_HEADER = (
    "# okf-ingest ledger — maintained by `okf-ingest`, committed for visibility.\n"
    "# One entry per source document: where it came from, at which revision and\n"
    "# content hash, and which concept it projects to. Entries removed upstream\n"
    "# are flagged with `removed_at` and retained so the concept can be\n"
    "# resurrected if its content reappears.\n"
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

    def classify(self, source_uri: str, revision: str, content_sha256: str) -> State:
        """State of one *present* document ("removed" is decided by sweep)."""
        entry = self._documents.get(source_uri)
        if entry is None:
            return "new"
        if entry.get("content_sha256") == content_sha256:
            return "unchanged"
        if "content_sha256" not in entry and entry.get("revision") == revision:
            return "unchanged"  # pre-hash ledger entry; fall back to revision
        return "modified"

    def record(
        self,
        source_uri: str,
        source_name: str,
        concept: str,
        revision: str,
        content_sha256: str,
    ) -> None:
        self._documents[source_uri] = {
            "source": source_name,
            "concept": concept,
            "revision": revision,
            "content_sha256": content_sha256,
            "synced_at": _now(),
        }

    def mark_seen(self, source_uri: str, revision: str | None = None) -> None:
        """A tracked document is present and unchanged; refresh its revision
        and `synced_at` so the `--since` staleness window stays meaningful
        for documents that never change."""
        entry = self._documents.get(source_uri)
        if entry is not None:
            entry.pop("removed_at", None)
            if revision is not None:
                entry["revision"] = revision
            entry["synced_at"] = _now()

    def match_by_sha(self, content_sha256: str, current_uris: set[str]) -> str | None:
        """A prior URI whose content matches and which is absent from this run.

        Absent-and-unflagged means a rename in flight; absent-and-flagged
        (`removed_at`) means a candidate resurrection. Either way the concept
        identity carries over via `adopt`.
        """
        for uri, entry in self._documents.items():
            if uri not in current_uris and entry.get("content_sha256") == content_sha256:
                return uri
        return None

    def adopt(self, old_uri: str, new_uri: str, revision: str) -> tuple[dict, bool]:
        """Transfer an entry to a new URI (rename/resurrection), keeping the
        concept. Returns (entry, was_removed)."""
        entry = self._documents.pop(old_uri)
        was_removed = entry.pop("removed_at", None) is not None
        entry["revision"] = revision
        entry["synced_at"] = _now()
        self._documents[new_uri] = entry
        return entry, was_removed

    def sweep_removed(self, seen_uris: set[str], source: str) -> list[str]:
        """Flag `source`'s tracked documents that vanished upstream; return
        newly flagged uris. Scoped to `source` (via the `source` field
        stamped by `record`) so one source's isolated failure can never
        tombstone another source's entries."""
        newly_removed = []
        for uri, entry in self._documents.items():
            if entry.get("source") != source:
                continue
            if uri not in seen_uris and "removed_at" not in entry:
                entry["removed_at"] = _now()
                newly_removed.append(uri)
        return sorted(newly_removed)

    def active_count(self, source: str) -> int:
        """Non-removed entries tracked for `source` — used by sync's
        empty-source guard (0 documents from a source that previously had
        entries is suspicious, not a legitimate removal)."""
        return sum(
            1
            for entry in self._documents.values()
            if entry.get("source") == source and "removed_at" not in entry
        )

    def status(self, current: dict[str, tuple[str, str]]) -> list[tuple[str, State]]:
        """Classify every known and current document, without mutating.

        `current` maps source URI → (revision, content_sha256).
        """
        states: dict[str, State] = {}
        for uri, (revision, sha) in current.items():
            states[uri] = self.classify(uri, revision, sha)
        for uri in self._documents:
            if uri not in current:
                states[uri] = "removed"
        return sorted(states.items())

    def entry(self, source_uri: str) -> dict | None:
        return self._documents.get(source_uri)

    def documents(self) -> Iterator[tuple[str, dict]]:
        """Iterate all tracked (source_uri, entry) pairs, including removed
        ones — callers filter on `removed_at` themselves."""
        return iter(self._documents.items())
