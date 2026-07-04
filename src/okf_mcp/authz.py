"""Per-resource authorization and audit logging for resolve_resource.

Resource access is deliberately separate from knowledge read access: a caller
may read *about* a table without being allowed to query it. The stub here is
a config allowlist mapping scope labels to resource URIs; production replaces
it with a real policy engine behind the same `is_allowed` seam. Like
visibility, the rule is set-based: a resource is allowed when a granting
scope is `public` or intersects the caller's scope set.

Every resolve_resource call — allow or deny — produces one audit entry.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import yaml

from okf_mcp.scopes import PUBLIC

_logger = logging.getLogger("okf_mcp.audit")


class ResourceConfigError(ValueError):
    """Raised when the resource grants config is malformed."""


class ResourceAuthorizer:
    """Config-allowlist authorizer: scope label → set of resource URIs."""

    def __init__(self, grants: dict[str, frozenset[str]]) -> None:
        self._grants = {scope: frozenset(uris) for scope, uris in grants.items()}

    @classmethod
    def from_file(cls, path: Path) -> ResourceAuthorizer:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        grants = (raw or {}).get("grants")
        if not isinstance(grants, dict):
            raise ResourceConfigError(f"{path}: resource config must have a `grants` mapping")
        parsed: dict[str, frozenset[str]] = {}
        for scope, uris in grants.items():
            well_formed = (
                isinstance(scope, str)
                and isinstance(uris, list)
                and uris
                and all(isinstance(u, str) and u for u in uris)
            )
            if not well_formed:
                raise ResourceConfigError(
                    f"{path}: grants must map scope labels to lists of resource URIs"
                )
            parsed[scope] = frozenset(uris)
        return cls(parsed)

    def is_allowed(self, caller_scopes: frozenset[str], resource: str) -> bool:
        granting = {scope for scope, uris in self._grants.items() if resource in uris}
        return PUBLIC in granting or bool(granting & caller_scopes)


class AuditLog:
    """Append-only JSONL audit trail; falls back to the audit logger."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def record(self, **event: object) -> None:
        entry = {"ts": datetime.now(UTC).isoformat(), **event}
        line = json.dumps(entry, sort_keys=True)
        if self._path is not None:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        else:
            _logger.info(line)
