"""Semantic-aware serving (issue #45): search_concepts + the scope guarantee.

Semantic hits must never bypass `OkfIndex.visible_to` — a concept outside
the caller's scopes must be unreachable via vector similarity even when its
vector is the closest match to the query.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from okf_mcp.embeddings import EmbeddingStore, default_store_path
from okf_mcp.index import OkfIndex, summary
from okf_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "acme-knowledge"

_INDEX_MD = """---
type: Index
scope_default: [public]
---

# KB
"""

_PUBLIC_CONCEPT = """---
type: Term
title: Widget
description: A public gadget.
---

# Widget
"""

_RESTRICTED_CONCEPT = """---
type: Term
title: Secret Plan
scope: [exco]
---

# Secret Plan
"""

_NO_KEYWORD_HIT_QUERY = "zzzznonmatchingquery"


class FixedEncoder:
    """Always returns the same vector — isolates the scope guarantee from
    any particular hashing/similarity behaviour."""

    def __init__(self, vector: list[float], model_id: str = "fixed-v1") -> None:
        self.model_id = model_id
        self._vector = vector

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vector) for _ in texts]


@pytest.fixture()
def scoped_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "knowledge"
    kb = root / "bundles" / "kb"
    (kb / "public").mkdir(parents=True)
    (kb / "restricted").mkdir(parents=True)
    (kb / "index.md").write_text(_INDEX_MD)
    (kb / "public" / "widget.md").write_text(_PUBLIC_CONCEPT)
    (kb / "restricted" / "secret.md").write_text(_RESTRICTED_CONCEPT)

    store = EmbeddingStore(default_store_path(root))
    # the restricted concept is the exact match for the fixed query vector;
    # the public concept is orthogonal (a worse match)
    store.upsert("sha-secret", "fixed-v1", "/restricted/secret", [1.0, 0.0])
    store.upsert("sha-widget", "fixed-v1", "/public/widget", [0.0, 1.0])
    store.close()

    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)
    return root


@pytest.mark.anyio
async def test_semantic_hit_outside_scope_never_surfaces(scoped_root: Path) -> None:
    kb = scoped_root / "bundles" / "kb"
    encoder = FixedEncoder([1.0, 0.0])  # closest to the restricted concept

    public_server = build_server(bundle_dirs=kb, scopes=[], encoder=encoder)
    result = await public_server.call_tool(
        "search_concepts", {"query": _NO_KEYWORD_HIT_QUERY, "limit": 5}
    )
    ids = {json.loads(item.text)["id"] for item in result[0]}
    assert ids == {"/public/widget"}  # not the closer, but out-of-scope, secret

    exco_server = build_server(bundle_dirs=kb, scopes=["exco"], encoder=encoder)
    result = await exco_server.call_tool(
        "search_concepts", {"query": _NO_KEYWORD_HIT_QUERY, "limit": 5}
    )
    ids = {json.loads(item.text)["id"] for item in result[0]}
    assert "/restricted/secret" in ids  # visible once scoped in


@pytest.mark.anyio
async def test_search_without_encoder_or_store_matches_keyword_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OKF_KNOWLEDGE_ROOT", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)

    baseline = [summary(d) for d in OkfIndex(BUNDLE).visible_to([]).search("MRR", limit=10)]

    server = build_server(BUNDLE, scopes=[])  # no encoder injected, no knowledge root
    result = await server.call_tool("search_concepts", {"query": "MRR", "limit": 10})
    assert [json.loads(item.text) for item in result[0]] == baseline


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
