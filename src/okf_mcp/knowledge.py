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


GENERATIONS_DIRNAME = "generations"
POINTER_NAME = "CURRENT"


def generations_dir(root: Path) -> Path:
    """Where published generations live under a knowledge root."""
    return root / GENERATIONS_DIRNAME


def pointer_path(root: Path) -> Path:
    """The `CURRENT` pointer file: a plain-filesystem mechanism (no git
    required) flipped via atomic rename by `okf_mcp.ingest.generations`."""
    return generations_dir(root) / POINTER_NAME


def current_generation_dir(root: Path) -> Path | None:
    """The knowledge root's CURRENT generation directory, or None when this
    root has no generations published yet — including every existing
    plain-directory root, since generations are strictly additive."""
    try:
        generation_id = pointer_path(root).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    if not generation_id:
        return None
    candidate = generations_dir(root) / generation_id
    return candidate if candidate.is_dir() else None


def resolve_root(root: Path) -> Path:
    """The tree to actually read bundles/ledger from: the CURRENT
    generation when `root` publishes generationally, else `root` itself
    (legacy layout — unchanged when no `generations/CURRENT` pointer
    exists)."""
    return current_generation_dir(root) or root


def discover_bundles(root: Path) -> tuple[Path, ...]:
    """All bundles under `<root>/bundles` — directories carrying an index.md.

    Resolves the CURRENT generation first when `root` publishes
    generationally (see `resolve_root`); a plain-directory root behaves
    exactly as before.
    """
    bundles_dir = resolve_root(root) / "bundles"
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
