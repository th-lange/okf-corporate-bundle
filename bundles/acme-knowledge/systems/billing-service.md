---
type: Service
title: Billing Service
description: System of record for subscription state; emits subscription lifecycle events.
resource: https://git.acme.example/platform/billing-service
tags: [service, billing, subscription]
owner: /teams/platform
classification: internal
timestamp: 2026-07-03T09:00:00Z
---

# Billing Service

Owns subscription state transitions and emits events consumed downstream.

**Exposes:** [billing-api](/systems/billing-api).
**Emits into:** [fct_subscription_events](/data/tables/fct-subscription-events).
**Owned by:** [Platform](/teams/platform).
**On incident:** [MRR discrepancy runbook](/runbooks/mrr-discrepancy).
