---
type: Metric
title: Logo Churn Rate
description: Share of active accounts that fully cancelled during the period.
resource: bigquery://acme-analytics/analytics_core/logo_churn_monthly
aliases: [customer cancellations, logo churn, account churn]
tags: [retention, churn]
owner: /teams/growth
timestamp: 2026-07-04T09:00:00Z
---

# Logo Churn Rate

Count of [active accounts](/glossary/active-account) at period start that hold no paid
subscription at period end, divided by active accounts at start.

**Backing data:** [dim_account](/data/tables/dim-account),
[fct_subscription_events](/data/tables/fct-subscription-events).
**Business owner:** [Growth](/teams/growth).
**Related:** [MRR](/metrics/monthly-recurring-revenue).
