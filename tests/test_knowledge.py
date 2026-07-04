"""Operator/knowledge separation (issue #30): the external knowledge root."""

from pathlib import Path

import pytest

from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.knowledge import KnowledgeRootError, discover_bundles, knowledge_root
from okf_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[1]

BUNDLE_INDEX = """---
type: Index
title: External KB
scope_default: [public]
---

# External KB
"""

CONCEPT = """---
type: Term
title: Widget
description: The thing we sell.
---

# Widget
"""


@pytest.fixture()
def kroot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "knowledge"
    kb = root / "bundles" / "external-kb"
    kb.mkdir(parents=True)
    (kb / "index.md").write_text(BUNDLE_INDEX)
    (kb / "widget.md").write_text(CONCEPT)
    (root / "bundles" / "not-a-bundle").mkdir()  # no index.md → ignored
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)
    return root


def test_no_env_means_no_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OKF_KNOWLEDGE_ROOT", raising=False)
    assert knowledge_root() is None


def test_bad_root_fails_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", "/no/such/place")
    with pytest.raises(KnowledgeRootError, match="not a directory"):
        knowledge_root()


def test_discovery_finds_only_bundles(kroot: Path) -> None:
    found = discover_bundles(kroot)
    assert [p.name for p in found] == ["external-kb"]


def test_empty_root_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(KnowledgeRootError, match="no bundles"):
        discover_bundles(tmp_path)


@pytest.mark.anyio
async def test_server_serves_the_external_root(kroot: Path) -> None:
    server = build_server(scopes=[])
    result = await server.call_tool("get_concept", {"concept_id": "/widget"})
    assert "The thing we sell." in result[0].text
    # the demo fixtures are NOT loaded when a root is configured
    with pytest.raises(Exception, match="Unknown concept id"):
        await server.call_tool("get_concept", {"concept_id": "/glossary/mrr"})


@pytest.mark.anyio
async def test_explicit_bundle_dirs_beat_the_root(
    kroot: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OKF_BUNDLE_DIRS", str(REPO_ROOT / "bundles" / "acme-knowledge"))
    server = build_server(scopes=[])
    result = await server.call_tool("get_concept", {"concept_id": "/glossary/mrr"})
    assert "MRR" in result[0].text


def test_ingest_state_lands_in_the_root(
    kroot: Path, source_repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # default config = <root>/ingest.yaml; relative paths resolve into the root
    (kroot / "ingest.yaml").write_text(
        "sources:\n" f"  - name: handbook\n    type: git\n    url: {source_repo}\n"
    )
    monkeypatch.chdir(kroot / "bundles")  # anywhere that isn't the root or repo
    assert ingest_main(["run"]) == 0
    assert (kroot / "ingest" / "drafts" / "handbook" / "plain.md").exists()
    assert (kroot / "ingest" / "ledger.yaml").exists()
    # nothing written into the operator repo
    assert not (REPO_ROOT / "ingest").exists()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
