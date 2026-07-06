"""Transformer seam: source document → draft concept markdown.

Passthrough is the only implementation today: sources are expected to be
markdown already, and we stamp provenance plus a default `type`. LLM-assisted
conversion of arbitrary documents plugs in here later — one new class, no
change to the ingest loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import yaml

from okf_mcp.ingest.sources import SourceDocument
from okf_mcp.parser import split_frontmatter


class Transformer(Protocol):
    """Turns one source document into draft concept markdown."""

    def transform(self, doc: SourceDocument) -> str: ...


@dataclass(frozen=True)
class PassthroughTransformer:
    """Keep the document as-is; stamp provenance and default the `type`."""

    default_type: str = "Document"

    def transform(self, doc: SourceDocument) -> str:
        frontmatter, body = split_frontmatter(doc.content)
        # Scoping never comes from source content — visibility is assigned by
        # directory defaults in the knowledge tree, not by the document.
        frontmatter.pop("scope", None)
        frontmatter.pop("scope_default", None)
        frontmatter.setdefault("type", self.default_type)
        frontmatter["source"] = doc.source_uri
        frontmatter["source_rev"] = doc.revision
        frontmatter["ingested_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        return f"---\n{rendered}---\n{body}"
