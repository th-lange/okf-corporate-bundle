"""GitSource sparse/partial clone (issue #43): narrow `paths` narrows the fetch.

A fresh checkout of a remote-shaped URL (not an already-existing local
checkout — that path is used in place, unmodified, and never sparsified)
must clone with --filter=blob:none --sparse and materialize only the
configured `paths`, while keeping full commit history so per-file revision
lookup is unaffected.
"""

from pathlib import Path

import pytest
from conftest import git

from okf_mcp.ingest.sources import GitSource


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    """A repo with multiple folders, only one of which we'll scope to."""
    repo = tmp_path / "upstream"
    (repo / "docs").mkdir(parents=True)
    (repo / "policies").mkdir(parents=True)
    (repo / "bin").mkdir(parents=True)
    (repo / "docs" / "a.md").write_text("# Doc A\n")
    (repo / "policies" / "b.md").write_text("# Policy B\n")
    (repo / "bin" / "blob.dat").write_bytes(b"\x00" * 4096)  # not a source doc
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo


def as_remote(repo: Path) -> str:
    """A URL git will actually clone (not the local-checkout-in-place path)."""
    return f"file://{repo}"


def test_fresh_checkout_materializes_only_configured_paths(
    upstream: Path, tmp_path: Path
) -> None:
    source = GitSource(
        name="scoped",
        url=as_remote(upstream),
        paths=("docs/**/*.md",),
        cache_dir=tmp_path / "cache",
    )
    docs = {d.relative_path for d in source.documents()}
    assert docs == {"docs/a.md"}  # policies/ and bin/ never even asked for

    clone = source._checkout()
    materialized = {
        p.relative_to(clone).as_posix()
        for p in clone.rglob("*")
        if p.is_file() and ".git" not in p.relative_to(clone).parts
    }
    assert materialized == {"docs/a.md"}  # working tree matches the scope exactly
    assert (clone / ".git" / "info" / "sparse-checkout").read_text().strip() == "docs/**/*.md"


def test_default_paths_still_finds_everything_markdown(upstream: Path, tmp_path: Path) -> None:
    source = GitSource(name="all", url=as_remote(upstream), cache_dir=tmp_path / "cache")
    docs = {d.relative_path for d in source.documents()}
    assert docs == {"docs/a.md", "policies/b.md"}  # bin/blob.dat is not markdown


def test_revision_lookup_unaffected_by_sparse_scope(upstream: Path, tmp_path: Path) -> None:
    source = GitSource(
        name="scoped",
        url=as_remote(upstream),
        paths=("docs/**/*.md",),
        cache_dir=tmp_path / "cache",
    )
    (doc,) = list(source.documents())
    expected = git(upstream, "log", "-1", "--format=%H", "--", "docs/a.md").strip()
    assert doc.revision == expected


def test_existing_local_checkout_is_used_in_place_never_sparsified(
    upstream: Path, tmp_path: Path
) -> None:
    # url points directly at an existing local clone (not a remote to fetch)
    source = GitSource(
        name="local", url=str(upstream), paths=("docs/**/*.md",), cache_dir=tmp_path / "cache"
    )
    docs = {d.relative_path for d in source.documents()}
    assert docs == {"docs/a.md"}
    # the caller's own checkout was never touched — no sparse-checkout config,
    # every file still present
    assert not (upstream / ".git" / "info" / "sparse-checkout").exists()
    assert (upstream / "policies" / "b.md").exists()
    assert not (tmp_path / "cache").exists()


def test_update_via_pull_respects_the_sparse_scope(upstream: Path, tmp_path: Path) -> None:
    source = GitSource(
        name="scoped",
        url=as_remote(upstream),
        paths=("docs/**/*.md",),
        cache_dir=tmp_path / "cache",
    )
    list(source.documents())  # initial sparse clone

    (upstream / "docs" / "a.md").write_text("# Doc A v2\n")
    (upstream / "policies" / "b.md").write_text("# Policy B v2\n")
    git(upstream, "commit", "--quiet", "-am", "edit both")

    (doc,) = list(source.documents())  # triggers pull --ff-only on the cache
    assert doc.relative_path == "docs/a.md"
    assert doc.content == "# Doc A v2\n"
    clone = source._checkout()
    assert not (clone / "policies" / "b.md").exists()  # still out of scope
