from pathlib import Path

from okf_mcp.validator import validate_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]

CONCEPT = """\
---
type: Term
title: Thing
timestamp: 2026-07-03T09:00:00Z
---

# Thing

See [other](/other).
"""

OTHER = """\
---
type: Term
---

# Other
"""


def write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_clean_bundle_passes(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", CONCEPT)
    write(tmp_path, "other.md", OTHER)
    assert validate_bundle(tmp_path) == []


def test_missing_type_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", "---\ntitle: No Type\n---\n\n# No Type\n")
    findings = validate_bundle(tmp_path)
    assert len(findings) == 1
    assert "missing required frontmatter field `type`" in findings[0].reason


def test_no_frontmatter_at_all_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", "# Bare markdown\n")
    findings = validate_bundle(tmp_path)
    assert [f.reason for f in findings] == ["missing required frontmatter field `type`"]


def test_bad_yaml_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", "---\ntype: [unclosed\n---\n\n# Broken\n")
    findings = validate_bundle(tmp_path)
    assert len(findings) == 1
    assert "invalid YAML frontmatter" in findings[0].reason


def test_bad_timestamp_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", "---\ntype: Term\ntimestamp: yesterday-ish\n---\n")
    findings = validate_bundle(tmp_path)
    assert len(findings) == 1
    assert "not ISO-8601" in findings[0].reason


def test_dangling_link_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", CONCEPT)  # links to /other, which we omit
    findings = validate_bundle(tmp_path)
    assert len(findings) == 1
    assert "dangling link: /other" in findings[0].reason


def test_directory_link_resolves_via_index(tmp_path: Path) -> None:
    write(tmp_path, "thing.md", "---\ntype: Term\n---\n\nSee [dir](/sub).\n")
    write(tmp_path, "sub/index.md", "---\ntype: Index\n---\n\n# Sub\n")
    assert validate_bundle(tmp_path) == []


def test_reserved_name_used_as_concept_is_reported(tmp_path: Path) -> None:
    write(tmp_path, "sub/index.md", "---\ntype: Metric\n---\n\n# Sneaky concept\n")
    findings = validate_bundle(tmp_path)
    assert len(findings) == 1
    assert "reserved filename used as concept" in findings[0].reason


def test_shipped_bundles_are_clean() -> None:
    for name in ("acme-knowledge", "acme-knowledge-restricted"):
        assert validate_bundle(REPO_ROOT / "bundles" / name) == []
