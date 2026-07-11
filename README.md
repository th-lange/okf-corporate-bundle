# okf-corporate-bundle

A working example of serving corporate knowledge to AI agents: two
[OKF (Open Knowledge Format)](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundles for a fictional B2B SaaS company ("Acme"), plus an MCP server that exposes
them to agents with set-based, scoped access control.

> **Docs:** [Why — the inversion of knowledge](docs/inversion.md) · [Demo walkthrough](docs/demo.md) · [Usage — do's and don'ts](docs/usage.md) · [Agent entry point](AGENTS.md)

## Why: the inversion of knowledge

In the traditional pipeline, documentation is written *after* the work and dumped
into a wiki — unowned, conflicting, outdated. Agents make that failure mode
expensive: an agent that doesn't know the company's MRR definition, which table
backs it, or who owns it will guess, hallucinate a plausible-but-wrong query, or
bounce the question back to a human. Most agent inefficiency isn't reasoning
failure — it's *context starvation*.

This repo demonstrates the inversion: each sector **maintains** its knowledge
(rules, processes, patterns, decisions) as an owned artifact, reviewed in the
sector's own process; that knowledge is synchronized — with provenance and
mechanical validation, the sources staying authoritative — into a scoped,
validated **corporate brain**; and agents query the brain over MCP at the
**start** of every task. Knowledge becomes the pipeline's input, not its
exhaust. The full argument, with the old-vs-new pipeline diagrams and the
vision-to-mechanism mapping, is in [docs/inversion.md](docs/inversion.md).

## What is OKF?

OKF represents knowledge as a directory of Markdown files with YAML frontmatter.
Each file is one *concept* (a metric, table, service, runbook, …) with a small set
of queryable fields (`type` required; `title`, `description`, `resource`, `tags`,
`timestamp` recommended) and a Markdown body. Bundle-relative links between
concepts form a knowledge graph agents can traverse.

There is no schema registry, no central authority, no required tooling: if you can
`cat` a file you can read OKF; if you can `git clone` a repo you can ship it.

## The two-axis model

Knowledge is placed on a grid:

- **Domain axis** (the directories): glossary, metrics, data, systems, runbooks,
  playbooks, teams, decisions, policies. Types match the questions agents actually
  ask ("what do we mean by X?", "it's broken — what do I do?", "who owns this?").
- **Sensitivity axis** (classification → bundle separation): public → internal →
  confidential → restricted. Domain decides *where a concept lives*; sensitivity
  decides *which bundle/repo* it lives in, so access control rides on plain git
  permissions.

## Layout

```
bundles/
├── acme-knowledge/             internal bundle (glossary, metrics, data, systems,
│                               runbooks, playbooks, teams, decisions, policies)
└── acme-knowledge-restricted/  restricted bundle (trade-secret methods, patents, raw PII)
config/auth.yaml                demo token → scope-set assignments (persona users)
config/resources.yaml           per-resource authorization grants (scope → URIs)
config/ingest.yaml              ingest sources (demo: this repo's own docs/)
src/okf_mcp/                    MCP server package
├── knowledge.py                knowledge-root discovery (operator/knowledge separation)
├── parser.py                   frontmatter + link extraction
├── index.py                    in-memory index: lookup, search, graph traversal
├── scopes.py                   effective-scope resolution + visibility rule
├── embeddings.py               optional semantic search: hash-keyed vector store
├── auth.py                     pluggable Authenticator (IdP seam) + static demo impl
├── authz.py                    per-resource grants + JSONL audit log
├── server.py                   MCP server (stdio) exposing the tools
├── validator.py                bundle validator CLI (also run in CI)
└── ingest/                     okf-ingest: Source connectors → provenance-stamped drafts,
                                generations.py: staged generational publish (issue #47),
                                scheduler.py: `watch` background worker (issue #48)
docs/inversion.md               the reasoning: knowledge as pipeline input, not exhaust
docs/demo.md                    end-to-end walkthrough: MRR investigation + personas
docs/usage.md                   how to run, author, and consume the bundles
tests/
```

> **Production note — operator vs knowledge:** this repo is the **operator**
> (server, validator, ingester); the in-repo `bundles/` are demo fixtures only.
> In a real deployment the knowledge lives under an external **knowledge root**
> (`OKF_KNOWLEDGE_ROOT`, typically a volume mounted into a container), backed by
> one git repository per sensitivity tier so access control rides on plain git
> permissions — and all ingest state (staging drafts, ledger) lives with the
> knowledge, never in the operator. See [deployment](docs/usage.md#deployment).

## The MCP server

`okf-mcp` (stdio transport) serves one or more bundles (`OKF_BUNDLE_DIRS`,
default: both demo bundles) behind set-based scope enforcement. The session's
scope set is bound once at startup — a bearer token (`OKF_TOKEN`) is resolved
to a scope set by the pluggable auth layer (a static demo config today, a real
IdP behind the same interface in production) — and concepts outside it are
omitted from every tool: they cannot be listed, searched, retrieved, or
reached via `follow_links`, and lookups fail exactly like missing ids. No tool
accepts scopes or tokens as input, so prompt content can never widen
visibility. Current tools:

| Tool | Answers |
|---|---|
| `search_concepts(query, type?, tags?, limit?)` | "Where do I start?" — ranked keyword search (title/aliases > tags > description > body), optionally augmented by semantic similarity when embeddings are configured; compact summaries only |
| `list_by_type(type)` | "What metrics/runbooks/… exist?" |
| `get_concept(id)` | "What is the authoritative definition?" — full frontmatter + body |
| `follow_links(id, depth?)` | "What's connected?" — cycle-safe graph traversal, summaries + hop distance |
| `resolve_resource(id)` | "Give me the actual data pointer" — the `resource:` URI, gated by per-resource grants and audit-logged |

List-style tools return summaries (id/type/title/description) — never bodies — so
agents scan cheaply and fetch full text only for the concepts they actually need.

### Semantic search (optional)

`search_concepts` stays keyword-only by default. Installing the `semantic`
extra (`uv sync --extra semantic`) and configuring an `embeddings:` block in
`ingest.yaml` turns on a second layer: `okf-ingest sync` embeds each synced
concept's body into a persistent, sqlite-backed vector store keyed by
`(content_sha256, model_id)` under `$OKF_KNOWLEDGE_ROOT/ingest/embeddings.db`
— never the operator repo. Because the key is the content hash the ledger
already computes, sync only embeds *new*/*modified* documents; unchanged,
renamed, and resurrected documents reuse their existing vector, so nothing is
ever re-embedded for free. A model change re-embeds under its own key without
touching or mixing with vectors from a prior model.

At serving time `search_concepts` queries the store only for concept ids
already in the caller's scoped view (`OkfIndex.visible_to`), then merges
similarity hits after keyword hits — an out-of-scope concept's vector is
never reachable, however close a semantic match it is. Without the extra
installed or a store present, behaviour is identical to keyword-only search.
See [docs/usage.md](docs/usage.md#semantic-search-optional) for the config
block and enabling steps.

### Generational publish + hot reload (optional)

`okf-ingest sync` normally writes in place. Opting a knowledge root into
`generations: true` (in `ingest.yaml`) switches sync to **generational
publish**: the next full tree (`bundles/` + ledger) is staged under
`generations/<id>/`, validated, and only then does a `generations/CURRENT`
pointer file flip — via `os.replace`, atomic on POSIX and requiring no git
repository. Readers resolve the pointer and either see the previous
generation, complete, or the new one, complete — never a half-written tree.
A staged generation that fails validation, or a run that errors before it
finishes, is discarded before the pointer is ever touched, so the last-good
generation keeps serving. Git, when the root is a repo, remains the audit
trail (one commit per run, as before) — never the publish mechanism.

```
generations/
├── CURRENT              → "20260711T142233-000004" (plain text, atomically replaced)
├── 20260711T140901-000002/   bundles/ + ingest/ledger.yaml (superseded, retained)
└── 20260711T142233-000004/   bundles/ + ingest/ledger.yaml (CURRENT)
```

A server process resolves `CURRENT` once at startup and serves that
generation for its lifetime — under stdio the process *is* the session, so
this alone gives every connection a stable snapshot while sync advances
generations underneath it. A long-lived process (a future HTTP transport,
or a stdio host kept warm across sessions) opts into `OKF_HOT_RELOAD=1`,
which re-checks the pointer before answering each tool call (one `stat()`)
and swaps in a freshly built index — in-flight calls keep the object they
already hold. The embedding store (`ingest/embeddings.db`) is **not**
staged per generation: it is content-hash-keyed and shared across every
generation, so old and new views alike share its cache with no copy cost.
See [docs/usage.md](docs/usage.md#generational-publish-optional) for
enabling steps, retention, and that sharing tradeoff.

## Status & roadmap

Development is issue-driven; every issue carries acceptance criteria. Shipped:

- **Access control** (#6–#9): set-based scope model with layered defaults,
  pluggable token-to-scope-set auth, authz-gated `resolve_resource` with audit
  logging, and the end-to-end demo walkthrough.
- **Ingestion** (#15–#18): `okf-ingest` pulling from git, Google Drive, and S3
  behind a small `Source` interface, provenance-stamped drafts for human
  review, and a ledger tracking what changed or vanished upstream.
- **Hardening & architecture** (#28–#31): ranked alias-aware search with
  result limits, the LLM transformer (toolless worker + deterministic gate),
  operator/knowledge separation with a container story, and scope-gated
  cross-bundle references.
- **Source-authoritative sync** (#37): `okf-ingest sync` mirrors sector
  sources into the knowledge tree — add / replace / remove, hash-keyed
  identity (rename preservation, resurrection), one commit per run,
  last-known-good on failed conversions, post-sync integrity report.
- **The write-back loop** (#38): `propose_upstream` sends what an agent
  learned to the owning sector's source as a branch for *their* review; the
  next sync brings accepted knowledge back — the inversion as a cycle.
- **Generational atomic publish + hot reload** (#47): sync stages, validates,
  then atomically flips a `generations/CURRENT` pointer — no git required;
  a server pins its generation at startup and long-lived processes hot-swap
  on `OKF_HOT_RELOAD=1` without dropping connections.
- **Background sync worker** (#48): `okf-ingest watch` runs sync on a
  config-driven, per-source cadence — built on generational publish (a
  scheduled run never disturbs a live session) and per-source isolation (one
  source's failure never stalls the schedule); an overlap-guarding lockfile
  serializes it against `sync`; systemd-timer and docker-compose sidecar
  deployment recipes are in [docs/usage.md](docs/usage.md#background-sync-worker-okf-ingest-watch-optional).

The [inversion vision](docs/inversion.md) is fully implemented at demo scale;
how sectors plug in their own sources is documented as
[example configurations](docs/usage.md#example-federated-sector-sources)
rather than a runnable demo. Future work lands on the
[issue tracker](https://github.com/th-lange/okf-corporate-bundle/issues).

## Development

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # create venv, install package + dev tools
uv run pytest                            # tests
uv run ruff check                        # lint
uv run okf-validate bundles/acme-knowledge bundles/acme-knowledge-restricted
uv run okf-mcp                           # start the MCP server (stdio)
uv run okf-ingest                        # pull configured sources into draft concepts
uv run okf-ingest watch --once           # one background-sync tick (see docs/usage.md)
```

CI runs lint, tests, and the bundle validator on every push.
