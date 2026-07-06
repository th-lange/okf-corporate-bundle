"""Write-back loop (issue #38): proposals go upstream, never into the tree."""

import json
from pathlib import Path

import pytest
from conftest import git

from okf_mcp.authz import AuditLog
from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.server import build_server

UPDATED = """---
type: Document
title: Plain, improved
description: Clarified prose.
---

# Just prose

Improved prose, courtesy of an agent.
"""


@pytest.fixture()
def ws(source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A synced knowledge root over the conftest sector repo."""
    root = tmp_path / "knowledge"
    kb = root / "bundles" / "kb"
    kb.mkdir(parents=True)
    (kb / "index.md").write_text("---\ntype: Index\nscope_default: [public]\n---\n# KB\n")
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "init")
    (root / "ingest.yaml").write_text(
        "sources:\n"
        f"  - name: handbook\n    type: git\n    url: {source_repo}\n    target: kb\n"
    )
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    assert ingest_main(["sync"]) == 0
    return {"root": root, "kb": kb, "repo": source_repo, "audit": tmp_path / "audit.jsonl"}


def server(ws: dict):
    return build_server(
        [ws["root"] / "bundles" / "kb"], scopes=[], audit_log=AuditLog(ws["audit"])
    )


def propose(ws: dict, **overrides):
    args = {
        "concept_id": "/plain",
        "updated_markdown": UPDATED,
        "rationale": "clarify the prose",
    }
    args.update(overrides)
    return server(ws).call_tool("propose_upstream", args)


def proposal_branches(repo: Path) -> list[str]:
    out = git(repo, "branch", "--list", "okf-propose/*", "--format=%(refname:short)")
    return [line for line in out.splitlines() if line]


@pytest.mark.anyio
async def test_proposal_lands_as_branch_upstream(ws: dict) -> None:
    result = await propose(ws)
    payload = json.loads(result[0].text)
    assert payload["kind"] == "branch"

    branches = proposal_branches(ws["repo"])
    assert branches == [payload["ref"]]
    # the proposal is on the branch …
    shown = git(ws["repo"], "show", f"{payload['ref']}:plain.md")
    assert "Improved prose" in shown
    assert "scope" not in shown
    # … and nowhere else: sector HEAD and the knowledge tree are untouched
    assert "Improved prose" not in (ws["repo"] / "plain.md").read_text()
    assert "Improved prose" not in (ws["kb"] / "plain.md").read_text()

    entry = json.loads(ws["audit"].read_text().splitlines()[-1])
    assert entry["tool"] == "propose_upstream" and entry["decision"] == "allow"
    # authored as the session principal
    log = git(ws["repo"], "log", "-1", "--format=%an %B", payload["ref"])
    assert "session" in log and "clarify the prose" in log


@pytest.mark.anyio
async def test_accepted_proposal_returns_via_sync(ws: dict) -> None:
    result = await propose(ws)
    branch = json.loads(result[0].text)["ref"]
    # the sector's own review "accepts" the proposal
    git(ws["repo"], "merge", "--quiet", branch)

    assert ingest_main(["sync"]) == 0
    assert "Improved prose" in (ws["kb"] / "plain.md").read_text()  # loop closed


@pytest.mark.anyio
async def test_scope_fields_are_rejected(ws: dict) -> None:
    poisoned = UPDATED.replace("---\n\n# Just", "scope: [exco]\n---\n\n# Just")
    with pytest.raises(Exception, match="scope fields are rejected"):
        await propose(ws, updated_markdown=poisoned)
    assert proposal_branches(ws["repo"]) == []
    entry = json.loads(ws["audit"].read_text().splitlines()[-1])
    assert entry["decision"] == "deny"


@pytest.mark.anyio
async def test_changed_resource_must_be_in_rationale(ws: dict) -> None:
    with_resource = UPDATED.replace(
        "description: Clarified prose.",
        "description: Clarified prose.\nresource: bigquery://acme/new_table",
    )
    with pytest.raises(Exception, match="verbatim in the rationale"):
        await propose(ws, updated_markdown=with_resource)

    result = await propose(
        ws,
        updated_markdown=with_resource,
        rationale="table moved to bigquery://acme/new_table",
    )
    assert json.loads(result[0].text)["kind"] == "branch"


@pytest.mark.anyio
async def test_hand_maintained_concepts_have_no_upstream(ws: dict) -> None:
    (ws["kb"] / "manual.md").write_text(
        "---\ntype: Note\ntitle: M\ndescription: d\n---\n\nHand-made.\n"
    )
    with pytest.raises(Exception, match="no upstream provenance"):
        await propose(ws, concept_id="/manual")


@pytest.mark.anyio
async def test_non_git_sources_get_a_suggestion_artifact(ws: dict) -> None:
    (ws["kb"] / "faq.md").write_text(
        "---\ntype: Document\ntitle: FAQ\ndescription: d\nsource: gdrive://doc-9\n"
        "source_rev: r1\n---\n\nOld FAQ.\n"
    )
    result = await propose(ws, concept_id="/faq")
    payload = json.loads(result[0].text)
    assert payload["kind"] == "suggestion"
    artifact = ws["root"] / payload["ref"]
    assert artifact.is_file() and "gdrive://doc-9" in artifact.read_text()
    assert "Improved prose" in artifact.read_text()


@pytest.mark.anyio
async def test_out_of_scope_concepts_stay_unknown(ws: dict) -> None:
    (ws["kb"] / "secret.md").write_text(
        "---\ntype: Note\ntitle: S\ndescription: d\nscope: [exco]\n"
        "source: gdrive://x\n---\n\nSecret.\n"
    )
    with pytest.raises(Exception, match="Unknown concept id"):
        await propose(ws, concept_id="/secret")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
