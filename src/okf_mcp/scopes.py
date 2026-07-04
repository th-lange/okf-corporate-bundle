"""Set-based scope model: effective-scope resolution and visibility.

Every concept has an *effective scope* — a set of scope labels — resolved from
frontmatter with layered defaults:

    concept `scope:`                              if set
    else nearest ancestor index.md `scope_default:`
    else bundle root index.md `scope_default:`
    else {"public"}

Enforcement is pure set intersection: a concept is visible when its effective
scope contains "public" or intersects the caller's scope set. Organisational
layers (public / group / inner-exco) are realised purely through scope-set
*assignment* — a broader caller simply holds more scopes; there is no
hierarchy logic anywhere in enforcement.
"""

from __future__ import annotations

from okf_mcp.parser import Document

PUBLIC = "public"


def declared_scopes(frontmatter: dict, field: str) -> frozenset[str] | None:
    """Read a scope list from frontmatter; a bare string counts as one label.

    Returns None when the field is absent or malformed — malformed values are
    the validator's job to report; resolution treats them as unset so a typo
    can only ever *narrow* nothing (the layered default still applies).
    """
    value = frontmatter.get(field)
    if value is None:
        return None
    if isinstance(value, str):
        value = [value]
    if isinstance(value, list) and value and all(isinstance(v, str) and v for v in value):
        return frozenset(value)
    return None


def effective_scopes(documents: list[Document]) -> dict[str, frozenset[str]]:
    """Resolve the effective scope set for every concept of one bundle.

    `documents` must be a single bundle's full document list (concepts *and*
    reserved files), since defaults live in index.md frontmatter.
    """
    defaults = {
        doc.id: declared_scopes(doc.frontmatter, "scope_default")
        for doc in documents
        if doc.path.name == "index.md"
    }
    resolved: dict[str, frozenset[str]] = {}
    for doc in documents:
        if not doc.is_concept:
            continue
        scopes = declared_scopes(doc.frontmatter, "scope")
        directory = doc.id.rsplit("/", 1)[0]
        while scopes is None:
            scopes = defaults.get(f"{directory}/index")
            if not directory:
                break
            directory = directory.rsplit("/", 1)[0]
        resolved[doc.id] = scopes if scopes is not None else frozenset({PUBLIC})
    return resolved


def is_visible(effective: frozenset[str], caller_scopes: frozenset[str]) -> bool:
    return PUBLIC in effective or bool(effective & caller_scopes)
