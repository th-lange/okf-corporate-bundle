"""Generational atomic publish (issue #47).

`okf-ingest sync` normally writes straight into `$OKF_KNOWLEDGE_ROOT`
(legacy, in-place). When a knowledge root opts in (`generations: true` in
`ingest.yaml`), sync instead stages the next full knowledge tree under
`generations/<id>/` — seeded from the current generation via hardlinks so a
run only pays for what actually changed — runs the ordinary sync against
that staged copy, validates its structure, and only then flips the
`generations/CURRENT` pointer.

The pointer is a plain file holding one generation id, flipped with
`os.replace` (atomic on POSIX, same filesystem): readers that resolve it
either see the old id or the new one, never a partial write, and the
mechanism needs no git repository — `_commit` in `okf_mcp.ingest.cli`
already returns `None` on a non-git root, so git can only ever be the audit
trail, never the publish mechanism. A staged generation that fails to
build or fails `validate_generation` is discarded before the pointer is
ever touched, so the last-good generation keeps serving.

The embedding store (`okf_mcp.embeddings.EmbeddingStore`) is intentionally
NOT staged per generation — see `okf_mcp.embeddings` and docs/usage.md for
why it is shared, content-hash-keyed, at `<root>/ingest/embeddings.db`.
"""

from __future__ import annotations

import itertools
import os
import shutil
import time
from pathlib import Path

import yaml

from okf_mcp.knowledge import (
    KnowledgeRootError,
    current_generation_dir,
    discover_bundles,
    generations_dir,
    pointer_path,
)

DEFAULT_KEEP = 5

_id_counter = itertools.count()


class GenerationValidationError(ValueError):
    """Raised when a staged generation fails its pre-publish structural
    check; the caller discards the staged directory and never flips
    CURRENT."""


def generations_enabled_from_file(config_path: Path) -> bool:
    """Whether `ingest.yaml` opts into generational publish (`generations:
    true`). Read-only, best-effort like `embeddings_config_from_file`: a
    missing file or bad YAML just means "off", never a crash."""
    raw = _safe_load(config_path)
    return isinstance(raw, dict) and bool(raw.get("generations"))


def generations_keep_from_file(config_path: Path) -> int:
    """The `generations_keep` retention count (default `DEFAULT_KEEP`)."""
    raw = _safe_load(config_path)
    if not isinstance(raw, dict):
        return DEFAULT_KEEP
    keep = raw.get("generations_keep")
    return keep if isinstance(keep, int) and keep > 0 else DEFAULT_KEEP


def _safe_load(config_path: Path) -> object:
    try:
        return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None


def new_generation_id() -> str:
    """Monotonic id: a microsecond timestamp plus a process-local counter,
    so concurrent calls within one process never collide and lexicographic
    sort order matches publish order."""
    return f"{time.time_ns() // 1_000:016d}-{next(_id_counter):06d}"


def _hardlink_or_copy(source: str, dest: str) -> None:
    try:
        os.link(source, dest)
    except OSError:
        # Cross-device staging (e.g. some container overlay mounts) — a
        # real copy is the correct fallback, just not free.
        shutil.copy2(source, dest)


def stage_generation(root: Path) -> Path:
    """Create the next generation directory, seeded from the current one —
    hardlinked `bundles/` + `ingest/ledger.yaml`, so this run only needs to
    write what actually changes. The very first generation seeds from
    `root`'s existing plain-layout content, if any (the migration path for
    a root that just turned generations on)."""
    base = current_generation_dir(root) or root
    staged = generations_dir(root) / new_generation_id()
    staged.mkdir(parents=True)
    if (base / "bundles").is_dir():
        shutil.copytree(base / "bundles", staged / "bundles", copy_function=_hardlink_or_copy)
    else:
        (staged / "bundles").mkdir()
    ledger_src = base / "ingest" / "ledger.yaml"
    if ledger_src.is_file():
        (staged / "ingest").mkdir(parents=True, exist_ok=True)
        _hardlink_or_copy(str(ledger_src), str(staged / "ingest" / "ledger.yaml"))
    return staged


def validate_generation(staged: Path) -> None:
    """Cheap structural check of a staged generation before it can ever be
    published: every target bundle must exist with an index.md (the run's
    own per-document validation already ran inside `_sync`). Raises
    `GenerationValidationError` on failure."""
    try:
        discover_bundles(staged)
    except KnowledgeRootError as exc:
        raise GenerationValidationError(str(exc)) from exc


def discard_generation(staged: Path) -> None:
    """Drop a staged generation that never got published — a failed run,
    or an orphan left behind by one that crashed before cleanup."""
    shutil.rmtree(staged, ignore_errors=True)


def publish_generation(root: Path, staged: Path) -> None:
    """Atomically flip `generations/CURRENT` to `staged`. `os.replace` is
    atomic on POSIX for a rename within one filesystem (`staged` and the
    pointer both live under `root`), so a reader resolving the pointer
    concurrently always sees a complete id — the old one or the new one,
    never a torn write."""
    pointer = pointer_path(root)
    tmp = pointer.with_name(pointer.name + ".tmp")
    tmp.write_text(staged.name, encoding="utf-8")
    os.replace(tmp, pointer)


def prune_generations(root: Path, keep: int) -> list[str]:
    """Delete published generations beyond the most recent `keep`
    (lexicographic id order == publish order); CURRENT is never pruned.
    Returns the pruned ids."""
    gdir = generations_dir(root)
    if not gdir.is_dir():
        return []
    current = current_generation_dir(root)
    current_name = current.name if current else None
    ids = sorted(p.name for p in gdir.iterdir() if p.is_dir())
    doomed = ids[:-keep] if keep > 0 else ids
    pruned = []
    for generation_id in doomed:
        if generation_id == current_name:
            continue
        shutil.rmtree(gdir / generation_id, ignore_errors=True)
        pruned.append(generation_id)
    return pruned
