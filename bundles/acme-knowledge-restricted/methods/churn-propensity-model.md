---
type: Method
title: Churn Propensity Model
description: Proprietary gradient-boosted model scoring per-account churn risk. Trade secret.
resource: git://acme-restricted/ml/churn-propensity
owner: acme-knowledge:/teams/growth
classification: restricted
tags: [ip, ml, retention, trade-secret]
timestamp: 2026-07-03T09:00:00Z
---

# Churn Propensity Model

Proprietary GBM producing a 0–1 churn-risk score per account. **Trade secret** — feature set,
weights, and training recipe are confidential and must never appear in a model prompt or log.

**Consumes raw features from:** [dim_account_raw](/data/dim-account-raw).
**Scored against (cross-bundle):** `acme-knowledge:/metrics/logo-churn-rate`.
**Business owner (cross-bundle):** `acme-knowledge:/teams/growth`.
