# AGENTS.md — entry point for agents

This repo is a working example of serving corporate knowledge to AI agents: two
OKF (Open Knowledge Format) bundles for a fictional company plus an MCP server
(`okf-mcp`) that exposes them with scoped access. Read [README.md](README.md)
for the concept and architecture, and [docs/usage.md](docs/usage.md) for how to
run, consume, and author — including the do's and don'ts.

## Keep the documentation in sync — this is a requirement

Documentation here is part of the product, not an afterthought. **Any change
that alters behaviour, tools, structure, conventions, or roadmap MUST update
the affected documentation in the same commit/PR:**

- **AGENTS.md** (this file) — when commands, layout, or conventions change.
- **README.md** — when architecture, tools, layout, or roadmap change.
- **docs/usage.md** — when server usage, authoring rules, or the do's/don'ts change.
- **docs/demo.md** — when tool behaviour, personas, or scope assignments change
  the walkthrough's expected outputs (re-run its commands to confirm).
- **docs/inversion.md** — when a mechanism it maps (connectors, gate, scopes,
  provenance) changes shape, or a tracked gap (#36–#38) ships.

A change that lands without its doc updates is incomplete. If you finish a task
and haven't checked all three files, you are not done. Stale documentation is a
bug — fix it when you find it, even if it isn't your change.

## Map

```
bundles/acme-knowledge/             internal knowledge bundle (the demo corpus)
bundles/acme-knowledge-restricted/  restricted bundle (separate repo in production)
src/okf_mcp/knowledge.py            knowledge-root discovery + generation pointer
                                    resolution (OKF_KNOWLEDGE_ROOT, resolve_root)
src/okf_mcp/parser.py               frontmatter + link extraction
src/okf_mcp/index.py                in-memory index: lookup, search, follow_links,
                                    per-session scope filtering (visible_to)
src/okf_mcp/scopes.py               effective-scope resolution + visibility rule
src/okf_mcp/embeddings.py           optional semantic search: hash-keyed vector store
                                    (EmbeddingStore, Encoder seam, sync_embeddings)
src/okf_mcp/auth.py                 Authenticator protocol (IdP seam) + static demo impl
src/okf_mcp/authz.py                per-resource grants (ResourceAuthorizer) + AuditLog
config/auth.yaml                    demo persona tokens → scope sets
config/resources.yaml               resource grants: scope → resolvable URIs
src/okf_mcp/server.py               MCP server (stdio), tools: get_concept,
                                    search_concepts, list_by_type, follow_links,
                                    resolve_resource (authz-gated, audit-logged),
                                    propose_upstream (write-back to sector sources)
src/okf_mcp/writeback.py            upstream proposals: branch in the owning repo,
                                    or suggestion artifact for non-git sources
src/okf_mcp/validator.py            bundle validator CLI (okf-validate)
src/okf_mcp/ingest/                 okf-ingest: Source connectors (sources.py: git,
                                    drive.py: gdrive, s3.py: s3 — each opts into
                                    pairing `<path>.okf-vec.json` precomputed-vector
                                    sidecars via `vectors: sidecar`, issue #49),
                                    Transformer seam (transform.py: passthrough,
                                    llm.py: toolless worker + mechanical checks),
                                    hash-keyed ledger (ledger.py), sync engine + CLI
                                    (sync / status / watch — cli.py),
                                    generations.py: staged generational publish +
                                    CURRENT pointer flip + retention (issue #47),
                                    scheduler.py: `watch` cadence, overlap
                                    lockfile, graceful shutdown (issue #48)
config/ingest.yaml                  demo sync source configuration
<root>/ingest/ledger.yaml           committed ledger: source doc → hash, concept
tests/                              pytest suite, one file per feature
docs/inversion.md                   the "why": inversion of knowledge, vision → mechanism map
docs/demo.md                        end-to-end demo: MRR investigation + persona visibility
docs/usage.md                       usage doc, do's and don'ts
```

## Commands

```bash
uv sync                                  # install (Python ≥ 3.12, uv)
uv run pytest                            # tests
uv run ruff check                        # lint (line length 100)
uv run okf-validate bundles/acme-knowledge bundles/acme-knowledge-restricted
uv run okf-mcp                           # run the MCP server (stdio); OKF_BUNDLE_DIR selects the bundle
uv run okf-ingest sync                   # mirror sources into $OKF_KNOWLEDGE_ROOT (source-authoritative)
uv run okf-ingest watch --once           # one background-sync tick (systemd-timer style)
uv sync --extra semantic                 # optional: enable embeddings (search_concepts, ingest sync)
```

CI runs lint, tests, and the validator on every push — all three must pass.

## Conventions

- **Issue-driven development.** Work maps to GitHub issues with acceptance
  criteria and explicit `Blocked by` chains. Check the blockers before starting
  an issue. Open tracks: access control (#6→#7→#8→#9) and ingestion
  (#15→#16→#17/#18).
- **One concept per file; the path is the id.** Never rename concept files
  without updating every inbound link; run the validator after touching bundles.
- **Fixture bundle edits go through PR review** (this repo's `bundles/` are
  hand-maintained demo content). Update the bundle's `log.md` and the
  concept's `timestamp:` with content changes.
- **Sources are authoritative; sync is mechanical.** The sector's own review
  is the only editorial gate — sync mirrors sources into the knowledge tree
  (add / replace / remove, one commit per run, hash-keyed identity) and
  enforces only mechanical rules: validator passes, scope fields never come
  from source content, provenance is stamped by the pipeline, failed
  conversions keep last-known-good and land in quarantine. **Sources are
  isolated from each other** (issue #46): each is pulled and applied
  independently with a per-source outcome (OK / SKIPPED — not configured /
  FAILED — errored), and one source's failure never blocks another's
  update. The removal sweep is scoped per source (`Ledger.sweep_removed`
  keyed on the entry's `source` field), so a `FAILED` source's entries are
  never swept, and a source that cleanly returns zero documents while the
  ledger holds active entries for it is guarded (warn, or `--allow-empty`
  to sweep anyway) rather than silently tombstoned. Exit code is non-zero
  only on a real `FAILED` source or a quarantined document.
- **Write-back goes upstream, never into the tree.** `propose_upstream`
  creates a branch in the owning sector's source (or a suggestion artifact
  for non-git sources); it must never write to `bundles/` and never merge —
  the sector's review plus the next sync are the only way back in.
- **The LLM worker stays toolless and the checks stay deterministic.** Source
  documents are untrusted input; never give the ingest worker tools, never
  replace the mechanical checks with model judgment, and never let model
  output set scopes, provenance, or unverified resource URIs. The same holds
  for a precomputed vector pulled from a source sidecar (`vectors: sidecar`,
  issue #49): it is data, never model judgment — it is imported into the
  embedding store on a matching `model_id` and nothing else, and it never
  sets scopes, provenance, or resource URIs (provenance stays the ledger
  entry's own source/revision fields); a mismatched or malformed vector is
  quarantined and the document falls back to local encoding.
- **Operator ≠ knowledge.** This repo is the tool; real knowledge lives under
  `OKF_KNOWLEDGE_ROOT` (bundles, ledger, quarantine). The in-repo `bundles/`
  are demo fixtures and sync refuses to write without a knowledge root. Never
  write sync state or knowledge into the operator repo, and never bake
  knowledge into the container image.
- **List-style MCP tools return summaries, never bodies.** Preserve this when
  adding tools — it is the context-size guarantee.
- **Sensitivity = bundle separation.** Never move restricted concepts into the
  internal bundle or serve both to one unscoped session.
- **Scopes bind at session start, never from tool input.** Enforcement is pure
  set intersection over the per-session filtered index (`OkfIndex.visible_to`);
  every serving path must go through that view, and no MCP tool may ever accept
  scope labels as a parameter.
- **Generational publish is opt-in and additive.** A knowledge root with
  `generations: true` in `ingest.yaml` (issue #47) publishes to a staged
  `generations/<id>/`, validated, then flips `generations/CURRENT` via
  `os.replace` — a plain-filesystem mechanism, never git (`_commit` already
  returns `None` on a non-git root). Every existing plain-directory root
  keeps working unchanged — `okf_mcp.knowledge.resolve_root` only resolves
  the pointer when one exists. A server pins its generation at
  `build_server()` time and serves it for the process's lifetime;
  long-lived processes opt into `OKF_HOT_RELOAD=1` to hot-swap on pointer
  change without dropping connections. The embedding store is never staged
  per generation — it stays shared at `<root>/ingest/embeddings.db`.
- **Background sync is opt-in scheduling, not a new sync path.** `okf-ingest
  watch` (issue #48) reuses `_sync`/`_sync_generation` unmodified, restricted
  each tick to whichever sources are due per `schedule:` (per-source > global
  > the loop's own `--interval`); it never changes sync/publish semantics,
  only when they run. A lockfile at `<root>/ingest/sync.lock`
  (`scheduler.SyncLock`) serializes `sync` and every `watch` tick against the
  same knowledge root — reclaimed automatically once its holder is dead or
  the lock is older than 6 hours. A `FAILED` source (issue #46) during a
  tick is logged and simply retried on its own next cadence; the loop never
  exits on a source failure, and SIGINT/SIGTERM only take effect between
  ticks (the in-flight tick always finishes).
