"""MCP server (stdio) exposing OKF bundles to agents, scoped per session.

Bundle selection: OKF_BUNDLE_DIRS (os.pathsep-separated list; OKF_BUNDLE_DIR
also accepted), defaulting to both demo bundles under `bundles/`.

Scope binding: the session's scope set is resolved once, at server start —
never from tool input, so prompt content can never widen visibility. OKF_TOKEN
is authenticated via the pluggable auth layer (config: OKF_AUTH_CONFIG,
default `config/auth.yaml`); without a token, OKF_SCOPES (comma-separated
labels) acts as a local dev override. Neither set means public-layer only.

Resource authorization: resolve_resource grants come from OKF_RESOURCE_CONFIG
(default `config/resources.yaml`); every call is audit-logged as JSONL to
OKF_AUDIT_LOG, or to the `okf_mcp.audit` logger when unset.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from okf_mcp.auth import ANONYMOUS, Authenticator, Principal, StaticTokenAuthenticator
from okf_mcp.authz import AuditLog, ResourceAuthorizer
from okf_mcp.index import OkfIndex, UnknownConceptError, full, summary

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_BUNDLES = (
    _REPO_ROOT / "bundles" / "acme-knowledge",
    _REPO_ROOT / "bundles" / "acme-knowledge-restricted",
)
_DEFAULT_AUTH_CONFIG = _REPO_ROOT / "config" / "auth.yaml"
_DEFAULT_RESOURCE_CONFIG = _REPO_ROOT / "config" / "resources.yaml"


def _resolve_principal(
    scopes: Iterable[str] | None,
    authenticator: Authenticator | None,
    token: str | None,
) -> Principal:
    if scopes is not None:
        return Principal(subject="session", scopes=frozenset(scopes))
    if token is None:
        token = os.environ.get("OKF_TOKEN") or None
    if token is not None:
        if authenticator is None:
            config = Path(os.environ.get("OKF_AUTH_CONFIG", _DEFAULT_AUTH_CONFIG))
            authenticator = StaticTokenAuthenticator.from_file(config)
        return authenticator.authenticate(token)
    raw = os.environ.get("OKF_SCOPES", "")
    override = frozenset(s.strip() for s in raw.split(",") if s.strip())
    return Principal(subject="local-dev", scopes=override) if override else ANONYMOUS


def build_server(
    bundle_dirs: Path | Sequence[Path] | None = None,
    scopes: Iterable[str] | None = None,
    authenticator: Authenticator | None = None,
    token: str | None = None,
    authorizer: ResourceAuthorizer | None = None,
    audit_log: AuditLog | None = None,
) -> FastMCP:
    if bundle_dirs is None:
        raw = os.environ.get("OKF_BUNDLE_DIRS") or os.environ.get("OKF_BUNDLE_DIR")
        bundle_dirs = [Path(p) for p in raw.split(os.pathsep)] if raw else _DEFAULT_BUNDLES
    if isinstance(bundle_dirs, Path):
        bundle_dirs = [bundle_dirs]
    principal = _resolve_principal(scopes, authenticator, token)
    if authorizer is None:
        config = Path(os.environ.get("OKF_RESOURCE_CONFIG", _DEFAULT_RESOURCE_CONFIG))
        authorizer = ResourceAuthorizer.from_file(config)
    if audit_log is None:
        audit_path = os.environ.get("OKF_AUDIT_LOG")
        audit_log = AuditLog(Path(audit_path) if audit_path else None)
    index = OkfIndex(*bundle_dirs).visible_to(principal.scopes)
    mcp = FastMCP(
        "okf-knowledge",
        instructions=(
            "Curated company knowledge in Open Knowledge Format (OKF). "
            "Concept ids are bundle-relative paths like "
            "/metrics/monthly-recurring-revenue. Use list_by_type or the id "
            "from another concept's links to find concepts, then get_concept "
            "for the authoritative definition. Results are limited to this "
            "session's authorization scope; concepts outside it do not exist "
            "from your point of view."
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
        limit: int = 20,
    ) -> list[dict]:
        """Search concepts by keyword, ranked, optionally narrowed by type and tags.

        Results are ranked by where terms hit (title/aliases > tags >
        description > body) and truncated to `limit`. Returns compact
        summaries (id/type/title/description) — fetch bodies via get_concept.
        An empty list means nothing matched.

        Args:
            query: Keywords; all terms must match (case-insensitive).
            concept_type: Optional exact type filter, e.g. "Metric".
            tags: Optional tag filter; matches concepts carrying any of these tags.
            limit: Maximum results to return (default 20).
        """
        return [summary(d) for d in index.search(query, concept_type, tags, limit)]

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

    def _audit(concept_id: str, decision: str, resource: str | None = None) -> None:
        event: dict[str, object] = {
            "tool": "resolve_resource",
            "subject": principal.subject,
            "scopes": sorted(principal.scopes),
            "concept_id": concept_id,
            "decision": decision,
        }
        if resource is not None:
            event["resource"] = resource
        audit_log.record(**event)

    @mcp.tool()
    def resolve_resource(concept_id: str) -> dict:
        """Resolve a concept's `resource:` URI, if this session is authorized.

        Resource access is separate from knowledge read access — being able
        to read about a table does not imply permission to query it. Every
        call is audit-logged, allowed or not.

        Args:
            concept_id: Bundle-relative id, e.g. "/metrics/monthly-recurring-revenue".
        """
        try:
            doc = index.get_concept(concept_id)
        except UnknownConceptError:
            _audit(concept_id, "unknown-concept")
            raise ValueError(f"Unknown concept id {concept_id!r}.") from None
        resource = doc.frontmatter.get("resource")
        if not isinstance(resource, str) or not resource:
            _audit(concept_id, "no-resource")
            raise ValueError(f"{concept_id} declares no resource.")
        if not authorizer.is_allowed(principal.scopes, resource):
            # The denial must not reveal the URI.
            _audit(concept_id, "deny", resource)
            raise ValueError(f"Access to the resource of {concept_id} is denied for this session.")
        _audit(concept_id, "allow", resource)
        return {"concept_id": concept_id, "resource": resource}

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
