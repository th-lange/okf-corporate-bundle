# okf-corporate-bundle

A working example of serving corporate knowledge to AI agents: two [OKF
(Open Knowledge Format)](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundles for a fictional B2B SaaS company ("Acme"), plus an MCP server that exposes
them to agents with set-based, scoped access control.

## What is OKF?

OKF represents knowledge as a directory of Markdown files with YAML frontmatter.
Each file is one *concept* (a metric, table, service, runbook, …) with a small set
of queryable fields (`type` required; `title`, `description`, `resource`, `tags`,
`timestamp` recommended) and a Markdown body. Bundle-relative links between
concepts form a knowledge graph agents can traverse.

There is no schema registry, no central authority, no required tooling: if you can
`cat` a file you can read OKF; if you can `git clone` a repo you can ship it.

## Layout

```
bundles/
├── acme-knowledge/             internal bundle (glossary, metrics, data, systems,
│                               runbooks, playbooks, teams, decisions, policies)
└── acme-knowledge-restricted/  restricted bundle (trade-secret methods, patents, raw PII)
src/okf_mcp/                    MCP server package (work in progress)
tests/
```

> **Production note:** the two bundles live side by side here for demo purposes.
> In a real deployment, sensitivity tiers map to **separate repositories** so access
> control rides on plain git permissions — `acme-knowledge-restricted` would be its
> own repo with its own ACL.

## Roadmap

Development is issue-driven; see the
[issue tracker](https://github.com/th-lange/okf-corporate-bundle/issues). Highlights:
bundle validator + CI, MCP tools (`get_concept`, `list_by_type`, `search_concepts`,
`follow_links`, `resolve_resource`), a set-based scope model with layered defaults
(public / group / inner-exco), and a pluggable auth layer.

## Development

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync            # create venv, install package + dev tools
uv run pytest      # tests
uv run ruff check  # lint
```
