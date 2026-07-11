"""Precomputed vector import (issue #49): sidecar carrier, connector opt-in,
the `sync_embeddings` import path, and the model_id/quarantine guard.

Sidecars are metadata, not knowledge: `<path>.okf-vec.json` never becomes a
document in its own right, and an imported vector is data — it never sets
scopes, provenance, or resource URIs (provenance stays the ledger entry's
own source/revision fields, same as any other synced document).
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from pathlib import Path

import pytest

from okf_mcp.embeddings import EmbeddingStore
from okf_mcp.embeddings import sync_embeddings as sync_embeddings_fn
from okf_mcp.ingest.ledger import Ledger
from okf_mcp.ingest.sources import GitSource, SourceDocument, VectorPayload

_DIM = 16


class FakeEncoder:
    """Deterministic, keyword-hash-based vectors; never touches the network.
    Tracks call count so tests can assert exactly how much local encoding
    happened (ideally zero, for imported vectors)."""

    def __init__(self, model_id: str = "fake-v1") -> None:
        self.model_id = model_id
        self.calls = 0
        self.texts_seen: list[str] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts_seen.extend(texts)
        return [_hash_vector(t) for t in texts]


def _hash_vector(text: str, dim: int = _DIM) -> list[float]:
    vector = [0.0] * dim
    for word in text.lower().split():
        idx = int(hashlib.md5(word.encode()).hexdigest(), 16) % dim
        vector[idx] += 1.0
    return vector


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_concept(root: Path, rel: str, body: str) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\ntype: Term\n---\n{body}\n"
    path.write_text(text, encoding="utf-8")
    return _sha(text)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t.test", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def _repo_with(tmp_path: Path, files: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(repo, "init", "--quiet")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "seed")
    return repo


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "knowledge"


@pytest.fixture()
def store(tmp_path: Path) -> EmbeddingStore:
    return EmbeddingStore(tmp_path / "embeddings.db")


def _empty_ledger(root: Path) -> Ledger:
    return Ledger(root / "ingest" / "ledger.yaml")


# --- carrier format: GitSource sidecar opt-in ------------------------------


def test_gitsource_pairs_valid_sidecar_when_opted_in(tmp_path: Path) -> None:
    payload = {"model_id": "vendor-v1", "dim": 3, "vector": [0.1, 0.2, 0.3]}
    repo = _repo_with(
        tmp_path, {"a.md": "# A\n", "a.md.okf-vec.json": json.dumps(payload)}
    )
    docs = list(GitSource(name="src", url=str(repo), vectors_sidecar=True).documents())
    assert [d.relative_path for d in docs] == ["a.md"]  # sidecar never its own document
    doc = docs[0]
    assert doc.vector == VectorPayload(model_id="vendor-v1", dim=3, vector=(0.1, 0.2, 0.3))
    assert doc.vector_error is None


def test_gitsource_ignores_sidecar_without_optin(tmp_path: Path) -> None:
    payload = {"model_id": "vendor-v1", "dim": 1, "vector": [0.5]}
    repo = _repo_with(
        tmp_path, {"a.md": "# A\n", "a.md.okf-vec.json": json.dumps(payload)}
    )
    docs = list(GitSource(name="src", url=str(repo)).documents())  # vectors_sidecar=False
    assert len(docs) == 1
    assert docs[0].vector is None
    assert docs[0].vector_error is None


def test_sidecar_files_never_appear_as_concepts_in_the_knowledge_tree(tmp_path: Path) -> None:
    """A sidecar is metadata: it must never be enumerable as a document, no
    matter the connector's glob patterns — so `_apply` (which only ever
    writes concepts for yielded `SourceDocument`s) can never turn it into
    one."""
    payload = {"model_id": "vendor-v1", "dim": 1, "vector": [0.5]}
    repo = _repo_with(
        tmp_path, {"a.md": "# A\n", "a.md.okf-vec.json": json.dumps(payload)}
    )
    source = GitSource(name="src", url=str(repo), paths=("**/*",), vectors_sidecar=True)
    relative_paths = {d.relative_path for d in source.documents()}
    assert relative_paths == {"a.md"}
    assert not any(p.endswith(".okf-vec.json") for p in relative_paths)


@pytest.mark.parametrize(
    "payload",
    [
        {"model_id": "v1", "dim": 4, "vector": [0.1, 0.2, 0.3]},  # dim/length mismatch
        {"model_id": "v1", "dim": 1, "vector": ["not-a-number"]},  # non-numeric
        {"model_id": "v1", "dim": 1, "vector": [float("nan")]},  # NaN
        {"model_id": "v1", "dim": 1, "vector": [float("inf")]},  # Inf
        {"model_id": "", "dim": 1, "vector": [0.1]},  # empty model_id
        "not even an object",
    ],
    ids=["dim-mismatch", "non-numeric", "nan", "inf", "empty-model-id", "not-an-object"],
)
def test_gitsource_rejects_malformed_sidecar_without_crashing(
    tmp_path: Path, payload: object
) -> None:
    repo = _repo_with(
        tmp_path, {"a.md": "# A\n", "a.md.okf-vec.json": json.dumps(payload)}
    )
    docs = list(GitSource(name="src", url=str(repo), vectors_sidecar=True).documents())
    assert len(docs) == 1  # the source pull itself never raises
    assert docs[0].vector is None
    assert docs[0].vector_error is not None


# --- import path: sync_embeddings ------------------------------------------


def test_sync_embeddings_imports_valid_sidecar_vectors_with_zero_local_encodes(
    root: Path, store: EmbeddingStore
) -> None:
    sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    sha_b = _write_concept(root, "bundles/kb/b.md", "# B\nGoodbye world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha_a)
    ledger.record("uri://b", "src", "bundles/kb/b.md", "rev1", sha_b)

    encoder = FakeEncoder()
    docs_with_vectors = {
        "uri://a": SourceDocument(
            source_uri="uri://a",
            relative_path="a.md",
            revision="rev1",
            content="unused",
            vector=VectorPayload(model_id="fake-v1", dim=2, vector=(3.0, 4.0)),
        ),
        "uri://b": SourceDocument(
            source_uri="uri://b",
            relative_path="b.md",
            revision="rev1",
            content="unused",
            vector=VectorPayload(model_id="fake-v1", dim=2, vector=(1.0, 0.0)),
        ),
    }

    count = sync_embeddings_fn(root, ledger, encoder, store, docs_with_vectors=docs_with_vectors)

    assert count == 2
    assert encoder.calls == 0  # zero local encode calls — everything imported
    vector_a = store.vectors_for("fake-v1", ["/a"])["/a"]
    assert vector_a == pytest.approx([0.6, 0.8], abs=1e-6)  # (3,4) normalized


def test_sync_embeddings_mixed_run_encodes_only_the_doc_without_a_sidecar(
    root: Path, store: EmbeddingStore
) -> None:
    sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    sha_b = _write_concept(root, "bundles/kb/b.md", "# B\nGoodbye world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha_a)
    ledger.record("uri://b", "src", "bundles/kb/b.md", "rev1", sha_b)

    encoder = FakeEncoder()
    docs_with_vectors = {
        "uri://a": SourceDocument(
            source_uri="uri://a",
            relative_path="a.md",
            revision="rev1",
            content="unused",
            vector=VectorPayload(model_id="fake-v1", dim=2, vector=(1.0, 0.0)),
        )
    }

    count = sync_embeddings_fn(root, ledger, encoder, store, docs_with_vectors=docs_with_vectors)

    assert count == 2  # one imported, one locally encoded
    assert encoder.calls == 1  # exactly one encode call, for /b only
    assert encoder.texts_seen[0].strip() == "# B\nGoodbye world"
    assert set(store.vectors_for("fake-v1", ["/a", "/b"])) == {"/a", "/b"}


def test_sync_embeddings_model_id_mismatch_quarantines_and_falls_back_local(
    root: Path, store: EmbeddingStore, tmp_path: Path
) -> None:
    sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha_a)

    encoder = FakeEncoder(model_id="fake-v1")
    mismatched = VectorPayload(model_id="other-vendor-v2", dim=2, vector=(9.0, 9.0))
    docs_with_vectors = {
        "uri://a": SourceDocument(
            source_uri="uri://a",
            relative_path="a.md",
            revision="rev1",
            content="unused",
            vector=mismatched,
        )
    }
    quarantine_dir = tmp_path / "quarantine"

    count = sync_embeddings_fn(
        root,
        ledger,
        encoder,
        store,
        docs_with_vectors=docs_with_vectors,
        quarantine_dir=quarantine_dir,
    )

    assert count == 1
    assert encoder.calls == 1  # local fallback happened — never silently unembedded
    stored = store.vectors_for("fake-v1", ["/a"])["/a"]
    mismatched_normalized = [
        v / math.sqrt(9.0**2 + 9.0**2) for v in mismatched.vector
    ]
    assert stored != pytest.approx(mismatched_normalized)  # mismatched vector never entered

    artifacts = list((quarantine_dir / "vectors").glob("*.txt"))
    assert len(artifacts) == 1
    text = artifacts[0].read_text(encoding="utf-8")
    assert "uri://a" in text
    assert "fake-v1" in text and "other-vendor-v2" in text


def test_sync_embeddings_malformed_sidecar_quarantines_and_falls_back_local(
    root: Path, store: EmbeddingStore, tmp_path: Path
) -> None:
    sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha_a)

    encoder = FakeEncoder()
    docs_with_vectors = {
        "uri://a": SourceDocument(
            source_uri="uri://a",
            relative_path="a.md",
            revision="rev1",
            content="unused",
            vector_error="a.md.okf-vec.json: dim 4 does not match vector length 2",
        )
    }
    quarantine_dir = tmp_path / "quarantine"

    count = sync_embeddings_fn(
        root,
        ledger,
        encoder,
        store,
        docs_with_vectors=docs_with_vectors,
        quarantine_dir=quarantine_dir,
    )

    assert count == 1
    assert encoder.calls == 1  # never crashes sync — falls back to local encode
    assert store.has(sha_a, encoder.model_id)

    artifacts = list((quarantine_dir / "vectors").glob("*.txt"))
    assert len(artifacts) == 1
    assert "dim 4" in artifacts[0].read_text(encoding="utf-8")


def test_sync_embeddings_without_docs_with_vectors_is_unaffected(
    root: Path, store: EmbeddingStore
) -> None:
    """No `docs_with_vectors` argument (the pre-#49 call shape) behaves
    exactly as before — purely local encoding, no quarantine writes."""
    sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha_a)
    encoder = FakeEncoder()

    count = sync_embeddings_fn(root, ledger, encoder, store)

    assert count == 1
    assert encoder.calls == 1


# --- end-to-end: `okf-ingest sync` wires the sidecar opt-in ----------------


def test_cli_sync_imports_sidecar_vectors_and_falls_back_for_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full integration: a `vectors: sidecar` source in `ingest.yaml`
    reaches the store via the real `_build_connector` -> `_pull_source` ->
    `_post_sync` -> `sync_embeddings` path, not a hand-built map."""
    from okf_mcp import embeddings as embeddings_mod
    from okf_mcp.ingest.cli import main as ingest_main

    payload = {"model_id": "fake-cli-model", "dim": 16, "vector": _hash_vector("# A\nHas vector")}
    source_repo = _repo_with(
        tmp_path,
        {
            "a.md": "# A\nHas vector\n",
            "a.md.okf-vec.json": json.dumps(payload),
            "b.md": "# B\nNo vector here\n",
        },
    )

    root = tmp_path / "knowledge"
    kb = root / "bundles" / "kb"
    kb.mkdir(parents=True)
    (kb / "index.md").write_text("---\ntype: Index\nscope_default: [public]\n---\n# KB\n")
    _git(root, "init", "--quiet")
    _git(root, "add", ".")
    _git(root, "commit", "--quiet", "-m", "init knowledge repo")
    (root / "ingest.yaml").write_text(
        "embeddings:\n  model: fake-cli-model\n  path: ingest/embeddings.db\n"
        "sources:\n"
        f"  - name: precomputed\n    type: git\n    url: {source_repo}\n"
        "    vectors: sidecar\n    target: kb\n"
    )
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)
    monkeypatch.setattr(embeddings_mod, "sentence_transformers_available", lambda: True)
    monkeypatch.setattr(embeddings_mod, "SentenceTransformerEncoder", FakeEncoder)

    assert ingest_main(["sync"]) == 0

    store = embeddings_mod.EmbeddingStore(root / "ingest" / "embeddings.db")
    vectors = store.vectors_for("fake-cli-model", ["/a", "/b"])
    assert set(vectors) == {"/a", "/b"}
    # /a's vector came straight from the sidecar (normalized), never re-encoded
    norm = math.sqrt(sum(v * v for v in payload["vector"]))
    expected_a = [v / norm for v in payload["vector"]]
    assert vectors["/a"] == pytest.approx(expected_a, abs=1e-6)
    store.close()

    # concepts never include the sidecar itself
    assert not (kb / "a.md.okf-vec.json").exists()
    assert sorted(p.name for p in kb.glob("*.md")) == ["a.md", "b.md", "index.md"]
