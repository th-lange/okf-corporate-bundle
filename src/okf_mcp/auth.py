"""Pluggable authentication: token → identity → scope set.

This module is the swap-in seam for a real IdP: implement `Authenticator`
against OIDC/OAuth introspection, JWT validation, or whatever the deployment
uses, and hand it to the server — enforcement (`OkfIndex.visible_to`) never
changes. The session's principal is resolved exactly once, at connect time;
scope sets never derive from prompt content or tool input.

The demo implementation is `StaticTokenAuthenticator`, loading a YAML file
that maps bearer tokens to persona users and their scope sets.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from okf_mcp.scopes import declared_scopes


class AuthError(ValueError):
    """Raised when a presented token does not resolve to a principal."""


@dataclass(frozen=True)
class Principal:
    """An authenticated identity and the scope set it holds."""

    subject: str
    scopes: frozenset[str]


ANONYMOUS = Principal(subject="anonymous", scopes=frozenset())


class Authenticator(Protocol):
    """The IdP seam. Implementations resolve a bearer token to a Principal.

    `None` (no token presented) must resolve to `ANONYMOUS` — the public
    layer. Tokens that don't resolve must raise `AuthError` (fail closed),
    never fall back to anonymous.
    """

    def authenticate(self, token: str | None) -> Principal: ...


class StaticTokenAuthenticator:
    """Demo Authenticator: a static YAML config maps tokens to scope sets.

    Config shape:

        users:
          - subject: user-a@acme.test
            token: demo-token-a
            scopes: [growth]
    """

    def __init__(self, principals_by_token: dict[str, Principal]) -> None:
        self._by_token = dict(principals_by_token)

    @classmethod
    def from_file(cls, path: Path) -> StaticTokenAuthenticator:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        users = (raw or {}).get("users")
        if not isinstance(users, list):
            raise AuthError(f"{path}: auth config must have a `users` list")
        by_token: dict[str, Principal] = {}
        for entry in users:
            subject = entry.get("subject") if isinstance(entry, dict) else None
            token = entry.get("token") if isinstance(entry, dict) else None
            scopes = declared_scopes(entry, "scopes") if isinstance(entry, dict) else None
            if not (isinstance(subject, str) and isinstance(token, str) and scopes is not None):
                raise AuthError(f"{path}: every user needs `subject`, `token`, `scopes` (list)")
            if token in by_token:
                raise AuthError(f"{path}: duplicate token for {subject!r}")
            by_token[token] = Principal(subject=subject, scopes=scopes)
        return cls(by_token)

    def authenticate(self, token: str | None) -> Principal:
        if token is None:
            return ANONYMOUS
        principal = self._by_token.get(token)
        if principal is None:
            raise AuthError("unknown token")
        return principal
