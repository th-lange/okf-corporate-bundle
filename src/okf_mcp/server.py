"""MCP server (stdio) exposing OKF bundles to agents, scoped per session.

Bundle selection, in order: an explicit OKF_BUNDLE_DIRS list
(os.pathsep-separated; OKF_BUNDLE_DIR also accepted); otherwise every bundle
under `$OKF_KNOWLEDGE_ROOT/bundles/`; otherwise the demo fixtures bundled in
this repo. The operator never requires knowledge inside its own tree — see
okf_mcp.knowledge for the operator/knowledge separation.

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
from okf_mcp.embeddings import (
    Encoder,
    EmbeddingStore,
    SentenceTransformerEncoder,
    default_store_path,
    sentence_transformers_available,
)
from okf_mcp.index import OkfIndex, UnknownConceptError, full, summary
from okf_mcp.knowledge import REPO_ROOT, discover_bundles, knowledge_root
from okf_mcp.writeback import ProposalError
from okf_mcp.writeback import propose_upstream as propose_upstream_change

_DEFAULT_BUNDLES = (
    REPO_ROOT / "bundles" / "acme-knowledge",
    REPO_ROOT / "bundles" / "acme-knowledge-restricted",
)
_DEFAULT_AUTH_CONFIG = REPO_ROOT / "config" / "auth.yaml"
_DEFAULT_RESOURCE_CONFIG = REPO_ROOT / "config" / "resources.yaml"


def _default_bundle_dirs() -> Sequence[Path]:
    raw = os.environ.get("OKF_BUNDLE_DIRS") or os.environ.get("OKF_BUNDLE_DIR")
    if raw:
        return [Path(p) for p in raw.split(os.pathsep)]
    root = knowledge_root()
    if root is not None:
        return discover_bundles(root)
    return _DEFAULT_BUNDLES


def _resolve_semantic_search(
    encoder: Encoder | None,
) -> tuple[Encoder | None, EmbeddingStore | None]:
    """The (encoder, store) pair `search_concepts` augments keyword ranking
    with, or `(None, None)` when semantic search stays off.

    Production default: a store must exist at the default path under
    `$OKF_KNOWLEDGE_ROOT` (populated by `okf_mcp.embeddings.sync_embeddings`
    during sync) and an encoder must be available — `sentence-transformers`
    installed (the `semantic` extra) unless one was injected. No knowledge
    root, no store file, or the extra missing all mean semantic search is
    off and `search_concepts` behaves exactly like keyword-only search.
    """
    root = knowledge_root()
    if root is None:
        return None, None
    store_path = default_store_path(root)
    if not store_path.is_file():
        return None, None
    if encoder is None:
        if not sentence_transformers_available():
            return None, None
        encoder = SentenceTransformerEncoder()
    return encoder, EmbeddingStore(store_path)


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
    encoder: Encoder | None = None,
) -> FastMCP:
    if bundle_dirs is None:
        bundle_dirs = _default_bundle_dirs()
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
    semantic_encoder, semantic_store = _resolve_semantic_search(encoder)
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

        When semantic search is configured (a persistent embedding store
        exists and an encoder is available), results are augmented after
        keyword ranking: keyword hits keep today's order unchanged, then any
        semantically-similar concepts not already among them are appended,
        best cosine-similarity first, until `limit` is reached. Semantic
        lookups never reach outside this session's visible concepts — only
        already-scoped ids are ever queried against the vector store.
        Without a configured store/encoder, behaviour is identical to
        keyword-only search.

        Args:
            query: Keywords; all terms must match (case-insensitive).
            concept_type: Optional exact type filter, e.g. "Metric".
            tags: Optional tag filter; matches concepts carrying any of these tags.
            limit: Maximum results to return (default 20).
        """
        hits = index.search(query, concept_type, tags, limit)
        if semantic_encoder is not None and semantic_store is not None and len(hits) < limit:
            seen = {doc.id for doc in hits}
            visible_ids = index.ids()
            query_vector = semantic_encoder.encode([query])[0]
            candidates = semantic_store.top_k(
                semantic_encoder.model_id, visible_ids, query_vector, len(visible_ids)
            )
            for concept_id, _score in candidates:
                if len(hits) >= limit:
                    break
                if concept_id in seen:
                    continue
                try:
                    doc = index.get_concept(concept_id)
                except UnknownConceptError:
                    continue
                if concept_type is not None and doc.type != concept_type:
                    continue
                if tags and not set(tags) & set(doc.frontmatter.get("tags") or []):
                    continue
                hits.append(doc)
                seen.add(concept_id)
        return [summary(d) for d in hits]

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

    def _audit(
        concept_id: str, decision: str, resource: str | None = None, tool: str = "resolve_resource"
    ) -> None:
        event: dict[str, object] = {
            "tool": tool,
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

    @mcp.tool()
    def propose_upstream(concept_id: str, updated_markdown: str, rationale: str) -> dict:
        """Propose an update to a concept's UPSTREAM source — the owning
        sector's repository, resolved from the concept's provenance.

        Nothing is written to the knowledge tree and nothing is merged: for
        git sources the proposal becomes a branch in the sector's repo,
        authored as this session's principal, for the sector's own review;
        non-git sources get a recorded suggestion artifact. Scope fields are
        rejected; a changed resource URI must appear verbatim in the
        rationale. Every call is audit-logged.

        Args:
            concept_id: Bundle-relative id of the concept to improve.
            updated_markdown: The full replacement document (frontmatter + body).
            rationale: Why this change is right — becomes the commit message.
        """
        try:
            doc = index.get_concept(concept_id)
        except UnknownConceptError:
            _audit(concept_id, "unknown-concept", tool="propose_upstream")
            raise ValueError(f"Unknown concept id {concept_id!r}.") from None
        try:
            result = propose_upstream_change(
                doc, updated_markdown, rationale, principal.subject, knowledge_root()
            )
        except ProposalError as exc:
            _audit(concept_id, "deny", tool="propose_upstream")
            raise ValueError(str(exc)) from None
        _audit(concept_id, "allow", result.ref, tool="propose_upstream")
        return {"kind": result.kind, "ref": result.ref, "pushed": result.pushed}

    return mcp


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
