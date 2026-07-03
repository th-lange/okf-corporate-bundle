"""MCP server (stdio) exposing an OKF bundle to agents.

Bundle selection: the OKF_BUNDLE_DIR environment variable, defaulting to
`bundles/acme-knowledge` relative to the repository root.
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from okf_mcp.index import OkfIndex, UnknownConceptError, full, summary

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BUNDLE = _REPO_ROOT / "bundles" / "acme-knowledge"


def build_server(bundle_dir: Path | None = None) -> FastMCP:
    bundle_dir = bundle_dir or Path(os.environ.get("OKF_BUNDLE_DIR", _DEFAULT_BUNDLE))
    index = OkfIndex(bundle_dir)
    mcp = FastMCP(
        "okf-knowledge",
        instructions=(
            "Curated company knowledge in Open Knowledge Format (OKF). "
            "Concept ids are bundle-relative paths like "
            "/metrics/monthly-recurring-revenue. Use list_by_type or the id "
            "from another concept's links to find concepts, then get_concept "
            "for the authoritative definition."
        ),
    )

    @mcp.tool()
    def get_concept(concept_id: str) -> dict:
        """Return one concept's full frontmatter, markdown body, and outbound links.

        Args:
            concept_id: Bundle-relative id, e.g. "/metrics/monthly-recurring-revenue".
        """
        try:
            return full(index.get_concept(concept_id))
        except UnknownConceptError:
            known_types = ", ".join(index.types())
            raise ValueError(
                f"Unknown concept id {concept_id!r}. Ids are bundle-relative paths "
                f"like /glossary/mrr. Available types: {known_types}."
            ) from None

    @mcp.tool()
    def search_concepts(
        query: str,
        concept_type: str | None = None,
        tags: list[str] | None = None,
    ) -> list[dict]:
        """Search concepts by keyword, optionally narrowed by type and tags.

        Returns compact summaries (id/type/title/description) — fetch bodies
        via get_concept. An empty list means nothing matched.

        Args:
            query: Keywords; all terms must match (case-insensitive).
            concept_type: Optional exact type filter, e.g. "Metric".
            tags: Optional tag filter; matches concepts carrying any of these tags.
        """
        return [summary(d) for d in index.search(query, concept_type, tags)]

    @mcp.tool()
    def list_by_type(concept_type: str) -> list[dict]:
        """List all concepts of a type (id/type/title/description only, no bodies).

        Args:
            concept_type: OKF type string, e.g. "Metric", "Runbook", "BigQuery Table".
        """
        return [summary(d) for d in index.list_by_type(concept_type)]

    @mcp.tool()
    def follow_links(concept_id: str, depth: int = 1) -> list[dict]:
        """Traverse the knowledge graph outward from a concept.

        Returns every distinct concept reachable within `depth` link-hops as a
        summary plus `hops` (shortest distance) and `via` (the concept whose
        link reached it). Use this to gather a whole context subgraph — e.g.
        a metric's backing table, owning team, and runbook — in one call.

        Args:
            concept_id: Bundle-relative id to start from, e.g. "/glossary/mrr".
            depth: Maximum link-hops to follow (default 1).
        """
        try:
            reached = index.follow_links(concept_id, depth)
        except UnknownConceptError:
            raise ValueError(
                f"Unknown concept id {concept_id!r}. Ids are bundle-relative "
                f"paths like /glossary/mrr."
            ) from None
        return [{**summary(doc), "hops": hops, "via": via} for doc, hops, via in reached]

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
