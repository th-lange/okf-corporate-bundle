"""Write-back loop (issue #38): agents propose knowledge updates upstream.

Under source authority nothing writes to the brain except sync — so when an
agent learns something worth keeping (a runbook correction, a missing alias,
a decision), the proposal goes *upstream*, to the owning sector's source,
resolved from the concept's provenance (`source:`). For git sources that
means a branch in the sector's repository, authored as the session's
principal, for the sector's own review process — the only gate that exists —
to accept or reject; the next sync brings accepted knowledge back. Non-git
sources (Drive, S3) have no branch primitive, so the proposal is recorded as
a suggestion artifact under the knowledge root instead of being dropped.

Mechanical rules mirror the rest of the pipeline: scope fields are rejected
outright (visibility belongs to the knowledge tree), pipeline-owned fields
(provenance, flags) are stripped, and a *changed* `resource:` URI must
appear verbatim in the rationale. The proposal never touches the knowledge
tree or the operator repo, and never merges anything anywhere.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from okf_mcp.parser import Document, FrontmatterError, split_frontmatter

# Fields only the pipeline may set; silently stripped from proposals.
_PIPELINE_FIELDS = ("source", "source_rev", "ingested_at", "pii_flag", "needs_human", "gate_findings")


class ProposalError(ValueError):
    """Raised when a proposal violates the mechanical rules or has no viable
    upstream destination."""


@dataclass(frozen=True)
class ProposalResult:
    kind: str  # "branch" (git upstream) or "suggestion" (non-git artifact)
    ref: str  # branch name, or artifact path relative to the knowledge root
    pushed: bool


def propose_upstream(
    doc: Document,
    updated_markdown: str,
    rationale: str,
    subject: str,
    root: Path | None,
) -> ProposalResult:
    source_uri = doc.frontmatter.get("source")
    if not isinstance(source_uri, str) or not source_uri:
        raise ProposalError(
            "this concept has no upstream provenance (`source:`) — it is "
            "hand-maintained; propose the change to its maintainers instead"
        )
    if not rationale.strip():
        raise ProposalError("a proposal needs a rationale")
    text = _sanitize(doc, updated_markdown, rationale)

    if source_uri.startswith(("gdrive://", "s3://")):
        return _suggest(source_uri, text, rationale, subject, root)
    if "#" in source_uri:
        url, _, relpath = source_uri.partition("#")
        return _propose_git(url, relpath, text, rationale, subject)
    raise ProposalError(f"unrecognised provenance {source_uri!r}; cannot locate the upstream")


def _sanitize(doc: Document, updated_markdown: str, rationale: str) -> str:
    try:
        frontmatter, body = split_frontmatter(updated_markdown)
    except FrontmatterError as exc:
        raise ProposalError(f"proposal frontmatter does not parse: {exc}") from None
    if "scope" in frontmatter or "scope_default" in frontmatter:
        raise ProposalError(
            "scope fields are rejected — visibility is assigned by the "
            "knowledge tree, never by content"
        )
    for field in _PIPELINE_FIELDS:
        frontmatter.pop(field, None)
    if not isinstance(frontmatter.get("type"), str) or not frontmatter["type"].strip():
        raise ProposalError("proposal needs a `type` in its frontmatter")
    resource = frontmatter.get("resource")
    if (
        resource is not None
        and resource != doc.frontmatter.get("resource")
        and str(resource) not in rationale
    ):
        raise ProposalError(
            "a changed `resource:` URI must appear verbatim in the rationale"
        )
    rendered = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
    if not body.startswith("\n"):
        body = "\n" + body
    return f"---\n{rendered}---\n{body}"


def _propose_git(
    url: str, relpath: str, text: str, rationale: str, subject: str
) -> ProposalResult:
    local = Path(url)
    cleanup_clone: tempfile.TemporaryDirectory | None = None
    if (local / ".git").exists():
        repo = local
    else:
        cleanup_clone = tempfile.TemporaryDirectory(prefix="okf-propose-clone-")
        repo = Path(cleanup_clone.name)
        clone = subprocess.run(
            ["git", "clone", "--quiet", url, str(repo)], capture_output=True, text=True
        )
        if clone.returncode != 0:
            cleanup_clone.cleanup()
            raise ProposalError(f"cannot clone upstream {url!r}: {clone.stderr.strip()}")

    slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-") or "agent"
    branch = f"okf-propose/{datetime.now(UTC):%Y%m%d}-{slug}-{uuid.uuid4().hex[:6]}"
    try:
        # A worktree keeps the sector's checkout (HEAD, working tree) untouched.
        with tempfile.TemporaryDirectory(prefix="okf-propose-wt-") as wt:
            _git(repo, "worktree", "add", "--quiet", "-b", branch, wt)
            try:
                target = Path(wt) / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
                _git(Path(wt), "add", relpath)
                _git(
                    Path(wt),
                    "-c", f"user.name={subject}",
                    "-c", "user.email=write-back@okf.agents",
                    "commit", "--quiet",
                    "-m", f"Proposed update to {relpath}\n\n{rationale}\n\n"
                    f"Proposed-by: {subject} (agent write-back; review upstream)",
                )
            finally:
                _git(repo, "worktree", "remove", "--force", wt)
        pushed = False
        has_remote = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True
        )
        if has_remote.returncode == 0:
            push = subprocess.run(
                ["git", "-C", str(repo), "push", "--quiet", "-u", "origin", branch],
                capture_output=True,
            )
            pushed = push.returncode == 0
        return ProposalResult(kind="branch", ref=branch, pushed=pushed)
    finally:
        if cleanup_clone is not None:
            cleanup_clone.cleanup()


def _suggest(
    source_uri: str, text: str, rationale: str, subject: str, root: Path | None
) -> ProposalResult:
    if root is None:
        raise ProposalError(
            "this source has no branch primitive and no knowledge root is "
            "configured — nowhere to record the suggestion"
        )
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source_uri)
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    path = root / "ingest" / "proposals" / f"{stamp}-{safe}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"<!-- proposed update for {source_uri}\n"
        f"     by: {subject} (agent write-back)\n"
        f"     rationale: {rationale}\n"
        "     apply this in the owning source system; the next sync picks it up. -->\n"
    )
    path.write_text(header + text, encoding="utf-8")
    return ProposalResult(kind="suggestion", ref=str(path.relative_to(root)), pushed=False)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise ProposalError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout
