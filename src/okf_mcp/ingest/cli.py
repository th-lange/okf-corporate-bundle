"""okf-ingest CLI: source-authoritative synchronization into the knowledge tree.

Commands:

    okf-ingest [sync]    mirror sources into the knowledge tree: add new
                         concepts, replace modified ones in place, remove
                         what the owner removed — one git commit per run
    okf-ingest status    classify every document (new / unchanged / modified
                         / removed) against the ledger, changing nothing

There are no drafts and no editorial gate: curation happens at the source,
where the owning sector's own review process decides what gets published.
Sync keeps only *mechanical* guarantees — the validator must pass, scope
fields never come from source content, provenance is stamped by the
pipeline — and a document that fails them never replaces its predecessor
(last-known-good; the failed output lands in quarantine instead).

Consistency rolls on content hashes (see okf_mcp.ingest.ledger): unchanged
content is a no-op regardless of revision churn, renames keep concept
identity, and removed concepts resurrect when their content reappears.

Sync writes only under OKF_KNOWLEDGE_ROOT — the operator repo's fixture
bundles are read-only demo content, so `sync` refuses to run without a
knowledge root.

Config (YAML, default `$OKF_KNOWLEDGE_ROOT/ingest.yaml`):

    ledger: ingest/ledger.yaml
    quarantine: ingest/quarantine
    catalog_bundles: [bundles/acme-knowledge]   # link targets for the llm transformer
    sources:
      - name: compliance-handbook
        type: git
        url: git@github.com:acme/compliance-handbook.git
        paths: ["policies/**/*.md"]
        transformer: llm                    # default: passthrough
        target: acme-knowledge/compliance   # bundle[/dir] under <root>/bundles/
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

from okf_mcp.index import OkfIndex
from okf_mcp.ingest.drive import DriveSource
from okf_mcp.ingest.ledger import Ledger
from okf_mcp.ingest.llm import ClaudeClient, LlmError, LlmTransformer
from okf_mcp.ingest.s3 import S3Source
from okf_mcp.ingest.sources import GitSource, Source, SourceDocument
from okf_mcp.ingest.transform import PassthroughTransformer, Transformer
from okf_mcp.knowledge import (
    REPO_ROOT,
    KnowledgeRootError,
    discover_bundles,
    knowledge_root,
)
from okf_mcp.parser import RESERVED_NAMES, FrontmatterError, parse_document
from okf_mcp.validator import _check_document, _collect_ids, validate_bundle

_DEFAULT_CATALOG = (REPO_ROOT / "bundles" / "acme-knowledge",)
_TRANSFORMERS = ("passthrough", "llm")

SourceSpec = tuple[Source, str, str]  # (source, transformer name, target)


class ConfigError(ValueError):
    """Raised when the ingest config is malformed."""


def _default_config() -> Path:
    root = knowledge_root()
    if root is not None:
        return root / "ingest.yaml"
    return REPO_ROOT / "config" / "ingest.yaml"


def _build_source(entry: object) -> SourceSpec:
    if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
        raise ConfigError("every source needs at least `name`, `type`, and `target`")
    transformer = entry.get("transformer", "passthrough")
    if transformer not in _TRANSFORMERS:
        raise ConfigError(
            f"unknown transformer {transformer!r} for source {entry['name']!r} "
            f"(known: {', '.join(_TRANSFORMERS)})"
        )
    source = _build_connector(entry)
    target = entry.get("target")
    if not isinstance(target, str) or not target.strip("/"):
        raise ConfigError(
            f"source {entry['name']!r} needs a `target` — the bundle[/dir] under "
            "<knowledge-root>/bundles/ its concepts sync into"
        )
    return source, transformer, target.strip("/")


def _build_connector(entry: dict) -> Source:
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
    if kind == "s3":
        if not isinstance(entry.get("bucket"), str):
            raise ConfigError(f"s3 source {entry['name']!r} needs a `bucket`")
        return S3Source(
            name=entry["name"], bucket=entry["bucket"], prefix=entry.get("prefix", "")
        )
    raise ConfigError(
        f"unknown source type {kind!r} (known: git, gdrive, s3). New connectors "
        "implement the Source protocol in okf_mcp.ingest.sources."
    )


def load_config(path: Path) -> tuple[Path, Path, list[SourceSpec], tuple[Path, ...]]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), list):
        raise ConfigError(f"{path}: ingest config must have a `sources` list")
    # All sync state lives with the knowledge, never in the operator repo.
    base = knowledge_root() or Path.cwd()
    ledger_path = base / Path(raw.get("ledger", "ingest/ledger.yaml"))
    quarantine_dir = base / Path(raw.get("quarantine", "ingest/quarantine"))
    catalog = tuple(base / Path(p) for p in raw.get("catalog_bundles", [])) or _DEFAULT_CATALOG
    return ledger_path, quarantine_dir, [_build_source(e) for e in raw["sources"]], catalog


def _build_transformers(
    specs: list[SourceSpec], catalog_bundles: tuple[Path, ...]
) -> dict[str, Transformer]:
    """One transformer per source name; the LLM worker is built only if used."""
    transformers: dict[str, Transformer] = {}
    passthrough = PassthroughTransformer()
    llm: LlmTransformer | None = None
    for source, kind, _ in specs:
        if kind == "llm":
            if llm is None:
                index = OkfIndex(*catalog_bundles)
                known = {
                    doc.id: f"{doc.type} — {doc.frontmatter.get('title')}: "
                    f"{doc.frontmatter.get('description')}"
                    for doc in (index.get_concept(cid) for cid in index.ids())
                }
                llm = LlmTransformer(
                    client=ClaudeClient.from_env(),
                    known_concepts=known,
                    type_names=tuple(index.types()) or ("Document",),
                )
            transformers[source.name] = llm
        else:
            transformers[source.name] = passthrough
    return transformers


def _pull(specs: list[SourceSpec]) -> list[tuple[Source, str, SourceDocument]]:
    return [(source, target, doc) for source, _, target in specs for doc in source.documents()]


def _apply(
    root: Path,
    concept_rel: str,
    source: Source,
    doc: SourceDocument,
    transformer: Transformer,
    quarantine_dir: Path,
) -> str | None:
    """Transform, validate, and write one concept. Returns a failure line, or
    None on success. On failure the tree is untouched (last-known-good) and
    the offending output sits in quarantine."""
    rel = doc.relative_path if doc.relative_path.endswith(".md") else doc.relative_path + ".md"
    if Path(rel).name in RESERVED_NAMES:
        return f"{doc.source_uri}: reserved filename {Path(rel).name!r} cannot become a concept"
    qpath = quarantine_dir / source.name / rel
    qpath.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = transformer.transform(doc)
    except (FrontmatterError, LlmError) as exc:
        qpath.write_text(doc.content, encoding="utf-8")
        return f"{doc.source_uri}: transform failed: {exc}"
    qpath.write_text(text, encoding="utf-8")
    try:
        parsed = parse_document(quarantine_dir, qpath)
        findings = _check_document(parsed, doc.relative_path)
    except FrontmatterError as exc:
        return f"{doc.source_uri}: invalid output frontmatter: {exc}"
    if findings:
        return f"{doc.source_uri}: " + "; ".join(f.reason for f in findings)
    qpath.unlink()
    final = root / concept_rel
    final.parent.mkdir(parents=True, exist_ok=True)
    final.write_text(text, encoding="utf-8")
    return None


def _commit(root: Path, ledger_path: Path, counts: Counter) -> str | None:
    """One commit per sync run in the knowledge repo; none if it isn't one."""
    if not (root / ".git").exists():
        return None
    paths = ["bundles"]
    if ledger_path.is_relative_to(root):
        paths.append(str(ledger_path.relative_to(root)))
    subprocess.run(["git", "-C", str(root), "add", "-A", *paths], capture_output=True, check=True)
    staged = subprocess.run(
        ["git", "-C", str(root), "diff", "--cached", "--quiet"], capture_output=True
    )
    if staged.returncode == 0:
        return None
    message = "okf-ingest sync: " + ", ".join(
        f"{counts[s]} {s}" for s in ("new", "modified", "renamed", "restored", "removed")
    )
    subprocess.run(
        [
            "git", "-C", str(root),
            "-c", "user.name=okf-ingest", "-c", "user.email=okf-ingest@local",
            "commit", "--quiet", "-m", message,
        ],
        capture_output=True,
        check=True,
    )
    short = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    return short.stdout.strip()


def _integrity(root: Path) -> list[str]:
    """Post-sync report: dangling links across the whole knowledge tree,
    so deletions surface to the owners of whoever pointed at them."""
    try:
        bundles = discover_bundles(root)
    except KnowledgeRootError:
        return []
    external = {b.name: _collect_ids(b) for b in bundles}
    return [
        f"{bundle.name}/{finding}"
        for bundle in bundles
        for finding in validate_bundle(bundle, external)
        if "dangling" in finding.reason
    ]


def _sync(
    root: Path,
    ledger: Ledger,
    ledger_path: Path,
    specs: list[SourceSpec],
    transformers: dict[str, Transformer],
    quarantine_dir: Path,
) -> int:
    for _, _, target in specs:
        bundle = target.split("/", 1)[0]
        if not (root / "bundles" / bundle / "index.md").is_file():
            print(
                f"target bundle {bundle!r} does not exist under {root / 'bundles'} "
                "(a bundle is a directory with an index.md)",
                file=sys.stderr,
            )
            return 2

    pulled = _pull(specs)
    current_uris = {doc.source_uri for _, _, doc in pulled}
    counts: Counter[str] = Counter()
    failures: list[str] = []
    seen: set[str] = set()

    for source, target, doc in pulled:
        uri, sha = doc.source_uri, doc.content_sha256
        seen.add(uri)
        state = ledger.classify(uri, doc.revision, sha)
        if state == "unchanged":
            entry = ledger.entry(uri) or {}
            gone = "removed_at" in entry or not (root / entry.get("concept", "")).is_file()
            if not gone:
                # hash governs identity — don't churn the ledger (and the
                # knowledge repo's history) over a revision-only change
                ledger.mark_seen(uri)
                counts["unchanged"] += 1
                continue
            # same URI, same content, but the concept was removed from the
            # tree (deleted upstream earlier, now reverted) — resurrect it.
            concept_rel = entry["concept"]
            counts["restored"] += 1
        elif state == "new":
            prior = ledger.match_by_sha(sha, current_uris)
            if prior is not None:
                adopted, was_removed = ledger.adopt(prior, uri, doc.revision)
                concept_rel = adopted["concept"]
                counts["restored" if was_removed else "renamed"] += 1
            else:
                rel = (
                    doc.relative_path
                    if doc.relative_path.endswith(".md")
                    else doc.relative_path + ".md"
                )
                concept_rel = f"bundles/{target}/{rel}"
                counts["new"] += 1
        else:
            concept_rel = ledger.entry(uri)["concept"]
            counts["modified"] += 1

        failure = _apply(root, concept_rel, source, doc, transformers[source.name], quarantine_dir)
        if failure:
            failures.append(failure)  # last-known-good: ledger keeps the old state
            continue
        ledger.record(uri, source.name, concept_rel, doc.revision, sha)

    newly_removed = ledger.sweep_removed(seen)
    for uri in newly_removed:
        concept = (ledger.entry(uri) or {}).get("concept")
        if concept and (root / concept).exists():
            (root / concept).unlink()
    counts["removed"] += len(newly_removed)
    ledger.save()

    commit = _commit(root, ledger_path, counts)

    for line in _integrity(root):
        print(f"  INTEGRITY {line}", file=sys.stderr)
    for line in failures:
        print(f"  QUARANTINED {line}", file=sys.stderr)

    summary = ", ".join(
        f"{counts[s]} {s}"
        for s in ("new", "modified", "renamed", "restored", "unchanged", "removed")
    )
    tail = f"; committed {commit}" if commit else ""
    print(f"{summary}{tail}; ledger: {ledger.path}")
    return 1 if failures else 0


def _status(ledger: Ledger, sources: list[Source]) -> int:
    current = {
        doc.source_uri: (doc.revision, doc.content_sha256)
        for source in sources
        for doc in source.documents()
    }
    states = ledger.status(current)
    for uri, state in states:
        print(f"{state.upper():10} {uri}")
    counts = Counter(state for _, state in states)
    print(", ".join(f"{counts[s]} {s}" for s in ("new", "modified", "unchanged", "removed")))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize sources into the knowledge tree (source-authoritative)."
    )
    parser.add_argument("command", nargs="?", choices=("sync", "status"), default="sync")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="ingest config file (default: $OKF_KNOWLEDGE_ROOT/ingest.yaml, "
        "else the repo's demo config)",
    )
    args = parser.parse_args(argv)

    try:
        config_path = args.config if args.config is not None else _default_config()
        ledger_path, quarantine_dir, specs, catalog_bundles = load_config(config_path)
        sources = [source for source, _, _ in specs]
        ledger = Ledger.load(ledger_path)
        if args.command == "status":
            return _status(ledger, sources)
        root = knowledge_root()
        if root is None:
            print(
                "sync writes to the knowledge tree; set OKF_KNOWLEDGE_ROOT — the "
                "operator repo's fixture bundles are read-only demo content.",
                file=sys.stderr,
            )
            return 2
        transformers = _build_transformers(specs, catalog_bundles)
    except (ConfigError, KnowledgeRootError, LlmError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2

    return _sync(root, ledger, ledger_path, specs, transformers, quarantine_dir)


if __name__ == "__main__":
    raise SystemExit(main())
