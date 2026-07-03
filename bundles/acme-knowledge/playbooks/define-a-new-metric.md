---
type: Playbook
title: Define a New Metric
description: The standard workflow for proposing, defining, and publishing a canonical metric.
tags: [process, metrics, governance]
owner: /teams/growth
timestamp: 2026-07-03T09:00:00Z
---

# Playbook: Define a New Metric

1. Add/confirm the underlying [terms](/glossary) so language is unambiguous.
2. Identify backing tables in the [data catalog](/data); confirm grain and lineage.
3. Draft a `type: Metric` concept under [/metrics](/metrics) with `resource`, `owner`, definition.
4. If it contradicts or supersedes an existing number, record an ADR under [/decisions](/decisions).
5. Confirm classification against [data-classification](/policies/data-classification).
6. Open a PR; review by business owner ([Growth](/teams/growth)) + data owner ([Platform](/teams/platform)).
