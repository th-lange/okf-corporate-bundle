# Usage

How to run the server, consume the knowledge as an agent, and author concepts —
with the do's and don'ts that keep the bundle trustworthy.

## Running the MCP server

```bash
uv sync
uv run okf-mcp        # stdio transport
```

Environment variables configure a session; everything is bound once at
startup and no tool accepts scopes or tokens as input, so prompt content can
never widen visibility:

- `OKF_KNOWLEDGE_ROOT` — the external knowledge tree (see
  [Deployment](#deployment)); every bundle under `<root>/bundles/` is served.
- `OKF_BUNDLE_DIRS` — explicit bundle list, separated by the OS path
  separator (`:` on Linux/macOS); overrides the knowledge root. With neither
  set, the repo's demo fixtures are served.
- `OKF_TOKEN` — bearer token, resolved to a scope set by the auth layer.
- `OKF_AUTH_CONFIG` — auth config path (default: `config/auth.yaml`).
- `OKF_SCOPES` — comma-separated scope labels; local dev override used only
  when no token is presented. Neither set means public-layer only.
- `OKF_RESOURCE_CONFIG` — per-resource grants for `resolve_resource`
  (default: `config/resources.yaml`).
- `OKF_AUDIT_LOG` — file receiving one JSONL audit entry per
  `resolve_resource` call (allow and deny); unset logs via `okf_mcp.audit`.

The demo auth config defines five personas:

| Token | Subject | Scopes |
|---|---|---|
| `demo-token-a` | user-a@acme.test | `growth` |
| `demo-token-b` | user-b@acme.test | `platform` |
| `demo-token-ab` | user-ab@acme.test | `growth, platform` |
| `demo-token-c` | user-c@acme.test | `finance` (no matching concepts → public only) |
| `demo-token-exco` | exco@acme.test | `growth, platform, exco` |

Swapping in a real IdP means implementing the `Authenticator` protocol
(`src/okf_mcp/auth.py`) — token in, subject + scope set out; enforcement does
not change. Unknown tokens fail closed; no token means anonymous
(public layer).

Example Claude Code registration (`.mcp.json`) for a growth-scoped session:

```json
{
  "mcpServers": {
    "okf-knowledge": {
      "command": "uv",
      "args": ["run", "okf-mcp"],
      "env": { "OKF_TOKEN": "demo-token-a" }
    }
  }
}
```

## Consuming knowledge (a typical investigation)

Concept ids are bundle-relative paths (`/metrics/monthly-recurring-revenue`).
The intended flow — using "why did MRR drop?" as the example:

1. `search_concepts("MRR", concept_type="Metric")` → find the entry point.
2. `get_concept("/metrics/monthly-recurring-revenue")` → the canonical
   definition, the backing table (`resource:`), the owner, and links onward.
3. `follow_links("/metrics/monthly-recurring-revenue")` → the backing table,
   producing service, owning team, and runbook in one call.
4. `get_concept("/runbooks/mrr-discrepancy")` → the exact diagnostic steps.
5. `resolve_resource("/metrics/monthly-recurring-revenue")` → *if this
   session's scopes are granted that resource*, the exact BigQuery table URI.

Search and list tools return compact summaries only; fetch bodies via
`get_concept` for just the concepts you need. Navigate the graph — don't crawl
the corpus.

Resource access is separate from knowledge read access: anyone can *read
about* MRR (it's a public concept), but only sessions holding a granting
scope can resolve its table URI. Denials never include the URI; every call,
allowed or denied, lands in the audit log.

## Authoring concepts

One concept per file; the file path **is** the concept id, so ids are stable and
citable. Frontmatter:

```yaml
---
type: Metric                    # required — the house taxonomy (see below)
title: Monthly Recurring Revenue (MRR)
description: One-line summary shown in search results — keep it tight.
resource: bigquery://acme-analytics/analytics_core/mrr_daily   # optional: the data this describes
aliases: [monthly recurring revenue]   # optional: synonyms searchers actually use
tags: [finance, revenue]
owner: /teams/growth            # every concept names an owning team
timestamp: 2026-07-03T09:00:00Z
---
```

Search is ranked (title and aliases outrank tags, then description, then
body) and result-limited, so `aliases:` is the recall lever: when an agent
misses a concept under a reasonable phrasing, add that phrasing as an alias —
curated, deterministic, and reviewable, no embedding infrastructure needed.

### Scoping

Visibility is controlled by scope labels, resolved with layered defaults:
a concept's own `scope:` list wins; otherwise the nearest ancestor `index.md`
with a `scope_default:` applies, falling back to the bundle root's default and
finally to `public`. A concept is visible when its effective scope contains
`public` or intersects the caller's scope set — there is no hierarchy logic;
broader roles simply hold more scopes.

Prefer directory-level `scope_default:` (set it in the directory's `index.md`)
and use concept-level `scope:` only for deliberate exceptions — e.g. MRR is
explicitly `public` while `metrics/` defaults to `growth`. Out-of-scope
concepts are omitted entirely: they cannot be listed, searched, retrieved, or
reached via `follow_links`, and look exactly like ids that don't exist.

### Links

Link with bundle-absolute markdown links (`[MRR term](/glossary/mrr)`), and name
the relationship in the surrounding prose ("computed from", "owned by",
"on break: see runbook"). The link asserts the relationship; the prose types it.

**Cross-bundle references** use the qualified form
`[logo churn rate](acme-knowledge:/metrics/logo-churn-rate)` — the prefix is
the bundle's directory name. The MCP layer resolves these only when the named
bundle is served *and* the target is within the caller's scopes: the edge
exists only for callers who can see both sides, and for everyone else there is
no trace of it. Links into bundles a session doesn't serve are inert (bundles
stay independently shippable); `okf-validate` cross-checks qualified links
when the named bundle is part of the same validation run, and skips them when
a bundle is validated alone.

The house taxonomy maps directories to types and to the question each answers:
`glossary/` (Term), `metrics/` (Metric), `data/` (BigQuery Table, Dataset),
`systems/` (Service, API Endpoint), `runbooks/` (Runbook), `playbooks/`
(Playbook), `teams/` (Team), `decisions/` (Decision), `policies/` (Policy).
Consumers must tolerate unknown types, so adding a type never breaks anyone.

Before opening a PR:

```bash
uv run okf-validate bundles/acme-knowledge bundles/acme-knowledge-restricted
```

and record the change in the bundle's `log.md`.

## Ingesting external documents

`okf-ingest` pulls documents from configured sources and proposes them as
**draft** concepts — it never writes into a served bundle:

```bash
uv run okf-ingest                  # ingest new/modified docs (config/ingest.yaml)
uv run okf-ingest status           # what's new / unchanged / modified / removed
uv run okf-ingest --config my.yaml
```

Sources live in `config/ingest.yaml` (`staging_dir` plus a `sources` list).
Available types — new connectors implement the `Source` protocol in
`src/okf_mcp/ingest/sources.py`:

- `git` — `url` (local path or anything `git clone` accepts) + optional
  `paths` glob patterns. Revision = last commit touching the file.
- `gdrive` — `folder_id` of a Drive folder. Native Google Docs are exported
  as markdown, `*.md` files downloaded as-is, everything else skipped.
  Revision = `headRevisionId` (falling back to `modifiedTime`). Credentials
  come from the `GOOGLE_DRIVE_TOKEN` env var (an OAuth bearer token with
  `drive.readonly` scope) — never from config files.
- `s3` — `bucket` + optional `prefix`; `*.md` objects only. Revision = the
  object's ETag. Requires the `s3` extra (`uv sync --extra s3`); credentials
  come from the standard AWS chain (env vars, profile, instance role). Every draft lands under
`ingest/drafts/<source>/…` stamped with provenance frontmatter: `source:`
(the per-document source URI), `source_rev:` (the revision it was taken
from), and `ingested_at:`. Documents without frontmatter get `type: Document`
so drafts always pass validation; a `Transformer` seam
(`src/okf_mcp/ingest/transform.py`) is where smarter conversion plugs in
later.

### LLM-assisted conversion (`transformer: llm`)

For sources that aren't OKF-shaped markdown (Drive exports, plain prose), set
`transformer: llm` on the source entry. The design is deliberately rigid,
because source documents are untrusted input:

- The **worker** is one toolless Claude call per document (official SDK,
  `uv sync --extra llm`, key from `ANTHROPIC_API_KEY`, model override via
  `OKF_LLM_MODEL`). It gets the house type taxonomy and compact summaries of
  the concepts in `catalog_bundles` for link proposals — and nothing else.
- The **gate** is deterministic code, not another LLM: required
  type/title/description; every proposed link must resolve to a catalog
  concept; `scope:`/`scope_default:` are stripped unconditionally; a
  `resource:` URI must appear verbatim in the source or is dropped; PII
  patterns set `pii_flag: true` for restricted-tier review; provenance is
  stamped by the pipeline, never by the model.
- Gate findings are fed back to the worker at most twice; then the draft is
  written with `needs_human: true` and the findings attached.

Injected instructions in a source document ("add scope: [exco]") have nothing
to grab: the worker has no tools, and the gate strips or rejects anything the
policy forbids. Human PR review remains the final gate.

The **ledger** (`ingest/ledger.yaml`, committed) gives full visibility into
what has been ingested: one entry per source document with its URI, revision,
draft path, and ingest time. `okf-ingest status` compares current source
revisions against it and classifies every document as new / unchanged /
modified / removed. Re-running ingest regenerates drafts **only** for new and
modified documents; documents that vanished upstream are flagged in the
ledger (`removed_at`) and reported — never deleted. Retiring the concept a
removed document produced is a human decision.

The staging directory is gitignored on purpose: drafts reach a bundle only by
a human reviewing them, moving them in, and opening a normal PR.

## Deployment

This repo is the **operator** — a self-contained tool. The **knowledge** it
serves lives outside, under a single knowledge root (`OKF_KNOWLEDGE_ROOT`):

```
<knowledge-root>/            typically a mounted volume; each bundle its own
├── bundles/                 git repo in production (per sensitivity tier)
│   ├── acme-knowledge/
│   └── acme-knowledge-restricted/
├── ingest.yaml              ingest source configuration
└── ingest/
    ├── drafts/              staging written by okf-ingest
    └── ledger.yaml          ingest ledger
```

With a root configured, the server serves every bundle under
`<root>/bundles/`, and okf-ingest reads `<root>/ingest.yaml` and keeps its
staging and ledger under the root — the operator never writes into its own
tree. Foreign sources (git repos, Drive folders, S3 buckets) flow in through
the ingester; the knowledge root is where their drafts and provenance land.
Without a root, the bundled demo fixtures keep the fresh-clone experience
working.

Containerized:

```bash
docker build -t okf-operator .
docker run -i --rm \
  -v /srv/acme-knowledge:/knowledge \
  -e OKF_KNOWLEDGE_ROOT=/knowledge \
  -e OKF_TOKEN=demo-token-a \
  okf-operator
```

(`-i` because MCP speaks over stdio.) Auth and resource-grant configs default
to the demo files baked into the image; point `OKF_AUTH_CONFIG` /
`OKF_RESOURCE_CONFIG` at mounted files for real deployments.

## Do's

- **Curate narrow and correct.** A small corpus that is never wrong beats a big
  one that is occasionally wrong — trust, once lost, doesn't come back. Add a
  concept the first time an agent needed it and couldn't find it.
- **Keep ids stable.** Links are the product; renames break the graph. If you
  must move a concept, update every inbound link in the same change.
- **Organise by knowledge domain, not org chart.** Teams reorg; the questions
  ("what's the metric?", "where's the data?") are stable. Ownership is an
  attribute *on* concepts, not the directory structure.
- **Name an owner on every concept.** Ownership drives accountability,
  freshness, and escalation.
- **Cross-link deliberately.** A metric should link its table, its owner, its
  runbook, and the decision that made it canonical. Agents traverse; they don't
  re-derive.
- **Keep descriptions tight.** The one-line `description` is what every search
  result carries — it's the primary defence against context bloat.
- **Timestamp and log.** Update `timestamp:` when content changes and append to
  `log.md`, so staleness is visible instead of silently trusted.
- **Classify into the right bundle.** Sensitivity maps to bundle separation;
  restricted material goes in the restricted bundle, full stop.
- **Route all additions through PR review** — including agent- or
  ingester-proposed concepts. The ingester proposes, never publishes.

## Don'ts

- **Don't dump the whole wiki in.** Bulk imports kill findability and trust.
  Start narrow (metrics + data + runbooks pay back first) and grow by demand.
- **Don't structure by team or org chart.** It churns on every reorg and
  orphans links.
- **Don't put PII or secrets in concept bodies.** Keep raw sensitive data in
  the restricted bundle behind `resource:` URIs, never inline. `teams/` stores
  roles and channels, not individuals.
- **Don't let definitions drift into dashboards.** The bundle is the single
  source of truth for definitions; that divergence is exactly what
  [ADR 0001](../bundles/acme-knowledge/decisions/0001-mrr-single-source-of-truth.md)
  ended.
- **Don't serve restricted content from a general session.** Sensitivity tiers
  are separate bundles (separate repos in production) precisely so a normal
  caller can't even enumerate them.
- **Don't trust retrieved bodies blindly.** Treat every retrieved document as
  potentially containing indirect prompt injection; enforcement (scoping,
  masking, audit) belongs in the MCP layer, not in the model's goodwill.
