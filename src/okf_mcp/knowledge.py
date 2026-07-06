"""Locate the knowledge root — the external tree the operator serves.

This repo is the **operator** (MCP server, validator, ingester); the
**knowledge** lives outside it, under a single root — typically a volume
mounted into a container, itself backed by one git repository per
sensitivity tier. Expected layout:

    <knowledge-root>/
    ├── bundles/            one directory per bundle (must contain index.md)
    ├── ingest.yaml         sync source configuration (optional)
    └── ingest/
        ├── quarantine/     failed conversions (last-known-good stays served)
        └── ledger.yaml     sync ledger (hash-keyed identity)

The root comes from the OKF_KNOWLEDGE_ROOT environment variable. Without it,
the operator falls back to the demo fixtures bundled in this repo, so a
fresh clone works with zero configuration.
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV = "OKF_KNOWLEDGE_ROOT"


class KnowledgeRootError(ValueError):
    """Raised when a configured knowledge root is unusable."""


def knowledge_root() -> Path | None:
    """The configured knowledge root, or None to use the demo fixtures."""
    raw = os.environ.get(_ENV)
    if not raw:
        return None
    root = Path(raw)
    if not root.is_dir():
        raise KnowledgeRootError(f"{_ENV}={raw!r} is not a directory")
    return root


def discover_bundles(root: Path) -> tuple[Path, ...]:
    """All bundles under `<root>/bundles` — directories carrying an index.md."""
    bundles_dir = root / "bundles"
    found = tuple(
        sorted(p for p in bundles_dir.iterdir() if (p / "index.md").is_file())
        if bundles_dir.is_dir()
        else ()
    )
    if not found:
        raise KnowledgeRootError(
            f"{bundles_dir} contains no bundles (expected subdirectories with an index.md)"
        )
    return found
