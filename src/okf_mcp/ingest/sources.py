"""Source connectors: enumerate documents with stable revision ids.

`Source` is the extension seam (issue #15): a connector yields
`SourceDocument`s and nothing else in the ingest loop knows or cares where
they come from. A new source system means one new class here plus config —
no core-loop changes.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SourceError(RuntimeError):
    """Raised when a source cannot be reached or read."""


class SourceUnconfiguredError(SourceError):
    """Raised when a source has no credentials/config to even attempt a
    pull (missing env var, missing token, missing SDK) — distinct from a
    configured source failing at runtime. Sync treats this as SKIPPED,
    never FAILED."""


class VectorPayloadError(ValueError):
    """Raised when a sidecar's vector payload is malformed: wrong types,
    non-finite values (NaN/Inf), or a `dim` that doesn't match the vector's
    length. Callers quarantine on this rather than crash the source."""


SIDECAR_SUFFIX = ".okf-vec.json"


@dataclass(frozen=True)
class VectorPayload:
    """A precomputed vector carried alongside a source document — data, not
    knowledge: it is never written into the knowledge tree, never becomes a
    concept, and (per the ingest security posture) never sets scopes,
    provenance, or resource URIs on its own."""

    model_id: str
    dim: int
    vector: tuple[float, ...]


def parse_vector_payload(raw: str) -> VectorPayload:
    """Strictly parse and validate a sidecar's JSON body
    (`{"model_id": str, "dim": int, "vector": [float, ...]}`). Raises
    `VectorPayloadError` naming the problem on any malformed input —
    wrong types, non-finite values, or a dim/length mismatch."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VectorPayloadError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VectorPayloadError("sidecar must be a JSON object")
    model_id, dim, vector = data.get("model_id"), data.get("dim"), data.get("vector")
    if not isinstance(model_id, str) or not model_id:
        raise VectorPayloadError("model_id must be a non-empty string")
    if not isinstance(dim, int) or isinstance(dim, bool) or dim <= 0:
        raise VectorPayloadError("dim must be a positive integer")
    if not isinstance(vector, list) or not vector:
        raise VectorPayloadError("vector must be a non-empty array of numbers")
    values: list[float] = []
    for item in vector:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise VectorPayloadError("vector entries must be numbers")
        value = float(item)
        if not math.isfinite(value):
            raise VectorPayloadError("vector entries must be finite (no NaN/Inf)")
        values.append(value)
    if len(values) != dim:
        raise VectorPayloadError(f"dim {dim} does not match vector length {len(values)}")
    return VectorPayload(model_id=model_id, dim=dim, vector=tuple(values))


def is_sidecar(path: Path) -> bool:
    """True for a vector sidecar file — never enumerated as a document."""
    return path.name.endswith(SIDECAR_SUFFIX)


def sidecar_for(document_path: Path) -> Path:
    """The sidecar path for `<path>` -> `<path>.okf-vec.json`, same directory."""
    return document_path.with_name(document_path.name + SIDECAR_SUFFIX)


def load_sidecar_vector(document_path: Path) -> tuple[VectorPayload | None, str | None]:
    """Read and parse `<document_path>.okf-vec.json` if present.

    Returns `(payload, None)` on success, `(None, None)` when no sidecar
    exists, or `(None, error)` when a sidecar exists but fails to parse —
    distinct from "no sidecar" so callers can still quarantine it.
    """
    sidecar = sidecar_for(document_path)
    if not sidecar.is_file():
        return None, None
    try:
        raw = sidecar.read_text(encoding="utf-8")
    except OSError as exc:
        return None, f"{sidecar}: cannot read sidecar: {exc}"
    try:
        return parse_vector_payload(raw), None
    except VectorPayloadError as exc:
        return None, f"{sidecar}: {exc}"


def load_sidecar_vector_from_text(
    label: str, raw: str
) -> tuple[VectorPayload | None, str | None]:
    """Like `load_sidecar_vector`, for connectors (Drive, S3) that already
    hold a sidecar's content as text rather than a filesystem path.
    `label` names the sidecar for the quarantine reason (e.g. its Drive
    file name or S3 key)."""
    try:
        return parse_vector_payload(raw), None
    except VectorPayloadError as exc:
        return None, f"{label}: {exc}"


@dataclass(frozen=True)
class SourceDocument:
    """One document pulled from a source."""

    source_uri: str  # canonical per-document URI, e.g. "<repo-url>#<path>"
    relative_path: str  # path within the source; decides placement
    revision: str  # stable revision id (commit hash, etag, ...)
    content: str
    vector: VectorPayload | None = None  # precomputed vector, connector opt-in
    vector_error: str | None = None  # sidecar present but failed to parse

    @property
    def content_sha256(self) -> str:
        """Content identity — consistency rolls on hashes, not revisions:
        no-op revisions are no-ops here, and the same content at a new path
        (rename) or reappearing after removal (resurrection) is recognisable
        as the same concept.
        """
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class Source(Protocol):
    """A configured origin of documents."""

    name: str

    def documents(self) -> Iterator[SourceDocument]: ...


@dataclass(frozen=True)
class GitSource:
    """Pull markdown documents from a git repository.

    `url` is anything `git clone` accepts; a path to an existing local clone
    is used in place, unmodified. Each document's revision is the hash of
    the last commit that touched it, so unrelated upstream commits don't
    mark it as changed.

    A fresh checkout is a **partial + sparse** clone scoped to `paths`:
    `--filter=blob:none` defers blob downloads for everything outside the
    checked-out cone, and `--sparse` (non-cone mode, so `paths`' glob
    patterns apply directly) limits the working tree to it. Commit history
    is never shallowed — the per-file revision lookup needs full history,
    not blob content, so it is unaffected.
    """

    name: str
    url: str
    paths: tuple[str, ...] = ("**/*.md",)
    cache_dir: Path = Path(".okf-ingest-cache")
    vectors_sidecar: bool = False  # opt-in: pair `<path>.okf-vec.json` sidecars

    def documents(self) -> Iterator[SourceDocument]:
        root = self._checkout()
        seen: set[Path] = set()
        for pattern in self.paths:
            for path in sorted(root.glob(pattern)):
                if not path.is_file() or path in seen or is_sidecar(path):
                    continue
                seen.add(path)
                rel = path.relative_to(root).as_posix()
                revision = _git(root, "log", "-1", "--format=%H", "--", rel).strip()
                if not revision:
                    continue  # untracked file — not part of the source's history
                vector = vector_error = None
                if self.vectors_sidecar:
                    vector, vector_error = load_sidecar_vector(path)
                yield SourceDocument(
                    source_uri=f"{self.url}#{rel}",
                    relative_path=rel,
                    revision=revision,
                    content=path.read_text(encoding="utf-8"),
                    vector=vector,
                    vector_error=vector_error,
                )

    def _checkout(self) -> Path:
        local = Path(self.url)
        if (local / ".git").exists():
            return local
        clone = self.cache_dir / self.name
        if (clone / ".git").exists():
            # Partial-clone filter and sparse-checkout patterns persist in
            # the clone's config, so a plain fast-forward pull respects both.
            _git(clone, "pull", "--ff-only", "--quiet")
        else:
            clone.parent.mkdir(parents=True, exist_ok=True)
            self._sparse_clone(clone)
        return clone

    def _sparse_clone(self, clone: Path) -> None:
        try:
            _git(
                Path.cwd(),
                "clone", "--quiet", "--filter=blob:none", "--sparse",
                "--no-checkout", self.url, str(clone),
            )
        except SourceError as exc:
            raise SourceError(f"cannot clone {self.url!r}: {exc}") from exc
        _git(clone, "sparse-checkout", "init", "--no-cone")
        patterns = self.paths
        if self.vectors_sidecar:
            # sparse-checkout is pattern-scoped; without this, sidecars
            # outside `paths`' literal match never reach the working tree.
            patterns = (*self.paths, *(f"{p}{SIDECAR_SUFFIX}" for p in self.paths))
        _git(clone, "sparse-checkout", "set", *patterns)
        _git(clone, "checkout", "--quiet")


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise SourceError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return result.stdout
