"""Optional semantic search: a content-hash-keyed embedding store.

Search today (`okf_mcp.index.OkfIndex.search`) is keyword-only. This module
adds an *optional* vector layer on top, without ever requiring it: the core
of this file is stdlib-only (`sqlite3` for storage, `array`/`struct` for
packed float32 vectors, plain Python for cosine similarity — no numpy). The
only non-stdlib dependency, `sentence-transformers`, is imported lazily
inside `SentenceTransformerEncoder` and only when embeddings are actually
used; nothing here is imported at module import time, so `import
okf_mcp.embeddings` never requires the `semantic` extra.

The store lives under `$OKF_KNOWLEDGE_ROOT` (default
`<root>/ingest/embeddings.db`) — never the operator repo, per "operator ≠
knowledge" (see `okf_mcp.knowledge`). It is keyed on the composite
`(content_sha256, model_id)`, the exact identity `okf-ingest` already
computes per document (`SourceDocument.content_sha256`, tracked in
`ingest/ledger.yaml`). That is what makes sync **incremental**: a document
whose hash is already embedded under the current model is never re-encoded,
only its `concept_id` mapping is refreshed (covers rename and resurrection,
both already classified by `okf_mcp.ingest.ledger.Ledger`). A `model_id`
change never mixes vectors across models — it simply re-embeds under a new
key, leaving old rows untouched.

Serving (see `okf_mcp.server`) queries this store only for concept ids drawn
from the caller's already-scoped `OkfIndex` view — an out-of-scope concept's
vector is never looked up, let alone returned.
"""

from __future__ import annotations

import array
import hashlib
import importlib.util
import logging
import math
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import yaml

if TYPE_CHECKING:
    from okf_mcp.ingest.ledger import Ledger
    from okf_mcp.ingest.sources import SourceDocument

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_STORE_RELATIVE_PATH = "ingest/embeddings.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS embeddings (
    content_sha256 TEXT NOT NULL,
    model_id       TEXT NOT NULL,
    concept_id     TEXT NOT NULL,
    dim            INTEGER NOT NULL,
    vector         BLOB NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (content_sha256, model_id)
)
"""
_INDEX = """
CREATE INDEX IF NOT EXISTS embeddings_model_concept
ON embeddings (model_id, concept_id)
"""


class EncoderUnavailableError(RuntimeError):
    """Raised when an encoder's backing library isn't installed."""


@runtime_checkable
class Encoder(Protocol):
    """The minimal seam semantic search needs from an embedding model."""

    @property
    def model_id(self) -> str:
        """Stable identifier stamped on every vector this encoder produces."""
        ...

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts, one vector per input, same order."""
        ...


def sentence_transformers_available() -> bool:
    """Whether `sentence-transformers` is importable, without importing it."""
    return importlib.util.find_spec("sentence_transformers") is not None


class SentenceTransformerEncoder:
    """Encoder backed by `sentence-transformers` (the `semantic` extra).

    The library is imported lazily on first `encode()` call — never at
    module import time — so the rest of this module works with the extra
    absent. When it's missing, `encode()` raises `EncoderUnavailableError`
    naming the fix (`uv sync --extra semantic`) rather than an opaque
    ImportError.
    """

    def __init__(self, model_id: str = DEFAULT_MODEL_ID) -> None:
        self._model_id = model_id
        self._model = None

    @property
    def model_id(self) -> str:
        return self._model_id

    def _load(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ImportError as exc:
                raise EncoderUnavailableError(
                    "sentence-transformers is not installed; run "
                    "`uv sync --extra semantic` to enable embeddings."
                ) from exc
            self._model = SentenceTransformer(self._model_id)
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        model = self._load()
        vectors = model.encode(list(texts))
        return [[float(x) for x in vector] for vector in vectors]


def _pack(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> list[float]:
    values = array.array("f")
    values.frombytes(blob)
    return values.tolist()


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return list(vector)
    return [x / norm for x in vector]


def cosine_top_k(
    query_vector: list[float], vectors: dict[str, list[float]], k: int
) -> list[tuple[str, float]]:
    """Cosine-similarity top-k over `{concept_id: vector}`, best first.

    `EmbeddingStore` normalizes vectors to unit length at write time, so
    only the query needs normalizing here; the score then reduces to a
    plain dot product.
    """
    query = _normalize(query_vector)
    scored = [
        (concept_id, sum(a * b for a, b in zip(query, vector, strict=True)))
        for concept_id, vector in vectors.items()
    ]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[: max(k, 0)]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class EmbeddingStore:
    """Sqlite-backed `(content_sha256, model_id) -> vector` store.

    Must live under `$OKF_KNOWLEDGE_ROOT` (default
    `<root>/ingest/embeddings.db`) — callers are responsible for pointing
    `path` there; this class only opens whatever path it's given.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_INDEX)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> EmbeddingStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def has(self, content_sha256: str, model_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM embeddings WHERE content_sha256 = ? AND model_id = ?",
            (content_sha256, model_id),
        ).fetchone()
        return row is not None

    def upsert(
        self, content_sha256: str, model_id: str, concept_id: str, vector: list[float]
    ) -> None:
        """Insert or replace one vector, normalized to unit length."""
        normalized = _normalize(vector)
        self._conn.execute(
            "INSERT INTO embeddings "
            "(content_sha256, model_id, concept_id, dim, vector, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (content_sha256, model_id) DO UPDATE SET "
            "concept_id = excluded.concept_id, dim = excluded.dim, "
            "vector = excluded.vector, created_at = excluded.created_at",
            (content_sha256, model_id, concept_id, len(normalized), _pack(normalized), _now()),
        )
        self._conn.commit()

    def set_concept(self, content_sha256: str, model_id: str, concept_id: str) -> None:
        """Rename/resurrection reuse: remap an existing vector's concept id
        without touching the vector itself (zero encode calls)."""
        self._conn.execute(
            "UPDATE embeddings SET concept_id = ? WHERE content_sha256 = ? AND model_id = ?",
            (concept_id, content_sha256, model_id),
        )
        self._conn.commit()

    def vectors_for(self, model_id: str, concept_ids: list[str]) -> dict[str, list[float]]:
        """Vectors for exactly the given concept ids under one model.

        Callers pass only ids already visible to the caller's session (e.g.
        `OkfIndex.visible_to(...).ids()`) — this is the enforcement point
        that keeps out-of-scope concepts unreachable via vector similarity.
        """
        ids = list(concept_ids)
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        rows = self._conn.execute(
            f"SELECT concept_id, vector FROM embeddings "
            f"WHERE model_id = ? AND concept_id IN ({placeholders})",
            (model_id, *ids),
        ).fetchall()
        return {concept_id: _unpack(blob) for concept_id, blob in rows}

    def top_k(
        self, model_id: str, concept_ids: list[str], query_vector: list[float], k: int
    ) -> list[tuple[str, float]]:
        """Cosine top-k restricted to `concept_ids` (the caller's scoped view)."""
        return cosine_top_k(query_vector, self.vectors_for(model_id, concept_ids), k)


def default_store_path(root: Path) -> Path:
    return Path(root) / DEFAULT_STORE_RELATIVE_PATH


def _concept_id_from_rel(concept_rel: str) -> str:
    """`bundles/<bundle>/<rel>.md` (ledger's root-relative concept path) ->
    `/<rel-without-bundle-without-.md>` (the bundle-relative id `OkfIndex`
    and `okf_mcp.parser.parse_document` use)."""
    parts = concept_rel.split("/")
    if len(parts) < 3 or parts[0] != "bundles":
        raise ValueError(f"unexpected concept path {concept_rel!r}")
    rel_in_bundle = "/".join(parts[2:])
    if rel_in_bundle.endswith(".md"):
        rel_in_bundle = rel_in_bundle[:-3]
    return "/" + rel_in_bundle


def _read_body(root: Path, concept_rel: str) -> str | None:
    """The concept's markdown body (frontmatter stripped), or None if the
    file is missing from the tree (e.g. quarantined)."""
    from okf_mcp.parser import split_frontmatter

    path = Path(root) / concept_rel
    if not path.is_file():
        return None
    _, body = split_frontmatter(path.read_text(encoding="utf-8"))
    return body


def _ledger_documents(ledger: Ledger) -> Iterable[tuple[str, dict]]:
    return ledger.documents()


def _quarantine_slug(source_uri: str) -> str:
    """Filesystem-safe stem for a quarantine artifact name, derived from a
    source URI that may contain `/`, `#`, `:`, ... ."""
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", source_uri).strip("_")
    digest = hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:8]
    return f"{slug[:120]}-{digest}"


def _quarantine_vector(quarantine_dir: Path | None, source_uri: str, reason: str) -> None:
    """Write a small quarantine artifact naming the offending source
    document and why its imported vector was rejected — the doc itself
    still gets a local-encode fallback (see `sync_embeddings`)."""
    if quarantine_dir is None:
        return
    qdir = Path(quarantine_dir) / "vectors"
    qdir.mkdir(parents=True, exist_ok=True)
    artifact = qdir / f"{_quarantine_slug(source_uri)}.txt"
    artifact.write_text(f"source_uri: {source_uri}\nreason: {reason}\n", encoding="utf-8")


def sync_embeddings(
    root: Path,
    ledger: Ledger,
    encoder: Encoder,
    store: EmbeddingStore,
    *,
    docs_with_vectors: dict[str, SourceDocument] | None = None,
    quarantine_dir: Path | None = None,
) -> int:
    """Incrementally embed the knowledge tree via the ledger's content hashes.

    For every tracked, non-removed ledger entry: if `(content_sha256,
    model_id)` is already in the store, only the `concept_id` mapping is
    refreshed — renames and resurrections reuse the existing vector with
    zero `encode()` calls. Otherwise the concept's body is read from the
    tree and queued. All queued texts are embedded in a single batched
    `encoder.encode()` call. Returns the number of documents encoded.

    `docs_with_vectors` (issue #49) maps `source_uri -> SourceDocument` for
    this run's documents that carried a precomputed vector (or a sidecar
    that failed to parse) — built by the caller from the freshly-pulled
    sources, keyed exactly like the ledger. A document with a valid
    `SourceDocument.vector` whose `model_id` matches this run's encoder is
    imported directly (`store.upsert`, zero `encode()` calls) — it is
    never queued for local encoding. A `model_id` mismatch or a malformed
    sidecar (`SourceDocument.vector_error`) is quarantined under
    `quarantine_dir` (source_uri + reason) and falls back to the ordinary
    local-encode path below, so the document is never silently unembedded
    and a mismatched vector never enters the store.

    Imported vectors are data, not model judgment: they never set scopes,
    provenance, or resource URIs — provenance for them is the ledger
    entry's own source/revision fields, same as any other synced document.

    Rows for removed entries are retained (so a later resurrection still
    hits the cache) — this function never deletes rows.
    """
    model_id = encoder.model_id
    docs_with_vectors = docs_with_vectors or {}
    to_embed: list[tuple[str, str, str]] = []  # (sha, concept_id, text)
    imported = 0
    for source_uri, entry in _ledger_documents(ledger):
        if entry.get("removed_at"):
            continue
        sha = entry.get("content_sha256")
        concept_rel = entry.get("concept")
        if not sha or not concept_rel:
            continue
        concept_id = _concept_id_from_rel(concept_rel)

        doc = docs_with_vectors.get(source_uri)
        if doc is not None and doc.vector is not None:
            if doc.vector.model_id == model_id:
                store.upsert(sha, model_id, concept_id, list(doc.vector.vector))
                imported += 1
                continue
            _quarantine_vector(
                quarantine_dir,
                source_uri,
                f"model_id mismatch: expected {model_id!r}, got {doc.vector.model_id!r}",
            )
        elif doc is not None and doc.vector_error is not None:
            _quarantine_vector(quarantine_dir, source_uri, doc.vector_error)

        if store.has(sha, model_id):
            store.set_concept(sha, model_id, concept_id)
            continue
        text = _read_body(root, concept_rel)
        if text is None:
            continue
        to_embed.append((sha, concept_id, text))

    if not to_embed:
        return imported

    vectors = encoder.encode([text for _, _, text in to_embed])
    for (sha, concept_id, _text), vector in zip(to_embed, vectors, strict=True):
        store.upsert(sha, model_id, concept_id, vector)
    return imported + len(to_embed)


def embeddings_config_from_file(config_path: Path) -> dict | None:
    """The `embeddings:` block of an ingest config file (`{model, path}`),
    or None when absent or malformed.

    Reads the file independently of `okf_mcp.ingest.cli.load_config` (which
    doesn't surface this optional, embeddings-only block) — a read-only,
    best-effort parse: a missing file or bad YAML means "no embeddings
    configured", never a crash.
    """
    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    block = raw.get("embeddings")
    return block if isinstance(block, dict) else None


def make_post_sync_hook(
    config: dict, store_root: Path, quarantine_dir: Path | None = None
) -> Callable[..., None]:
    """Build a `_post_sync(root, ledger, specs, docs_with_vectors)`-shaped
    hook from an `embeddings:` config block ({model, path}, both optional).

    `store_root` is always the true `$OKF_KNOWLEDGE_ROOT` — the embedding
    store is content-hash-keyed and shared across generations (issue #47),
    never staged per generation, so it stays fixed even when the hook's
    `root` argument (the tree to read concept bodies from) is a staged
    generation directory.

    `quarantine_dir` (issue #49) is the run's configured quarantine
    directory, forwarded to `sync_embeddings` so a mismatched or malformed
    imported vector lands next to the run's other quarantine artifacts.

    Runs `sync_embeddings` when `sentence-transformers` is importable; when
    it isn't (the `semantic` extra not installed), logs a clear skip naming
    the fix and returns — an unavailable encoder never fails sync.
    """
    model_id = config.get("model") or DEFAULT_MODEL_ID
    store_relative_path = config.get("path") or DEFAULT_STORE_RELATIVE_PATH

    def hook(
        root: Path,
        ledger: Ledger,
        specs: object,
        docs_with_vectors: dict[str, SourceDocument] | None = None,
    ) -> None:
        del specs  # unused: sync_embeddings walks the ledger directly
        if not sentence_transformers_available():
            logger.warning(
                "embeddings configured (model=%s) but sentence-transformers is not "
                "installed; skipping. Run `uv sync --extra semantic` to enable.",
                model_id,
            )
            return
        store = EmbeddingStore(Path(store_root) / store_relative_path)
        try:
            count = sync_embeddings(
                root,
                ledger,
                SentenceTransformerEncoder(model_id),
                store,
                docs_with_vectors=docs_with_vectors,
                quarantine_dir=quarantine_dir,
            )
            logger.info("embedded %d document(s) under model %s", count, model_id)
        finally:
            store.close()

    return hook
