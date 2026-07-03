from pathlib import Path

import pytest

from okf_mcp.index import OkfIndex, summary

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "acme-knowledge"


@pytest.fixture(scope="module")
def index() -> OkfIndex:
    return OkfIndex(BUNDLE)


def test_query_mrr_finds_term_and_metric(index: OkfIndex) -> None:
    ids = {d.id for d in index.search("MRR")}
    assert "/glossary/mrr" in ids
    assert "/metrics/monthly-recurring-revenue" in ids


def test_type_facet_narrows(index: OkfIndex) -> None:
    # Both metrics mention MRR (churn links to it); the facet keeps only Metrics
    results = index.search("MRR", concept_type="Metric")
    assert {d.id for d in results} == {
        "/metrics/monthly-recurring-revenue",
        "/metrics/logo-churn-rate",
    }
    assert all(d.type == "Metric" for d in results)
    # A more specific query pins the single canonical concept
    assert {d.id for d in index.search("canonical company-wide", concept_type="Metric")} == {
        "/metrics/monthly-recurring-revenue"
    }


def test_tags_facet_matches_any(index: OkfIndex) -> None:
    # retention -> churn metric; oncall -> the runbook AND the platform team
    ids = {d.id for d in index.search("", tags=["retention", "oncall"])}
    assert ids == {
        "/metrics/logo-churn-rate",
        "/runbooks/mrr-discrepancy",
        "/teams/platform",
    }


def test_all_terms_must_match(index: OkfIndex) -> None:
    assert index.search("churn zeppelin") == []


def test_empty_result_is_list_not_error(index: OkfIndex) -> None:
    assert index.search("zzz-no-such-word") == []


def test_result_shape_excludes_body(index: OkfIndex) -> None:
    results = [summary(d) for d in index.search("MRR")]
    assert results and all(set(r) == {"id", "type", "title", "description"} for r in results)
