# okf-corporate-bundle

A working example of serving corporate knowledge to AI agents: two
[OKF (Open Knowledge Format)](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundles for a fictional B2B SaaS company ("Acme"), plus an MCP server that exposes
them to agents with set-based, scoped access control.

> **Docs:** [Demo walkthrough](docs/demo.md) · [Usage — do's and don'ts](docs/usage.md) · [Agent entry point](AGENTS.md)

## Why

Most agent inefficiency isn't reasoning failure — it's *context starvation*. An agent
asked "why did MRR drop?" that doesn't know the company's MRR definition, which table
backs it, or who owns it will guess, hallucinate a plausible-but-wrong query, or bounce
the question back to a human. A curated, cross-linked, permissioned knowledge bundle
removes the starvation: the authoritative answer is one tool call away, and the related
answers are one link-hop further. Agents *look things up* instead of guessing — and they
navigate the graph instead of crawling the corpus, so context stays small no matter how
large the knowledge base grows.

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
<<<<<<< HEAD
=======
<<<<<<< HEAD
=======
config/auth.yaml                demo token → scope-set assignments (persona users)
config/resources.yaml           per-resource authorization grants (scope → URIs)
>>>>>>> origin/main
>>>>>>> origin/main
config/ingest.yaml              ingest sources (demo: this repo's own docs/)
src/okf_mcp/                    MCP server package
├── parser.py                   frontmatter + link extraction
├── index.py                    in-memory index: lookup, search, graph traversal
├── scopes.py                   effective-scope resolution + visibility rule
├── auth.py                     pluggable Authenticator (IdP seam) + static demo impl
├── authz.py                    per-resource grants + JSONL audit log
├── server.py                   MCP server (stdio) exposing the tools
├── validator.py                bundle validator CLI (also run in CI)
└── ingest/                     okf-ingest: Source connectors → provenance-stamped drafts
<<<<<<< HEAD
=======
<<<<<<< HEAD
=======
docs/demo.md                    end-to-end walkthrough: MRR investigation + personas
>>>>>>> origin/main
>>>>>>> origin/main
docs/usage.md                   how to run, author, and consume the bundles
tests/
```

> **Production note:** the two bundles live side by side here for demo purposes.
> In a real deployment, sensitivity tiers map to **separate repositories** so access
> control rides on plain git permissions — `acme-knowledge-restricted` would be its
> own repo with its own ACL.

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
| `search_concepts(query, type?, tags?)` | "Where do I start?" — keyword search, compact summaries only |
| `list_by_type(type)` | "What metrics/runbooks/… exist?" |
| `get_concept(id)` | "What is the authoritative definition?" — full frontmatter + body |
| `follow_links(id, depth?)` | "What's connected?" — cycle-safe graph traversal, summaries + hop distance |
| `resolve_resource(id)` | "Give me the actual data pointer" — the `resource:` URI, gated by per-resource grants and audit-logged |

List-style tools return summaries (id/type/title/description) — never bodies — so
agents scan cheaply and fetch full text only for the concepts they actually need.

## Roadmap

Development is issue-driven; every issue carries acceptance criteria and explicit
blockers. Two independent tracks are open on the
[issue tracker](https://github.com/th-lange/okf-corporate-bundle/issues):

- **Access control** (#6 → #7 → #8 → #9): set-based scope model with layered
  defaults (public / group / inner-exco), a pluggable token-to-scope-set auth
  layer, authz-gated `resolve_resource`, and an end-to-end demo walkthrough.
- **Ingestion** (#15 → #16 → #17/#18): `okf-ingest` CLI pulling documents from
  configurable sources (git, Google Drive, S3) behind a small `Source` interface,
  producing provenance-stamped **draft** concepts for human review — the ingester
  proposes, never publishes — plus a ledger tracking what's ingested and what
  changed or vanished upstream.

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
