---
type: Policy
title: Data Classification
description: Four sensitivity tiers and how they map to bundle separation and agent access.
tags: [governance, security, access]
owner: /teams/platform
timestamp: 2026-07-03T09:00:00Z
---

# Policy: Data Classification

Every concept carries a `classification`. Tiers map to **separate bundles/repos**, and the
MCP server (this repository's `okf_mcp` package) grants tools per the caller's scope.

| Tier | Example | Bundle | Agent access |
|---|---|---|---|
| public | marketing glossary | `acme-knowledge-public` | any agent |
| internal | this bundle | `acme-knowledge` | authenticated employees/agents |
| confidential | [dim_account](/data/tables/dim-account) | `acme-knowledge` (masked views) | scoped |
| restricted | raw PII, IP methods | `acme-knowledge-restricted` | named service accounts only |

Rule: an agent never *sees* a concept above its scope — the MCP index is filtered before search.
