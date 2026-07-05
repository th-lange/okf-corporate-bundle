"""Cross-bundle references (issue #31): bundle:/concept/id, scope-gated."""

from pathlib import Path

import pytest

from okf_mcp.index import DuplicateBundleError, OkfIndex, UnknownConceptError
from okf_mcp.parser import parse_document
from okf_mcp.validator import main as validate_main
from okf_mcp.validator import validate_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "acme-knowledge"
RESTRICTED = REPO_ROOT / "bundles" / "acme-knowledge-restricted"

CHURN_MODEL = "/methods/churn-propensity-model"


@pytest.fixture(scope="module")
def catalog() -> OkfIndex:
    return OkfIndex(BUNDLE, RESTRICTED)


def test_parser_extracts_qualified_links_but_not_urls(tmp_path: Path) -> None:
    (tmp_path / "doc.md").write_text(
        "---\ntype: Note\n---\n\n"
        "[local](/glossary/mrr) "
        "[cross](acme-knowledge:/metrics/logo-churn-rate) "
        "[web](https://example.com/page) "
        "[repo](git://host/repo) "
        "[table](bigquery://project/dataset/table)\n"
    )
    doc = parse_document(tmp_path, tmp_path / "doc.md")
    assert doc.links == ("/glossary/mrr", "acme-knowledge:/metrics/logo-churn-rate")


def test_exco_full_persona_crosses_bundles(catalog: OkfIndex) -> None:
    view = catalog.visible_to(["growth", "platform", "exco"])
    reached = {doc.id for doc, _, _ in view.follow_links(CHURN_MODEL)}
    assert "/metrics/logo-churn-rate" in reached  # restricted → internal edge
    assert "/teams/growth" in reached
    assert "/data/dim-account-raw" in reached  # same-bundle link still works


def test_cross_edge_is_gated_per_side(catalog: OkfIndex) -> None:
    # exco alone reads the model, but logo-churn-rate is growth-scoped:
    # the caller must be able to see BOTH sides for the edge to exist.
    view = catalog.visible_to(["exco"])
    reached = {doc.id for doc, _, _ in view.follow_links(CHURN_MODEL)}
    assert "/metrics/logo-churn-rate" not in reached
    assert "/teams/growth" in reached  # public target still reachable


def test_no_trace_without_the_restricted_scope(catalog: OkfIndex) -> None:
    with pytest.raises(UnknownConceptError):
        catalog.visible_to(["growth"]).follow_links(CHURN_MODEL)


def test_link_into_unloaded_bundle_is_inert(catalog: OkfIndex, tmp_path: Path) -> None:
    (tmp_path / "index.md").write_text("---\ntype: Index\n---\n# T\n")
    (tmp_path / "a.md").write_text(
        "---\ntype: Note\ntitle: A\ndescription: d\n---\n\n[ghost](other-bundle:/x)\n"
    )
    index = OkfIndex(tmp_path)  # loads fine
    assert index.follow_links("/a") == []  # the edge simply does not exist


def test_duplicate_bundle_names_fail_loudly(tmp_path: Path) -> None:
    for parent in ("one", "two"):
        root = tmp_path / parent / "kb"
        root.mkdir(parents=True)
        (root / "index.md").write_text("---\ntype: Index\n---\n# T\n")
    with pytest.raises(DuplicateBundleError, match="kb"):
        OkfIndex(tmp_path / "one" / "kb", tmp_path / "two" / "kb")


def test_demo_bundles_validate_together(capsys) -> None:
    assert validate_main([str(BUNDLE), str(RESTRICTED)]) == 0


def test_validator_cross_checks_only_within_the_run(tmp_path: Path) -> None:
    root = tmp_path / "satellite"
    root.mkdir()
    (root / "index.md").write_text("---\ntype: Index\n---\n# T\n")
    (root / "a.md").write_text(
        "---\ntype: Note\ntitle: A\ndescription: d\n---\n\n"
        "[bad](acme-knowledge:/no/such-concept) [ok](acme-knowledge:/glossary/mrr)\n"
    )
    # validated alone: qualified links are skipped — independently shippable
    assert validate_bundle(root) == []
    # validated with the named bundle: the broken target is a finding
    external = {"acme-knowledge": {d.id for d in map(
        lambda p: parse_document(BUNDLE, p), BUNDLE.rglob("*.md")
    )}}
    findings = [str(f) for f in validate_bundle(root, external)]
    assert any("dangling cross-bundle link: acme-knowledge:/no/such-concept" in f for f in findings)
    assert not any("/glossary/mrr" in f for f in findings)
