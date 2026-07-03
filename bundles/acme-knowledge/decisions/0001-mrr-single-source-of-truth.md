---
type: Decision
title: ADR 0001 — Single Source of Truth for MRR
description: MRR has exactly one canonical definition and computation, owned by Growth.
tags: [adr, finance, governance]
status: accepted
owner: /teams/growth
timestamp: 2026-06-28T16:00:00Z
---

# ADR 0001 — Single Source of Truth for MRR

**Status:** accepted (2026-06-28)

**Context:** Finance, Sales, and Product each maintained slightly different MRR formulas,
producing conflicting board numbers.

**Decision:** One canonical [MRR metric](/metrics/monthly-recurring-revenue), defined via the
[MRR term](/glossary/mrr), computed from [fct_subscription_events](/data/tables/fct-subscription-events).
All dashboards must reference it; no local re-definitions.

**Consequences:** Divergent numbers become a data/pipeline bug (see the
[runbook](/runbooks/mrr-discrepancy)), not a definitional argument. New metrics follow the
[define-a-new-metric playbook](/playbooks/define-a-new-metric).
