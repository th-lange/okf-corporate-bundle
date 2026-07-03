---
type: Runbook
title: MRR Discrepancy
description: Diagnose and resolve mismatches between reported MRR and expected MRR.
tags: [oncall, finance, incident]
owner: /teams/platform
timestamp: 2026-07-03T09:00:00Z
---

# Runbook: MRR Discrepancy

Trigger: reported [MRR](/metrics/monthly-recurring-revenue) diverges from finance's expectation
by > 1%.

## Steps
1. Check [billing-service](/systems/billing-service) event lag (dashboard in resource of the service).
2. Verify late/duplicate rows in [fct_subscription_events](/data/tables/fct-subscription-events)
   for the affected day (`event_ts` vs ingest time).
3. Recompute `mrr_daily`; compare to snapshot.
4. If event loss confirmed, page on-call [Platform](/teams/platform); if definition dispute,
   escalate to [Growth](/teams/growth) and re-read [ADR 0001](/decisions/0001-mrr-single-source-of-truth).
