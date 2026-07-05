"""In-memory index over OKF bundles, backing the MCP tools."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from okf_mcp.parser import Document, load_bundle
from okf_mcp.scopes import effective_scopes, is_visible


class UnknownConceptError(KeyError):
    """Raised when a concept id does not exist in the index."""


class DuplicateConceptError(ValueError):
    """Raised when the same concept id appears in more than one bundle."""


class DuplicateBundleError(ValueError):
    """Raised when two loaded bundles share a name (the qualified-link prefix)."""


class OkfIndex:
    """Loads one or more bundles once and answers concept lookups.

    Only *concepts* are indexed for retrieval; reserved files (index.md,
    log.md) are parsed but not served as concepts. The full index is the
    catalog; `visible_to` derives the per-session view that every serving
    path must go through.
    """

    def __init__(self, *bundle_dirs: Path) -> None:
        self.bundle_dirs = tuple(bundle_dirs)
        names = [Path(d).name for d in bundle_dirs]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise DuplicateBundleError(
                f"bundle names must be unique (qualified links resolve by name): "
                f"{', '.join(duplicates)}"
            )
        self._bundle_names = frozenset(names)
        self._concepts: dict[str, Document] = {}
        self._scopes: dict[str, frozenset[str]] = {}
        for root in bundle_dirs:
            documents = load_bundle(root)
            scopes = effective_scopes(documents)
            for doc in documents:
                if not doc.is_concept:
                    continue
                if doc.id in self._concepts:
                    raise DuplicateConceptError(
                        f"concept id {doc.id!r} appears in more than one bundle"
                    )
                self._concepts[doc.id] = doc
                self._scopes[doc.id] = scopes[doc.id]

    def visible_to(self, caller_scopes: Iterable[str]) -> OkfIndex:
        """The per-session view: concepts outside the caller's scopes are
        omitted entirely — they cannot be listed, searched, retrieved, or
        reached via follow_links, and lookups fail exactly like missing ids.
        """
        caller = frozenset(caller_scopes)
        view = OkfIndex()
        view.bundle_dirs = self.bundle_dirs
        view._bundle_names = self._bundle_names
        view._concepts = {
            cid: doc
            for cid, doc in self._concepts.items()
            if is_visible(self._scopes[cid], caller)
        }
        view._scopes = {cid: self._scopes[cid] for cid in view._concepts}
        return view

    def __len__(self) -> int:
        return len(self._concepts)

    def ids(self) -> list[str]:
        return sorted(self._concepts)

    def effective_scope(self, concept_id: str) -> frozenset[str]:
        self.get_concept(concept_id)
        return self._scopes[concept_id]

    def get_concept(self, concept_id: str) -> Document:
        doc = self._concepts.get(concept_id)
        if doc is None:
            raise UnknownConceptError(concept_id)
        return doc

    def list_by_type(self, concept_type: str) -> list[Document]:
        return [d for d in self._concepts.values() if d.type == concept_type]

    def types(self) -> list[str]:
        return sorted({d.type for d in self._concepts.values() if d.type})

    def follow_links(
        self, concept_id: str, depth: int = 1
    ) -> list[tuple[Document, int, str]]:
        """Breadth-first traversal of outbound links from a concept.

        Returns (document, hop_distance, via_id) triples for every distinct
        concept reachable within `depth` hops, excluding the start concept.
        Cycle-safe: each concept appears at most once, at its shortest
        distance. Links pointing at reserved files (directory indexes) or
        outside the served bundles are skipped, and qualified cross-bundle
        links (`bundle:/concept/id`) are followed only when that bundle is
        loaded and the target is within this view.
        """
        start = self.get_concept(concept_id)
        seen = {start.id}
        frontier = [start]
        reached: list[tuple[Document, int, str]] = []
        for hop in range(1, max(depth, 0) + 1):
            next_frontier: list[Document] = []
            for doc in frontier:
                for target_id in doc.links:
                    resolved = self._resolve_link(target_id)
                    if resolved is None or resolved in seen:
                        continue
                    seen.add(resolved)
                    target = self._concepts.get(resolved)
                    if target is None:
                        continue  # directory index, log, dangling, or out of scope
                    reached.append((target, hop, doc.id))
                    next_frontier.append(target)
            frontier = next_frontier
        return reached

    def _resolve_link(self, target: str) -> str | None:
        """Bundle-absolute targets pass through; qualified `bundle:/id` targets
        resolve into the flat namespace only when that bundle is loaded —
        links into bundles this session does not serve simply do not exist.
        """
        if target.startswith("/"):
            return target
        bundle, sep, rest = target.partition(":")
        if sep and bundle in self._bundle_names and rest.startswith("/"):
            return rest
        return None

    def search(
        self,
        query: str,
        concept_type: str | None = None,
        tags: list[str] | None = None,
        limit: int = 20,
    ) -> list[Document]:
        """Ranked keyword search with optional facets and a result limit.

        Every whitespace-separated term must occur (case-insensitive) in at
        least one field. Results rank by where terms hit — title and curated
        `aliases:` outrank tags, then description, then body — with the
        concept id as a stable tiebreak. `concept_type` filters exactly;
        `tags` matches if any requested tag is present; at most `limit`
        results are returned so responses stay small at any corpus size.
        """
        terms = [t.lower() for t in query.split() if t]
        scored: list[tuple[float, str, Document]] = []
        for doc in self._concepts.values():
            if concept_type is not None and doc.type != concept_type:
                continue
            if tags:
                doc_tags = doc.frontmatter.get("tags") or []
                if not set(tags) & set(doc_tags):
                    continue
            fields = _weighted_fields(doc)
            score = 0.0
            for term in terms:
                best = max((w for w, text in fields if term in text), default=0.0)
                if best == 0.0:
                    break
                score += best
            else:
                scored.append((-score, doc.id, doc))
        scored.sort()
        return [doc for _, _, doc in scored[: max(limit, 0)]]


def _weighted_fields(doc: Document) -> tuple[tuple[float, str], ...]:
    """(weight, lowercased text) pairs — the ranking order for search."""
    title = str(doc.frontmatter.get("title") or "")
    aliases = doc.frontmatter.get("aliases") or []
    tags = doc.frontmatter.get("tags") or []
    return (
        (3.0, " ".join([title, *map(str, aliases)]).lower()),
        (2.0, " ".join(map(str, tags)).lower()),
        (1.5, str(doc.frontmatter.get("description") or "").lower()),
        (1.0, doc.body.lower()),
    )


def summary(doc: Document) -> dict:
    """The compact shape returned by list-style tools (no body)."""
    return {
        "id": doc.id,
        "type": doc.type,
        "title": doc.frontmatter.get("title"),
        "description": doc.frontmatter.get("description"),
    }


def full(doc: Document) -> dict:
    """The complete shape returned by get_concept."""
    return {
        "id": doc.id,
        "frontmatter": {k: str(v) if not isinstance(v, (str, int, float, bool, list)) else v
                        for k, v in doc.frontmatter.items()},
        "body": doc.body,
        "links": list(doc.links),
    }
