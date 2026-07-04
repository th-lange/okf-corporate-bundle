"""resolve_resource (issue #8): per-resource authz, audit-logged."""

import json
from pathlib import Path

import pytest

from okf_mcp.authz import AuditLog, ResourceAuthorizer
from okf_mcp.server import build_server

REPO_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_CONFIG = REPO_ROOT / "config" / "resources.yaml"
BUNDLES = (
    REPO_ROOT / "bundles" / "acme-knowledge",
    REPO_ROOT / "bundles" / "acme-knowledge-restricted",
)

MRR = "/metrics/monthly-recurring-revenue"
MRR_TABLE = "bigquery://acme-analytics/analytics_core/mrr_daily"


def read_audit(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture(scope="module")
def authorizer() -> ResourceAuthorizer:
    return ResourceAuthorizer.from_file(RESOURCE_CONFIG)


def test_grants_are_set_based(authorizer: ResourceAuthorizer) -> None:
    assert authorizer.is_allowed(frozenset({"growth"}), MRR_TABLE)
    assert not authorizer.is_allowed(frozenset({"platform"}), MRR_TABLE)
    # public grants resolve for any session, including anonymous
    assert authorizer.is_allowed(frozenset(), "https://acme.example/teams/growth")


@pytest.mark.anyio
async def test_authorized_caller_receives_uri(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    server = build_server(BUNDLES, token="demo-token-a", audit_log=AuditLog(audit))
    result = await server.call_tool("resolve_resource", {"concept_id": MRR})
    assert MRR_TABLE in result[0].text

    (entry,) = read_audit(audit)
    assert entry["decision"] == "allow"
    assert entry["subject"] == "user-a@acme.test"
    assert entry["resource"] == MRR_TABLE


@pytest.mark.anyio
async def test_denied_caller_never_sees_uri(tmp_path: Path) -> None:
    # Anonymous callers can READ the MRR concept (it is public) but must not
    # resolve its table — and the denial must not contain the URI.
    audit = tmp_path / "audit.jsonl"
    server = build_server(BUNDLES, scopes=[], audit_log=AuditLog(audit))
    with pytest.raises(Exception, match="denied") as excinfo:
        await server.call_tool("resolve_resource", {"concept_id": MRR})
    assert MRR_TABLE not in str(excinfo.value)
    assert "bigquery" not in str(excinfo.value)

    (entry,) = read_audit(audit)
    assert entry["decision"] == "deny"
    assert entry["resource"] == MRR_TABLE  # the server-side log does record it


@pytest.mark.anyio
async def test_concept_without_resource(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    server = build_server(BUNDLES, token="demo-token-a", audit_log=AuditLog(audit))
    with pytest.raises(Exception, match="declares no resource"):
        await server.call_tool("resolve_resource", {"concept_id": "/glossary/mrr"})
    (entry,) = read_audit(audit)
    assert entry["decision"] == "no-resource"


@pytest.mark.anyio
async def test_out_of_scope_concept_is_unknown_and_audited(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    server = build_server(BUNDLES, token="demo-token-a", audit_log=AuditLog(audit))
    with pytest.raises(Exception, match="Unknown concept id"):
        await server.call_tool(
            "resolve_resource", {"concept_id": "/methods/churn-propensity-model"}
        )
    (entry,) = read_audit(audit)
    assert entry["decision"] == "unknown-concept"
    assert "resource" not in entry


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
