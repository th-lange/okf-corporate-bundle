---
okf_version: "0.1"
type: Index
title: Acme Knowledge Base — RESTRICTED
description: Proprietary IP (trade-secret methods, patents) and raw PII. Named service accounts only.
classification: restricted
timestamp: 2026-07-03T09:00:00Z
---

# Acme Knowledge Base — RESTRICTED

A **separate repo** from `acme-knowledge` (the internal bundle). Same OKF format, different
blast radius: trade-secret methods, patents, and raw PII. Governed by [access policy](/access).

**Cross-bundle references** use `bundle:/concept/id` notation
(e.g. `acme-knowledge:/metrics/logo-churn-rate`), resolved by the MCP layer for callers holding
`restricted` scope — never by plain markdown links, which are bundle-local.

## Directories
- [Methods](/methods) — proprietary algorithms & models (trade secrets)
- [Patents](/patents) — filed / granted patents
- [Data](/data) — raw sensitive tables (PII)
- [Access Policy](/access)
