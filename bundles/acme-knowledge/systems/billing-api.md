---
type: API Endpoint
title: Billing API
description: REST surface for reading and mutating subscription state.
resource: https://api.acme.example/billing/v2/openapi.json
tags: [api, billing]
owner: /teams/platform
classification: internal
timestamp: 2026-07-03T09:00:00Z
---

# Billing API (v2)

Part of [billing-service](/systems/billing-service).

Key routes: `GET /subscriptions/{id}`, `POST /subscriptions`, `POST /subscriptions/{id}/cancel`.
Auth: service-to-service mTLS + scoped tokens. Full contract in the linked OpenAPI `resource`.
