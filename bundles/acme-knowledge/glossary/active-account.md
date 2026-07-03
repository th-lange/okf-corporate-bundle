---
type: Term
title: Active Account
description: An account with at least one non-cancelled paid subscription in the period.
tags: [subscription, definitions]
timestamp: 2026-07-03T09:00:00Z
---

# Active Account

An account is **active** in a period if it holds >= 1 paid, non-cancelled subscription
at period end. Trials and fully-churned accounts are excluded.

- Used by: [Logo churn rate](/metrics/logo-churn-rate).
- Backing data: [dim_account](/data/tables/dim-account).
