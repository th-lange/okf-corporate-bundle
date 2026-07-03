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
    def list_by_type(concept_type: str) -> list[dict]:
        """List all concepts of a type (id/type/title/description only, no bodies).

        Args:
            concept_type: OKF type string, e.g. "Metric", "Runbook", "BigQuery Table".
        """
        return [summary(d) for d in index.list_by_type(concept_type)]

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
