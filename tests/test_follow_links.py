from pathlib import Path

import pytest

from okf_mcp.index import OkfIndex, UnknownConceptError

REPO_ROOT = Path(__file__).resolve().parents[1]
BUNDLE = REPO_ROOT / "bundles" / "acme-knowledge"

MRR = "/metrics/monthly-recurring-revenue"


@pytest.fixture(scope="module")
def index() -> OkfIndex:
    return OkfIndex(BUNDLE)


def write(root: Path, rel: str, links: list[str]) -> None:
    body = "\n".join(f"[{link}]({link})" for link in links)
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntype: Term\n---\n\n{body}\n", encoding="utf-8")


def test_depth_1_reaches_direct_neighbours(index: OkfIndex) -> None:
    reached = {doc.id for doc, hops, _ in index.follow_links(MRR, depth=1)}
    assert {
        "/glossary/mrr",
        "/data/tables/fct-subscription-events",
        "/teams/growth",
        "/runbooks/mrr-discrepancy",
    } <= reached


def test_depth_2_reaches_billing_service_via_the_table(index: OkfIndex) -> None:
    by_id = {doc.id: (hops, via) for doc, hops, via in index.follow_links(MRR, depth=2)}
    hops, via = by_id["/systems/billing-service"]
    assert hops == 2
    assert via == "/data/tables/fct-subscription-events"


def test_each_concept_appears_once_at_shortest_distance(index: OkfIndex) -> None:
    reached = index.follow_links(MRR, depth=3)
    ids = [doc.id for doc, _, _ in reached]
    assert len(ids) == len(set(ids))
    assert MRR not in ids  # start concept excluded even though cycles point back


def test_cycles_terminate(tmp_path: Path) -> None:
    write(tmp_path, "a.md", ["/b"])
    write(tmp_path, "b.md", ["/c"])
    write(tmp_path, "c.md", ["/a"])  # closes the cycle
    reached = OkfIndex(tmp_path).follow_links("/a", depth=10)
    assert [(d.id, hops) for d, hops, _ in reached] == [("/b", 1), ("/c", 2)]


def test_unknown_start_raises(index: OkfIndex) -> None:
    with pytest.raises(UnknownConceptError):
        index.follow_links("/nope", depth=1)


def test_depth_zero_reaches_nothing(index: OkfIndex) -> None:
    assert index.follow_links(MRR, depth=0) == []
