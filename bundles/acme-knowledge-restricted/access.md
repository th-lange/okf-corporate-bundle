---
type: Policy
title: Restricted Access Policy
description: Who and what may read this bundle, and how cross-bundle references resolve.
classification: restricted
tags: [governance, security, access]
owner: acme-knowledge:/teams/platform
timestamp: 2026-07-03T09:00:00Z
---

# Policy: Restricted Access

- Access limited to **named service accounts**, granted per-concept and time-boxed.
- Requires MCP scope `restricted`; the `internal`-scope index never contains these concepts,
  so a normal agent cannot search, retrieve, or even enumerate them.
- Every `get_concept` / `resolve_resource` call is audit-logged; break-glass needs approval.
- **Cross-bundle rule:** an internal concept may *name* a restricted one via `bundle:/id`, but the
  MCP resolves it only for callers already holding `restricted` scope — otherwise it returns "exists, denied".
