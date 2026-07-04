"""okf-ingest CLI: pull configured sources into validated draft concepts.

Config (YAML, default `config/ingest.yaml`):

    staging_dir: ingest/drafts
    sources:
      - name: handbook
        type: git
        url: https://example.com/acme/handbook.git
        paths: ["docs/**/*.md"]

Exit code is non-zero when any generated draft fails validation.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from okf_mcp.ingest.core import ingest
from okf_mcp.ingest.sources import GitSource, Source
from okf_mcp.parser import FrontmatterError, parse_document
from okf_mcp.validator import _check_document

_DEFAULT_CONFIG = Path("config/ingest.yaml")


class ConfigError(ValueError):
    """Raised when the ingest config is malformed."""


def _build_source(entry: object) -> Source:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise ConfigError("every source needs at least `name` and `type`")
    kind = entry.get("type")
    if kind == "git":
        if not isinstance(entry.get("url"), str):
            raise ConfigError(f"git source {entry['name']!r} needs a `url`")
        paths = entry.get("paths", ["**/*.md"])
        return GitSource(name=entry["name"], url=entry["url"], paths=tuple(paths))
    raise ConfigError(
        f"unknown source type {kind!r} (known: git). New connectors implement "
        "the Source protocol in okf_mcp.ingest.sources."
    )


def load_config(path: Path) -> tuple[Path, list[Source]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ConfigError(f"{path}: ingest config must have a `sources` list")
    staging_dir = Path(raw.get("staging_dir", "ingest/drafts"))
    return staging_dir, [_build_source(entry) for entry in raw["sources"]]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest sources into draft OKF concepts.")
    parser.add_argument(
        "--config", type=Path, default=_DEFAULT_CONFIG, help="ingest config file"
    )
    args = parser.parse_args(argv)

    try:
        staging_dir, sources = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2

    drafts = ingest(sources, staging_dir)

    failures = 0
    for draft in drafts:
        try:
            doc = parse_document(staging_dir, draft.path)
            findings = _check_document(doc, str(draft.path))
        except FrontmatterError as exc:
            findings = [f"{draft.path}: {exc}"]
        for finding in findings:
            print(f"  INVALID {finding}", file=sys.stderr)
        failures += len(findings)

    print(f"{len(drafts)} draft(s) written to {staging_dir} — review and move into a bundle via PR.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
