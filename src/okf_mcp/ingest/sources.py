"""Source connectors: enumerate documents with stable revision ids.

`Source` is the extension seam (issue #15): a connector yields
`SourceDocument`s and nothing else in the ingest loop knows or cares where
they come from. A new source system means one new class here plus config —
no core-loop changes.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class SourceError(RuntimeError):
    """Raised when a source cannot be reached or read."""


@dataclass(frozen=True)
class SourceDocument:
    """One document pulled from a source."""

    source_uri: str  # canonical per-document URI, e.g. "<repo-url>#<path>"
    relative_path: str  # path within the source; decides placement
    revision: str  # stable revision id (commit hash, etag, ...)
    content: str

    @property
    def content_sha256(self) -> str:
        """Content identity — consistency rolls on hashes, not revisions:
        no-op revisions are no-ops here, and the same content at a new path
        (rename) or reappearing after removal (resurrection) is recognisable
        as the same concept.
        """
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


class Source(Protocol):
    """A configured origin of documents."""

    name: str

    def documents(self) -> Iterator[SourceDocument]: ...


@dataclass(frozen=True)
class GitSource:
    """Pull markdown documents from a git repository.

    `url` is anything `git clone` accepts; a path to an existing local clone
    is used in place, unmodified. Each document's revision is the hash of
    the last commit that touched it, so unrelated upstream commits don't
    mark it as changed.

    A fresh checkout is a **partial + sparse** clone scoped to `paths`:
    `--filter=blob:none` defers blob downloads for everything outside the
    checked-out cone, and `--sparse` (non-cone mode, so `paths`' glob
    patterns apply directly) limits the working tree to it. Commit history
    is never shallowed — the per-file revision lookup needs full history,
    not blob content, so it is unaffected.
    """

    name: str
    url: str
    paths: tuple[str, ...] = ("**/*.md",)
    cache_dir: Path = Path(".okf-ingest-cache")

    def documents(self) -> Iterator[SourceDocument]:
        root = self._checkout()
        seen: set[Path] = set()
        for pattern in self.paths:
            for path in sorted(root.glob(pattern)):
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                rel = path.relative_to(root).as_posix()
                revision = _git(root, "log", "-1", "--format=%H", "--", rel).strip()
                if not revision:
                    continue  # untracked file — not part of the source's history
                yield SourceDocument(
                    source_uri=f"{self.url}#{rel}",
                    relative_path=rel,
                    revision=revision,
                    content=path.read_text(encoding="utf-8"),
                )

    def _checkout(self) -> Path:
        local = Path(self.url)
        if (local / ".git").exists():
            return local
        clone = self.cache_dir / self.name
        if (clone / ".git").exists():
            # Partial-clone filter and sparse-checkout patterns persist in
            # the clone's config, so a plain fast-forward pull respects both.
            _git(clone, "pull", "--ff-only", "--quiet")
        else:
            clone.parent.mkdir(parents=True, exist_ok=True)
            self._sparse_clone(clone)
        return clone

    def _sparse_clone(self, clone: Path) -> None:
        try:
            _git(
                Path.cwd(),
                "clone", "--quiet", "--filter=blob:none", "--sparse",
                "--no-checkout", self.url, str(clone),
            )
        except SourceError as exc:
            raise SourceError(f"cannot clone {self.url!r}: {exc}") from exc
        _git(clone, "sparse-checkout", "init", "--no-cone")
        _git(clone, "sparse-checkout", "set", *self.paths)
        _git(clone, "checkout", "--quiet")


def _git(cwd: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
        )
    except subprocess.CalledProcessError as exc:
        raise SourceError(f"git {' '.join(args)} failed: {exc.stderr.strip()}") from exc
    return result.stdout
