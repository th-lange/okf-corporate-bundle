---
type: Metric
title: Monthly Recurring Revenue (MRR)
description: Canonical company-wide definition and computation of MRR.
resource: bigquery://acme-analytics/analytics_core/mrr_daily
tags: [finance, revenue, north-star]
owner: /teams/growth
scope: [public]
timestamp: 2026-07-04T09:00:00Z
---

# Monthly Recurring Revenue (MRR)

**Definition:** see the [MRR term](/glossary/mrr).
**Computation:** sum of active subscription plan values, normalised to monthly, from
[fct_subscription_events](/data/tables/fct-subscription-events), as of the reporting date.
**Grain:** one row per day (`mrr_daily`).
**Business owner:** [Growth](/teams/growth). **Data produced by:** [Platform](/teams/platform).
**Canonical per:** [ADR 0001](/decisions/0001-mrr-single-source-of-truth).
**When numbers look wrong:** [MRR discrepancy runbook](/runbooks/mrr-discrepancy).
**Related:** [Logo churn rate](/metrics/logo-churn-rate).
