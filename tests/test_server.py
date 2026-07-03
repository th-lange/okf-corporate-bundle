from pathlib import Path

import pytest

from okf_mcp.index import OkfIndex, UnknownConceptError, full, summary
from okf_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "acme-knowledge"


@pytest.fixture(scope="module")
def index() -> OkfIndex:
    return OkfIndex(BUNDLE)


def test_index_counts_concepts_not_reserved_files(index: OkfIndex) -> None:
    # 25 md files minus 10 index.md (root + 9 directories) minus 1 log.md = 14 concepts
    assert len(index) == 14


def test_get_concept_returns_full_document(index: OkfIndex) -> None:
    doc = full(index.get_concept("/metrics/monthly-recurring-revenue"))
    assert doc["frontmatter"]["type"] == "Metric"
    assert "Monthly Recurring Revenue" in doc["body"]
    assert "/data/tables/fct-subscription-events" in doc["links"]


def test_get_concept_unknown_id_raises(index: OkfIndex) -> None:
    with pytest.raises(UnknownConceptError):
        index.get_concept("/nope/nothing")


def test_list_by_type_metrics(index: OkfIndex) -> None:
    metrics = [summary(d) for d in index.list_by_type("Metric")]
    assert {m["id"] for m in metrics} == {
        "/metrics/monthly-recurring-revenue",
        "/metrics/logo-churn-rate",
    }
    assert all("body" not in m for m in metrics)


def test_list_by_type_unknown_type_is_empty(index: OkfIndex) -> None:
    assert index.list_by_type("Nonexistent") == []


@pytest.mark.anyio
async def test_mcp_server_exposes_both_tools() -> None:
    server = build_server(BUNDLE)
    tools = {t.name for t in await server.list_tools()}
    assert tools == {"get_concept", "list_by_type"}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
