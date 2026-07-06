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
src/okf_mcp/knowledge.py            knowledge-root discovery (OKF_KNOWLEDGE_ROOT)
src/okf_mcp/parser.py               frontmatter + link extraction
src/okf_mcp/index.py                in-memory index: lookup, search, follow_links,
                                    per-session scope filtering (visible_to)
src/okf_mcp/scopes.py               effective-scope resolution + visibility rule
src/okf_mcp/auth.py                 Authenticator protocol (IdP seam) + static demo impl
src/okf_mcp/authz.py                per-resource grants (ResourceAuthorizer) + AuditLog
config/auth.yaml                    demo persona tokens → scope sets
config/resources.yaml               resource grants: scope → resolvable URIs
src/okf_mcp/server.py               MCP server (stdio), tools: get_concept,
                                    search_concepts, list_by_type, follow_links,
                                    resolve_resource (authz-gated, audit-logged)
src/okf_mcp/validator.py            bundle validator CLI (okf-validate)
src/okf_mcp/ingest/                 okf-ingest: Source connectors (sources.py: git,
                                    drive.py: gdrive, s3.py: s3), Transformer seam
                                    (transform.py: passthrough, llm.py: toolless
                                    worker + mechanical checks), hash-keyed ledger
                                    (ledger.py), sync engine + CLI (sync / status)
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
  conversions keep last-known-good and land in quarantine.
- **The LLM worker stays toolless and the checks stay deterministic.** Source
  documents are untrusted input; never give the ingest worker tools, never
  replace the mechanical checks with model judgment, and never let model
  output set scopes, provenance, or unverified resource URIs.
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
