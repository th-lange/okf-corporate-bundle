# Demo walkthrough

Two demonstrations against the running MCP server, from a fresh clone:

1. **The MRR investigation** — an agent answers "why did MRR drop?" by
   navigating the knowledge graph instead of guessing.
2. **Same query, three personas** — one search, three different sessions,
   three visibly different result sets.

## 0. Setup

Requires Python ≥ 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/th-lange/okf-corporate-bundle
cd okf-corporate-bundle
uv sync
uv run pytest   # optional sanity check — all tests should pass
```

Register the server in `.mcp.json` at the repo root (Claude Code picks it up
on next start). Start as persona A, a growth analyst:

```json
{
  "mcpServers": {
    "okf-knowledge": {
      "command": "uv",
      "args": ["run", "okf-mcp"],
      "env": { "OKF_TOKEN": "demo-token-a" }
    }
  }
}
```

The personas (see [usage](usage.md) for the full table): no token = public
layer only; `demo-token-a` = `growth`; `demo-token-exco` = `growth, platform,
exco`. The scope set binds when the server starts — changing it means editing
the env and restarting the session, exactly as intended: nothing an agent
says mid-session can widen visibility.

## Part 1 — the MRR investigation

Ask Claude Code:

> Using the okf-knowledge tools only, investigate: why might our MRR number
> have dropped? Find the canonical definition, the backing table, what
> produces it, and the procedure to follow — then get me the table to query.

A well-behaved agent performs this sequence (you can also issue the tool
calls yourself and compare):

**1. `search_concepts("MRR", concept_type="Metric")`** — find the entry point.

```
/metrics/monthly-recurring-revenue   Metric   Monthly Recurring Revenue (MRR)
```

**2. `get_concept("/metrics/monthly-recurring-revenue")`** — the canonical
definition: computation from `fct_subscription_events`, grain (one row per
day), business owner (Growth), the ADR that made it canonical, and the
runbook link.

**3. `follow_links("/metrics/monthly-recurring-revenue")`** — the context
subgraph in one call. As persona A you get exactly:

```
/glossary/mrr                              1 hop
/data/tables/fct-subscription-events       1 hop
/teams/growth                              1 hop
/teams/platform                            1 hop
/decisions/0001-mrr-single-source-of-truth 1 hop
/runbooks/mrr-discrepancy                  1 hop
/metrics/logo-churn-rate                   1 hop
```

(Note what is *not* there: `/systems/billing-service` is linked from the
fct table but scoped to `platform` — it is silently absent, not "denied".)

**4. `get_concept("/runbooks/mrr-discrepancy")`** — the exact diagnostic
steps for a wrong-looking MRR number.

**5. `resolve_resource("/metrics/monthly-recurring-revenue")`** — the actual
data pointer, because persona A's `growth` scope is granted this resource:

```
bigquery://acme-analytics/analytics_core/mrr_daily
```

Five tool calls, zero guessed table names, and the agent can cite the ADR if
anyone disputes the number.

To see the other side of the resource gate: remove `OKF_TOKEN` from
`.mcp.json` (anonymous session), restart, and repeat step 5. The MRR
*concept* is still readable — it is public — but the resolve is refused and
the denial does not contain the URI. Both calls are in the audit log if you
set `OKF_AUDIT_LOG=audit.jsonl` in the server env:

```
{"decision": "allow", "resource": "bigquery://...", "subject": "user-a@acme.test", ...}
{"decision": "deny",  "resource": "bigquery://...", "subject": "anonymous", ...}
```

## Part 2 — same query, three personas

The same `search_concepts("churn")` issued by three different sessions.
Either edit `OKF_TOKEN` in `.mcp.json` and restart between runs, or execute
the deterministic comparison directly against the server code:

```bash
uv run python - <<'EOF'
from pathlib import Path
from okf_mcp.auth import StaticTokenAuthenticator
from okf_mcp.index import OkfIndex

catalog = OkfIndex(Path("bundles/acme-knowledge"), Path("bundles/acme-knowledge-restricted"))
auth = StaticTokenAuthenticator.from_file(Path("config/auth.yaml"))
for label, token in [("anonymous", None), ("user-a", "demo-token-a"), ("exco", "demo-token-exco")]:
    scopes = auth.authenticate(token).scopes
    hits = [d.id for d in catalog.visible_to(scopes).search("churn")]
    print(f"{label:10} {len(hits)} hits: {hits}")
EOF
```

Expected output — three distinct result sets from one identical query:

**Anonymous (public layer) — 3 hits:**

```
/glossary/active-account
/metrics/monthly-recurring-revenue
/teams/growth
```

**Persona A, `growth` — 6 hits** (adds the group-scoped metric and tables;
results are ranked, so the churn-titled metric comes first):

```
/metrics/logo-churn-rate
/data/tables/dim-account
/data/tables/fct-subscription-events
/glossary/active-account
/metrics/monthly-recurring-revenue
/teams/growth
```

**Exco, `growth, platform, exco` — 7 hits** (the restricted bundle appears):

```
/methods/churn-propensity-model        ← restricted bundle, exco only
/metrics/logo-churn-rate
/data/tables/dim-account
/data/tables/fct-subscription-events
/glossary/active-account
/metrics/monthly-recurring-revenue
/teams/growth
```

The point to notice: the anonymous and growth sessions get no hint that
`/methods/churn-propensity-model` exists. It is not marked "restricted" in
their results — it is simply not in their world. Enforcement happened before
indexing, so no query, listing, or link traversal can surface it; and the
exco session needed no special-case code, it merely holds one more scope.

## Where to go next

- [usage.md](usage.md) — running the server, authoring concepts, do's & don'ts
- `tests/test_scopes.py` — the full visibility matrix as executable spec
- `config/auth.yaml`, `config/resources.yaml` — swap in your own personas and grants
