"""Visibility matrix for the set-based scope model (issue #6).

Scope layout of the demo bundles:
- acme-knowledge root default: public
- metrics/ default: growth — but MRR is explicitly scope: [public]
- data/ default: growth, platform (tables/ has no index.md → nearest ancestor)
- systems/ default: platform
- runbooks/ default: growth, platform
- acme-knowledge-restricted root default: exco
"""

from pathlib import Path

import pytest

from okf_mcp.index import OkfIndex, UnknownConceptError

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "acme-knowledge"
RESTRICTED = REPO_ROOT / "bundles" / "acme-knowledge-restricted"

PUBLIC_IDS = {
    "/decisions/0001-mrr-single-source-of-truth",
    "/glossary/active-account",
    "/glossary/mrr",
    "/metrics/monthly-recurring-revenue",
    "/playbooks/define-a-new-metric",
    "/policies/data-classification",
    "/teams/growth",
    "/teams/platform",
}
GROWTH_ONLY_IDS = {"/metrics/logo-churn-rate"}
PLATFORM_ONLY_IDS = {"/systems/billing-service", "/systems/billing-api"}
GROUP_SHARED_IDS = {
    "/data/tables/fct-subscription-events",
    "/data/tables/dim-account",
    "/runbooks/mrr-discrepancy",
}
RESTRICTED_IDS = {
    "/access",
    "/data/dim-account-raw",
    "/methods/churn-propensity-model",
    "/methods/dynamic-pricing-engine",
    "/patents/us-11234567-adaptive-ingestion",
}


@pytest.fixture(scope="module")
def catalog() -> OkfIndex:
    return OkfIndex(BUNDLE, RESTRICTED)


# --- effective scope resolution -------------------------------------------


def test_directory_default_applies(catalog: OkfIndex) -> None:
    assert catalog.effective_scope("/metrics/logo-churn-rate") == {"growth"}


def test_concept_scope_overrides_directory_default(catalog: OkfIndex) -> None:
    # metrics/ defaults to growth; MRR declares scope: [public]
    assert catalog.effective_scope("/metrics/monthly-recurring-revenue") == {"public"}


def test_nearest_ancestor_index_wins(catalog: OkfIndex) -> None:
    # data/tables/ has no index.md → falls through to data/index.md
    assert catalog.effective_scope("/data/tables/fct-subscription-events") == {
        "growth",
        "platform",
    }


def test_bundle_root_default_is_fallback(catalog: OkfIndex) -> None:
    assert catalog.effective_scope("/glossary/mrr") == {"public"}
    assert catalog.effective_scope("/access") == {"exco"}


# --- visibility matrix ------------------------------------------------------


def test_public_caller_sees_only_public(catalog: OkfIndex) -> None:
    assert set(catalog.visible_to([]).ids()) == PUBLIC_IDS


def test_growth_sees_growth_plus_public(catalog: OkfIndex) -> None:
    expected = PUBLIC_IDS | GROWTH_ONLY_IDS | GROUP_SHARED_IDS
    assert set(catalog.visible_to(["growth"]).ids()) == expected


def test_platform_sees_platform_plus_public(catalog: OkfIndex) -> None:
    expected = PUBLIC_IDS | PLATFORM_ONLY_IDS | GROUP_SHARED_IDS
    assert set(catalog.visible_to(["platform"]).ids()) == expected


def test_both_groups_see_whole_internal_bundle(catalog: OkfIndex) -> None:
    view = catalog.visible_to(["growth", "platform"])
    assert set(view.ids()) == PUBLIC_IDS | GROWTH_ONLY_IDS | PLATFORM_ONLY_IDS | GROUP_SHARED_IDS


def test_exco_scope_unlocks_restricted_bundle(catalog: OkfIndex) -> None:
    assert set(catalog.visible_to(["exco"]).ids()) == PUBLIC_IDS | RESTRICTED_IDS


def test_restricted_bundle_invisible_without_its_scope(catalog: OkfIndex) -> None:
    for scopes in ([], ["growth"], ["growth", "platform"]):
        assert set(catalog.visible_to(scopes).ids()) & RESTRICTED_IDS == set()


# --- enforcement across every access path ----------------------------------


def test_out_of_scope_get_concept_is_indistinguishable_from_missing(
    catalog: OkfIndex,
) -> None:
    view = catalog.visible_to([])
    with pytest.raises(UnknownConceptError):
        view.get_concept("/systems/billing-service")
    with pytest.raises(UnknownConceptError):
        view.get_concept("/no/such/concept")


def test_search_respects_scope(catalog: OkfIndex) -> None:
    growth_hits = {d.id for d in catalog.visible_to(["growth"]).search("churn")}
    assert "/metrics/logo-churn-rate" in growth_hits
    assert "/methods/churn-propensity-model" not in growth_hits

    exco_hits = {d.id for d in catalog.visible_to(["exco"]).search("churn")}
    assert "/methods/churn-propensity-model" in exco_hits
    assert "/metrics/logo-churn-rate" not in exco_hits


def test_list_by_type_respects_scope(catalog: OkfIndex) -> None:
    assert catalog.visible_to([]).list_by_type("Service") == []
    assert catalog.visible_to(["growth"]).list_by_type("Method") == []


def test_follow_links_never_leaks_out_of_scope_nodes(catalog: OkfIndex) -> None:
    for scopes in ([], ["growth"], ["platform"], ["exco"]):
        view = catalog.visible_to(scopes)
        visible = set(view.ids())
        reached = view.follow_links("/metrics/monthly-recurring-revenue", depth=5)
        assert {doc.id for doc, _, _ in reached} <= visible
        assert {via for _, _, via in reached} <= visible | {"/metrics/monthly-recurring-revenue"}


def test_follow_links_does_not_traverse_through_hidden_nodes(catalog: OkfIndex) -> None:
    # growth sees fct-subscription-events, which links billing-service
    # (platform-only); the hidden node must be neither reached nor a bridge.
    reached = catalog.visible_to(["growth"]).follow_links(
        "/metrics/monthly-recurring-revenue", depth=5
    )
    ids = {doc.id for doc, _, _ in reached}
    assert "/data/tables/fct-subscription-events" in ids
    assert "/systems/billing-service" not in ids
    assert "/systems/billing-api" not in ids
