---
type: BigQuery Table
title: dim_account_raw
description: Raw, unmasked account dimension including PII. Restricted — never leaves this bundle unmasked.
resource: bigquery://acme-restricted/pii/dim_account_raw
classification: restricted
tags: [pii, restricted, account]
timestamp: 2026-07-04T09:00:00Z
---

# dim_account_raw

Raw source of the masked, internal [dim_account](acme-knowledge:/data/tables/dim-account).

PII columns (`full_name`, `email`, `billing_address`) exist **only** here. The internal bundle
exposes derived/masked columns; the MCP layer denies these raw columns to any non-`restricted` caller.
