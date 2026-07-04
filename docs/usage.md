# Usage

How to run the server, consume the knowledge as an agent, and author concepts —
with the do's and don'ts that keep the bundle trustworthy.

## Running the MCP server

```bash
uv sync
uv run okf-mcp        # stdio transport
```

The server exposes one bundle, selected by the `OKF_BUNDLE_DIR` environment
variable (default: `bundles/acme-knowledge`). Example Claude Code registration
(`.mcp.json`):

```json
{
  "mcpServers": {
    "okf-knowledge": {
      "command": "uv",
      "args": ["run", "okf-mcp"],
      "env": { "OKF_BUNDLE_DIR": "bundles/acme-knowledge" }
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

Search and list tools return compact summaries only; fetch bodies via
`get_concept` for just the concepts you need. Navigate the graph — don't crawl
the corpus.

## Authoring concepts

One concept per file; the file path **is** the concept id, so ids are stable and
citable. Frontmatter:

```yaml
---
type: Metric                    # required — the house taxonomy (see below)
title: Monthly Recurring Revenue (MRR)
description: One-line summary shown in search results — keep it tight.
resource: bigquery://acme-analytics/analytics_core/mrr_daily   # optional: the data this describes
tags: [finance, revenue]
owner: /teams/growth            # every concept names an owning team
timestamp: 2026-07-03T09:00:00Z
---
```

Link with bundle-absolute markdown links (`[MRR term](/glossary/mrr)`), and name
the relationship in the surrounding prose ("computed from", "owned by",
"on break: see runbook"). The link asserts the relationship; the prose types it.

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
uv run okf-ingest                  # uses config/ingest.yaml
uv run okf-ingest --config my.yaml
```

Sources live in `config/ingest.yaml` (`staging_dir` plus a `sources` list;
`git` is the available type today — new connectors implement the `Source`
protocol in `src/okf_mcp/ingest/sources.py`). Every draft lands under
`ingest/drafts/<source>/…` stamped with provenance frontmatter: `source:`
(the per-document source URI), `source_rev:` (the revision it was taken
from), and `ingested_at:`. Documents without frontmatter get `type: Document`
so drafts always pass validation; a `Transformer` seam
(`src/okf_mcp/ingest/transform.py`) is where smarter conversion plugs in
later.

The staging directory is gitignored on purpose: drafts reach a bundle only by
a human reviewing them, moving them in, and opening a normal PR.

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
