---
type: BigQuery Table
title: fct_subscription_events
description: Immutable fact table of subscription lifecycle events (create, upgrade, downgrade, cancel).
resource: bigquery://acme-analytics/analytics_core/fct_subscription_events
tags: [data, subscription, fact-table]
owner: /teams/platform
classification: internal
timestamp: 2026-07-03T09:00:00Z
---

# fct_subscription_events

Grain: one row per subscription lifecycle event.

**Produced by:** [billing-service](/systems/billing-service) via the ingestion pipeline.
**Feeds:** [MRR](/metrics/monthly-recurring-revenue), [Logo churn rate](/metrics/logo-churn-rate).
**Joins to:** [dim_account](/data/tables/dim-account) on `account_id`.
**Classification:** internal — see [data-classification](/policies/data-classification).

## Key columns
| Column | Type | Notes |
|---|---|---|
| event_id | STRING | primary key |
| account_id | STRING | FK -> dim_account |
| event_type | STRING | create / upgrade / downgrade / cancel |
| mrr_delta | NUMERIC | change to MRR in USD |
| event_ts | TIMESTAMP | event time (UTC) |
