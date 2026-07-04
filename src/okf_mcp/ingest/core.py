"""The ingest loop: sources → transformer → drafts in the staging directory.

The ingester proposes, never publishes — drafts are written outside the
served bundles and reach a bundle only through human review in a normal PR.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from okf_mcp.ingest.sources import Source
from okf_mcp.ingest.transform import PassthroughTransformer, Transformer


@dataclass(frozen=True)
class Draft:
    """One draft concept written to the staging directory."""

    path: Path
    source_name: str
    source_uri: str
    revision: str


def ingest(
    sources: Iterable[Source],
    staging_dir: Path,
    transformer: Transformer | None = None,
) -> list[Draft]:
    """Pull every document from every source and write drafts.

    Drafts land at `<staging_dir>/<source name>/<relative path>`, so a
    source's internal layout is preserved and two sources can never collide.
    """
    transformer = transformer or PassthroughTransformer()
    drafts: list[Draft] = []
    for source in sources:
        for doc in source.documents():
            rel = doc.relative_path
            if not rel.endswith(".md"):
                rel += ".md"
            path = staging_dir / source.name / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(transformer.transform(doc), encoding="utf-8")
            drafts.append(
                Draft(
                    path=path,
                    source_name=source.name,
                    source_uri=doc.source_uri,
                    revision=doc.revision,
                )
            )
    return drafts
