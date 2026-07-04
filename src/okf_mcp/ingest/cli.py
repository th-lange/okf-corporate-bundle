"""okf-ingest CLI: pull configured sources into validated draft concepts.

Commands:

    okf-ingest [run]     ingest new/modified documents, update the ledger
    okf-ingest status    classify every document (new / unchanged / modified
                         / removed) against the ledger, changing nothing

Config (YAML, default `config/ingest.yaml`):

    staging_dir: ingest/drafts
    ledger: ingest/ledger.yaml
    sources:
      - name: handbook
        type: git
        url: https://example.com/acme/handbook.git
        paths: ["docs/**/*.md"]

`run` regenerates drafts only for new and modified documents. Documents that
vanished upstream are flagged in the ledger (`removed_at`) and reported —
never deleted; retiring a concept is a human decision. Exit code is non-zero
when any generated draft fails validation.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml

from okf_mcp.ingest.core import write_draft
from okf_mcp.ingest.drive import DriveSource
from okf_mcp.ingest.ledger import Ledger
from okf_mcp.ingest.sources import GitSource, Source, SourceDocument
from okf_mcp.ingest.transform import PassthroughTransformer
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
    if kind == "gdrive":
        if not isinstance(entry.get("folder_id"), str):
            raise ConfigError(f"gdrive source {entry['name']!r} needs a `folder_id`")
        return DriveSource(name=entry["name"], folder_id=entry["folder_id"])
    raise ConfigError(
        f"unknown source type {kind!r} (known: git, gdrive). New connectors "
        "implement the Source protocol in okf_mcp.ingest.sources."
    )


def load_config(path: Path) -> tuple[Path, Path, list[Source]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ConfigError(f"{path}: ingest config must have a `sources` list")
    staging_dir = Path(raw.get("staging_dir", "ingest/drafts"))
    ledger_path = Path(raw.get("ledger", "ingest/ledger.yaml"))
    return staging_dir, ledger_path, [_build_source(entry) for entry in raw["sources"]]


def _pull(sources: list[Source]) -> list[tuple[Source, SourceDocument]]:
    return [(source, doc) for source in sources for doc in source.documents()]


def _run(staging_dir: Path, ledger: Ledger, sources: list[Source]) -> int:
    transformer = PassthroughTransformer()
    written = []
    counts: Counter[str] = Counter()
    seen: set[str] = set()
    for source, doc in _pull(sources):
        seen.add(doc.source_uri)
        state = ledger.classify(doc.source_uri, doc.revision)
        counts[state] += 1
        if state in ("new", "modified"):
            draft = write_draft(doc, source.name, staging_dir, transformer)
            rel = draft.path.relative_to(staging_dir).as_posix()
            ledger.record(doc.source_uri, source.name, rel, doc.revision)
            written.append(draft)
        else:
            ledger.mark_seen(doc.source_uri)

    newly_removed = ledger.sweep_removed(seen)
    counts["removed"] += len(newly_removed)
    ledger.save()

    failures = 0
    for draft in written:
        try:
            doc = parse_document(staging_dir, draft.path)
            findings = _check_document(doc, str(draft.path))
        except FrontmatterError as exc:
            findings = [f"{draft.path}: {exc}"]
        for finding in findings:
            print(f"  INVALID {finding}", file=sys.stderr)
        failures += len(findings)

    for uri in newly_removed:
        entry = ledger.entry(uri) or {}
        print(
            f"  REMOVED upstream: {uri} (draft {entry.get('draft')}) — "
            "review whether the concept should be retired.",
            file=sys.stderr,
        )

    summary = ", ".join(f"{counts[s]} {s}" for s in ("new", "modified", "unchanged", "removed"))
    print(f"{summary} — {len(written)} draft(s) written to {staging_dir}; ledger: {ledger.path}")
    return 1 if failures else 0


def _status(ledger: Ledger, sources: list[Source]) -> int:
    current = {doc.source_uri: doc.revision for _, doc in _pull(sources)}
    states = ledger.status(current)
    for uri, state in states:
        print(f"{state.upper():10} {uri}")
    counts = Counter(state for _, state in states)
    print(", ".join(f"{counts[s]} {s}" for s in ("new", "modified", "unchanged", "removed")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest sources into draft OKF concepts.")
    parser.add_argument("command", nargs="?", choices=("run", "status"), default="run")
    parser.add_argument(
        "--config", type=Path, default=_DEFAULT_CONFIG, help="ingest config file"
    )
    args = parser.parse_args(argv)

    try:
        staging_dir, ledger_path, sources = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2

    ledger = Ledger.load(ledger_path)
    if args.command == "status":
        return _status(ledger, sources)
    return _run(staging_dir, ledger, sources)


if __name__ == "__main__":
    raise SystemExit(main())
