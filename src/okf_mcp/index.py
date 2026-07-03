"""In-memory index over an OKF bundle, backing the MCP tools."""

from __future__ import annotations

from pathlib import Path

from okf_mcp.parser import Document, load_bundle


class UnknownConceptError(KeyError):
    """Raised when a concept id does not exist in the index."""


class OkfIndex:
    """Loads a bundle once and answers concept lookups.

    Only *concepts* are indexed for retrieval; reserved files (index.md,
    log.md) are parsed but not served as concepts.
    """

    def __init__(self, bundle_dir: Path) -> None:
        self.bundle_dir = bundle_dir
        documents = load_bundle(bundle_dir)
        self._concepts: dict[str, Document] = {d.id: d for d in documents if d.is_concept}

    def __len__(self) -> int:
        return len(self._concepts)

    def get_concept(self, concept_id: str) -> Document:
        doc = self._concepts.get(concept_id)
        if doc is None:
            raise UnknownConceptError(concept_id)
        return doc

    def list_by_type(self, concept_type: str) -> list[Document]:
        return [d for d in self._concepts.values() if d.type == concept_type]

    def types(self) -> list[str]:
        return sorted({d.type for d in self._concepts.values() if d.type})


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
