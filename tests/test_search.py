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


# --- aliases, ranking, limits (issue #28) -----------------------------------


def test_alias_match_finds_concept(index: OkfIndex) -> None:
    results = index.search("customer cancellations")
    assert results and results[0].id == "/metrics/logo-churn-rate"


def test_title_match_ranks_above_body_only_match(index: OkfIndex) -> None:
    ids = [d.id for d in index.search("churn")]
    # "churn" is in logo-churn-rate's title but only in dim-account's body
    assert ids.index("/metrics/logo-churn-rate") < ids.index("/data/tables/dim-account")


def test_limit_truncates_results(index: OkfIndex) -> None:
    assert len(index.search("")) == 14  # whole bundle fits the default limit
    assert len(index.search("", limit=3)) == 3
    assert index.search("", limit=0) == []


def test_alias_match_respects_scope(index: OkfIndex) -> None:
    # logo-churn-rate is growth-scoped; an alias hit must not leak it
    public = index.visible_to([])
    assert all(d.id != "/metrics/logo-churn-rate" for d in public.search("customer cancellations"))
    growth = index.visible_to(["growth"])
    assert any(d.id == "/metrics/logo-churn-rate" for d in growth.search("customer cancellations"))


def test_validator_flags_malformed_aliases(tmp_path: Path) -> None:
    from okf_mcp.validator import validate_bundle

    (tmp_path / "index.md").write_text("---\ntype: Index\n---\n# T\n")
    (tmp_path / "bad.md").write_text("---\ntype: Term\naliases: [ok, 3]\n---\n# Bad\n")
    findings = [str(f) for f in validate_bundle(tmp_path)]
    assert any("aliases" in f for f in findings)
