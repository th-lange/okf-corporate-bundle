---
type: BigQuery Table
title: dim_account
description: Slowly-changing dimension of customer accounts and their attributes.
resource: bigquery://acme-analytics/analytics_core/dim_account
tags: [data, account, dimension]
owner: /teams/platform
classification: confidential
timestamp: 2026-07-03T09:00:00Z
---

# dim_account

Grain: one row per account per attribute-version (SCD type 2).

**Classification:** confidential — contains customer identifiers. Access is scoped;
see [data-classification](/policies/data-classification). PII columns are masked in the
`internal` view; raw columns live only in the `restricted` bundle.
**Used by:** [Active Account](/glossary/active-account), [Logo churn rate](/metrics/logo-churn-rate).

## Key columns
| Column | Type | Notes |
|---|---|---|
| account_id | STRING | primary key |
| plan_tier | STRING | free / pro / enterprise |
| region | STRING | billing region |
| is_active | BOOL | current active flag |
