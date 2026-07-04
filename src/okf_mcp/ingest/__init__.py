"""okf-ingest: pull documents from configurable sources into draft concepts.

The ingester *proposes, never publishes*: drafts land in a staging directory
outside the served bundles, carrying provenance frontmatter (`source:`,
`source_rev:`, `ingested_at:`), and a human moves them into a bundle via a
normal PR. Two seams keep it extensible without touching the core loop:

- `Source` (sources.py) — where documents come from (git today; Drive, S3
  and others are one class + config each).
- `Transformer` (transform.py) — how a source document becomes a draft
  concept (passthrough today; LLM-assisted conversion later).
"""

from okf_mcp.ingest.core import Draft, ingest
from okf_mcp.ingest.sources import GitSource, Source, SourceDocument, SourceError
from okf_mcp.ingest.transform import PassthroughTransformer, Transformer

__all__ = [
    "Draft",
    "GitSource",
    "PassthroughTransformer",
    "Source",
    "SourceDocument",
    "SourceError",
    "Transformer",
    "ingest",
]
