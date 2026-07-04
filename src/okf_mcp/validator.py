"""Validate OKF bundles.

Checks, per bundle:
- every document's YAML frontmatter parses
- every concept (non-reserved file) declares a `type`
- reserved filenames are not used for concepts (index.md must be an Index,
  log.md a Log)
- `timestamp`, when present, is valid ISO-8601
- `scope` (concepts) and `scope_default` (index.md files) are non-empty lists
  of non-empty strings, and each field appears only where it is meaningful
- bundle-absolute links resolve to a document (or a directory with an index.md)

Exit code is non-zero when any finding is reported.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from okf_mcp.parser import Document, FrontmatterError, parse_document
from okf_mcp.scopes import declared_scopes

_RESERVED_TYPES = {"index.md": "Index", "log.md": "Log"}


@dataclass(frozen=True)
class Finding:
    path: str  # bundle-relative file path
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def _check_document(doc: Document, rel: str) -> list[Finding]:
    findings = []
    if doc.is_concept:
        if doc.type is None:
            findings.append(Finding(rel, "missing required frontmatter field `type`"))
    else:
        expected = _RESERVED_TYPES[doc.path.name]
        if doc.type not in (None, expected):
            findings.append(
                Finding(rel, f"reserved filename used as concept (type: {doc.type!r})")
            )
    timestamp = doc.frontmatter.get("timestamp")
    if timestamp is not None and not _is_iso8601(timestamp):
        findings.append(Finding(rel, f"timestamp is not ISO-8601: {timestamp!r}"))
    findings.extend(_check_scope_fields(doc, rel))
    return findings


def _check_scope_fields(doc: Document, rel: str) -> list[Finding]:
    findings = []
    is_index = doc.path.name == "index.md"
    for field, allowed in (("scope", doc.is_concept), ("scope_default", is_index)):
        if field not in doc.frontmatter:
            continue
        if not allowed:
            where = "concepts" if field == "scope" else "index.md files"
            findings.append(Finding(rel, f"`{field}` is only valid on {where}"))
        elif declared_scopes(doc.frontmatter, field) is None:
            findings.append(
                Finding(rel, f"`{field}` must be a non-empty list of non-empty strings")
            )
    return findings


def _is_iso8601(value: object) -> bool:
    if isinstance(value, datetime):
        return True  # yaml already parsed it as a datetime
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def validate_bundle(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    documents: list[Document] = []
    for path in sorted(root.rglob("*.md")):
        rel = str(path.relative_to(root))
        try:
            doc = parse_document(root, path)
        except FrontmatterError as exc:
            findings.append(Finding(rel, str(exc)))
            continue
        documents.append(doc)
        findings.extend(_check_document(doc, rel))

    ids = {doc.id for doc in documents}
    for doc in documents:
        rel = str(doc.path.relative_to(root))
        for target in doc.links:
            if target not in ids and f"{target}/index" not in ids:
                findings.append(Finding(rel, f"dangling link: {target}"))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate OKF bundles.")
    parser.add_argument("bundles", nargs="+", type=Path, help="bundle root directories")
    args = parser.parse_args(argv)

    exit_code = 0
    for root in args.bundles:
        if not root.is_dir():
            print(f"[{root}] not a directory", file=sys.stderr)
            exit_code = 2
            continue
        findings = validate_bundle(root)
        status = "OK" if not findings else f"{len(findings)} finding(s)"
        print(f"[{root}] {status}")
        for finding in findings:
            print(f"  {finding}")
        if findings:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
