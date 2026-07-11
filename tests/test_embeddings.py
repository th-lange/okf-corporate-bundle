"""Semantic search (issue #45): content-hash-keyed embedding store."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from okf_mcp.embeddings import EmbeddingStore, cosine_top_k
from okf_mcp.embeddings import sync_embeddings as sync_embeddings_fn
from okf_mcp.ingest.ledger import Ledger

_DIM = 16


class FakeEncoder:
    """Deterministic, keyword-hash-based vectors; never touches the network
    or imports sentence_transformers. Tracks call count so tests can assert
    on exactly how much (re)encoding happened."""

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
    """Write `bundles/<bundle>/<rel>` with a minimal frontmatter, return the
    content hash of the full markdown as it would be tracked in the ledger."""
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"---\ntype: Term\n---\n{body}\n"
    path.write_text(text, encoding="utf-8")
    return _sha(text)


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    return tmp_path / "knowledge"


@pytest.fixture()
def store(tmp_path: Path) -> EmbeddingStore:
    return EmbeddingStore(tmp_path / "embeddings.db")


def _empty_ledger(root: Path) -> Ledger:
    return Ledger(root / "ingest" / "ledger.yaml")


def test_sync_embeddings_second_run_over_same_ledger_encodes_nothing(
    root: Path, store: EmbeddingStore
) -> None:
    sha = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha)
    encoder = FakeEncoder()

    first = sync_embeddings_fn(root, ledger, encoder, store)
    assert first == 1
    assert encoder.calls == 1

    second = sync_embeddings_fn(root, ledger, encoder, store)
    assert second == 0
    assert encoder.calls == 1  # zero recompute — unchanged content


def test_sync_embeddings_modified_content_reencodes_only_that_doc(
    root: Path, store: EmbeddingStore
) -> None:
    sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    sha_b = _write_concept(root, "bundles/kb/b.md", "# B\nGoodbye moon")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha_a)
    ledger.record("uri://b", "src", "bundles/kb/b.md", "rev1", sha_b)
    encoder = FakeEncoder()
    assert sync_embeddings_fn(root, ledger, encoder, store) == 2
    assert encoder.calls == 1  # one batched call for both

    # modify a.md — new content, new hash
    new_sha_a = _write_concept(root, "bundles/kb/a.md", "# A\nHello brand new world")
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev2", new_sha_a)

    encoded = sync_embeddings_fn(root, ledger, encoder, store)
    assert encoded == 1
    assert encoder.calls == 2
    assert encoder.texts_seen[-1].strip() == "# A\nHello brand new world"
    assert store.has(new_sha_a, encoder.model_id)
    assert store.has(sha_b, encoder.model_id)  # untouched


def test_rename_same_hash_new_concept_id_reuses_vector_zero_encodes(
    root: Path, store: EmbeddingStore
) -> None:
    sha = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://old", "src", "bundles/kb/a.md", "rev1", sha)
    encoder = FakeEncoder()
    assert sync_embeddings_fn(root, ledger, encoder, store) == 1
    assert encoder.calls == 1
    assert store.vectors_for(encoder.model_id, ["/a"])

    # simulate a rename: old source URI is gone, a new one carries the same
    # content hash but resolves to a different concept path; the old entry
    # stays tracked too (still resolvable), last-seen mapping wins the row
    ledger.record("uri://new", "src", "bundles/kb/renamed.md", "rev1", sha)

    encoded = sync_embeddings_fn(root, ledger, encoder, store)
    assert encoded == 0
    assert encoder.calls == 1  # zero recompute

    remapped = store.vectors_for(encoder.model_id, ["/renamed"])
    assert "/renamed" in remapped
    original = store.vectors_for(encoder.model_id, ["/a"])
    assert "/a" not in original  # old id no longer resolves


def test_model_change_forces_full_reembed_without_mixing_models(
    root: Path, store: EmbeddingStore
) -> None:
    sha = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha)

    old_encoder = FakeEncoder(model_id="fake-v1")
    sync_embeddings_fn(root, ledger, old_encoder, store)
    assert old_encoder.calls == 1
    assert store.has(sha, "fake-v1")
    assert not store.has(sha, "fake-v2")

    new_encoder = FakeEncoder(model_id="fake-v2")
    encoded = sync_embeddings_fn(root, ledger, new_encoder, store)
    assert encoded == 1
    assert new_encoder.calls == 1  # its own encode, independent of old_encoder
    assert store.has(sha, "fake-v2")
    assert store.has(sha, "fake-v1")  # old rows untouched, never overwritten

    v1 = store.vectors_for("fake-v1", ["/a"])["/a"]
    v2 = store.vectors_for("fake-v2", ["/a"])["/a"]
    assert v1 == v2  # same fake vectors, but stored under distinct model keys
    assert store.has(sha, "fake-v1") and store.has(sha, "fake-v2")


def test_removed_entries_are_retained_not_swept(root: Path, store: EmbeddingStore) -> None:
    sha = _write_concept(root, "bundles/kb/a.md", "# A\nHello world")
    ledger = _empty_ledger(root)
    ledger.record("uri://a", "src", "bundles/kb/a.md", "rev1", sha)
    encoder = FakeEncoder()
    sync_embeddings_fn(root, ledger, encoder, store)

    ledger.sweep_removed(seen_uris=set(), source="src")  # flags uri://a as removed_at
    encoded = sync_embeddings_fn(root, ledger, encoder, store)
    assert encoded == 0
    assert encoder.calls == 1  # removed entries are skipped, not re-embedded
    assert store.has(sha, encoder.model_id)  # but the row is retained


def test_cosine_top_k_ranks_most_similar_first() -> None:
    vectors = {
        "/a": [1.0, 0.0],
        "/b": [0.0, 1.0],
        "/c": [0.9, 0.1],
    }
    ranked = cosine_top_k([1.0, 0.0], vectors, k=2)
    assert [cid for cid, _ in ranked] == ["/a", "/c"]


def test_store_upsert_normalizes_and_roundtrips(tmp_path: Path) -> None:
    store = EmbeddingStore(tmp_path / "e.db")
    store.upsert("sha1", "m1", "/a", [3.0, 4.0])  # norm = 5
    vector = store.vectors_for("m1", ["/a"])["/a"]
    assert vector[0] == pytest.approx(0.6, abs=1e-6)
    assert vector[1] == pytest.approx(0.8, abs=1e-6)


def test_store_set_concept_remaps_without_new_vector(tmp_path: Path) -> None:
    store = EmbeddingStore(tmp_path / "e.db")
    store.upsert("sha1", "m1", "/old", [1.0, 0.0])
    store.set_concept("sha1", "m1", "/new")
    assert store.vectors_for("m1", ["/new"]) and not store.vectors_for("m1", ["/old"])


def test_cli_sync_wires_embeddings_hook_when_configured(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: `okf-ingest sync` with an `embeddings:` config block
    reaches `sync_embeddings` via the negotiated `_post_sync` hook."""
    from conftest import git

    from okf_mcp import embeddings as embeddings_mod
    from okf_mcp.ingest.cli import main as ingest_main

    root = tmp_path / "knowledge"
    kb = root / "bundles" / "kb"
    kb.mkdir(parents=True)
    (kb / "index.md").write_text("---\ntype: Index\nscope_default: [public]\n---\n# KB\n")
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "init knowledge repo")
    (root / "ingest.yaml").write_text(
        "embeddings:\n  model: fake-cli-model\n  path: ingest/embeddings.db\n"
        "sources:\n"
        f"  - name: handbook\n    type: git\n    url: {source_repo}\n    target: kb\n"
    )
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)
    monkeypatch.setattr(embeddings_mod, "sentence_transformers_available", lambda: True)
    monkeypatch.setattr(embeddings_mod, "SentenceTransformerEncoder", FakeEncoder)

    assert ingest_main(["sync"]) == 0

    store = embeddings_mod.EmbeddingStore(root / "ingest" / "embeddings.db")
    vectors = store.vectors_for("fake-cli-model", ["/plain", "/notes/mrr-tips"])
    assert set(vectors) == {"/plain", "/notes/mrr-tips"}
    store.close()

    # a no-op second sync (unchanged content) must not touch the hook's
    # global no-op state — pure keyword sync still works unconfigured
    assert ingest_main(["sync"]) == 0
