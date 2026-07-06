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
├── auth.py                     pluggable Authenticator (IdP seam) + static demo impl
├── authz.py                    per-resource grants + JSONL audit log
├── server.py                   MCP server (stdio) exposing the tools
├── validator.py                bundle validator CLI (also run in CI)
└── ingest/                     okf-ingest: Source connectors → provenance-stamped drafts
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
| `search_concepts(query, type?, tags?, limit?)` | "Where do I start?" — ranked keyword search (title/aliases > tags > description > body), compact summaries only |
| `list_by_type(type)` | "What metrics/runbooks/… exist?" |
| `get_concept(id)` | "What is the authoritative definition?" — full frontmatter + body |
| `follow_links(id, depth?)` | "What's connected?" — cycle-safe graph traversal, summaries + hop distance |
| `resolve_resource(id)` | "Give me the actual data pointer" — the `resource:` URI, gated by per-resource grants and audit-logged |

List-style tools return summaries (id/type/title/description) — never bodies — so
agents scan cheaply and fetch full text only for the concepts they actually need.

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
```

CI runs lint, tests, and the bundle validator on every push.
