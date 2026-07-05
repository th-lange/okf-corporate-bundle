"""Load OKF bundles: frontmatter parsing, document discovery, link extraction.

An OKF bundle is a directory tree of markdown files. Files named `index.md` or
`log.md` are reserved (directory listing / change history); every other markdown
file is a *concept*. A document's id is its bundle-relative path without the
`.md` suffix, in leading-slash form (e.g. `/metrics/monthly-recurring-revenue`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

RESERVED_NAMES = frozenset({"index.md", "log.md"})

# Concept links: bundle-absolute ](/path/to/concept) and qualified cross-bundle
# ](bundle-name:/path/to/concept). URL schemes (https://, git://, bigquery://)
# carry `//` after the colon and don't match.
_LINK_RE = re.compile(r"\]\(((?:[A-Za-z0-9][A-Za-z0-9._-]*:)?/(?!/)[^)#\s]+)")
_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


class FrontmatterError(ValueError):
    """Raised when a document's YAML frontmatter cannot be parsed."""


@dataclass(frozen=True)
class Document:
    """One markdown file in a bundle (concept or reserved file)."""

    id: str  # "/metrics/monthly-recurring-revenue"
    path: Path
    frontmatter: dict
    body: str
    links: tuple[str, ...]  # outbound bundle-absolute link targets

    @property
    def is_concept(self) -> bool:
        return self.path.name not in RESERVED_NAMES

    @property
    def type(self) -> str | None:
        value = self.frontmatter.get("type")
        return value if isinstance(value, str) else None


def split_frontmatter(text: str) -> tuple[dict, str]:
    """Split leading YAML frontmatter from the body.

    Returns ({}, text) when no frontmatter block is present; raises
    FrontmatterError on unparseable YAML or a non-mapping.
    """
    if match := _FRONTMATTER_RE.match(text):
        try:
            loaded = yaml.safe_load(match.group(1))
        except yaml.YAMLError as exc:
            raise FrontmatterError(f"invalid YAML frontmatter: {exc}") from exc
        if loaded is not None and not isinstance(loaded, dict):
            raise FrontmatterError("frontmatter is not a YAML mapping")
        return loaded or {}, text[match.end() :]
    return {}, text


def parse_document(root: Path, path: Path) -> Document:
    """Parse one markdown file relative to the bundle root."""
    frontmatter, body = split_frontmatter(path.read_text(encoding="utf-8"))
    doc_id = "/" + str(path.relative_to(root).with_suffix("")).replace("\\", "/")
    links = tuple(m.group(1) for m in _LINK_RE.finditer(body))
    return Document(id=doc_id, path=path, frontmatter=frontmatter, body=body, links=links)


def load_bundle(root: Path) -> list[Document]:
    """Parse every markdown file in a bundle, sorted by id.

    Raises FrontmatterError on the first unparseable document; callers that
    want per-file error reporting (the validator) parse file-by-file instead.
    """
    return sorted(
        (parse_document(root, p) for p in root.rglob("*.md")),
        key=lambda d: d.id,
    )
