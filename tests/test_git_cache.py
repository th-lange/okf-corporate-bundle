"""GitSource clone cache (issue #50): location, keying, and stale-clone safety.

Three defects fixed: (1) the default cache lived cwd-relative instead of under
the knowledge root, (2) the clone path was keyed by source `name` alone so two
configs sharing a name silently shared (or fought over) one clone, and (3) a
`url` change under a stable `name` kept fast-forwarding the old remote's
clone with no error.
"""

from pathlib import Path

import pytest
from conftest import git

from okf_mcp.ingest.sources import GitSource


@pytest.fixture()
def upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "upstream"
    repo.mkdir()
    (repo / "a.md").write_text("# A\n")
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo


@pytest.fixture()
def other_upstream(tmp_path: Path) -> Path:
    repo = tmp_path / "other-upstream"
    repo.mkdir()
    (repo / "b.md").write_text("# B\n")
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo


def as_remote(repo: Path) -> str:
    return f"file://{repo}"


def test_default_cache_lives_under_the_knowledge_root_not_cwd(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.chdir(tmp_path)  # process cwd is deliberately NOT the root
    source = GitSource(name="handbook", url=as_remote(upstream))
    list(source.documents())
    assert not (tmp_path / ".okf-ingest-cache").exists()  # never the cwd
    clones = list((root / "ingest" / "cache" / "git").glob("handbook-*"))
    assert len(clones) == 1
    assert (clones[0] / ".git").exists()


def test_explicit_cache_dir_still_overrides(upstream: Path, tmp_path: Path) -> None:
    override = tmp_path / "custom-cache"
    source = GitSource(name="handbook", url=as_remote(upstream), cache_dir=override)
    list(source.documents())
    assert list(override.glob("handbook-*"))


def test_same_name_two_urls_never_share_a_clone(
    upstream: Path, other_upstream: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    a = GitSource(name="handbook", url=as_remote(upstream), cache_dir=cache)
    b = GitSource(name="handbook", url=as_remote(other_upstream), cache_dir=cache)
    docs_a = {d.relative_path for d in a.documents()}
    docs_b = {d.relative_path for d in b.documents()}
    assert docs_a == {"a.md"}
    assert docs_b == {"b.md"}  # b's content never leaked into a's clone or vice versa
    clone_a, clone_b = a._checkout(), b._checkout()
    assert clone_a != clone_b


def test_url_change_under_stable_name_gets_a_fresh_clone_not_a_stale_pull(
    upstream: Path, other_upstream: Path, tmp_path: Path
) -> None:
    cache = tmp_path / "cache"
    original = GitSource(name="handbook", url=as_remote(upstream), cache_dir=cache)
    list(original.documents())

    repointed = GitSource(name="handbook", url=as_remote(other_upstream), cache_dir=cache)
    docs = {d.relative_path for d in repointed.documents()}
    assert docs == {"b.md"}  # fresh clone of the new remote, not a's stale content
    assert original._checkout() != repointed._checkout()


def test_origin_mismatch_on_a_reused_path_forces_reclone(
    upstream: Path, other_upstream: Path, tmp_path: Path
) -> None:
    """Belt-and-braces: a clone created before URL-hashed keys existed (or
    hand-placed) sits at the exact path a new config's key resolves to, but
    with a different origin. The mismatch must trigger a re-clone, never a
    pull of the wrong remote."""
    cache = tmp_path / "cache"
    source = GitSource(name="handbook", url=as_remote(upstream), cache_dir=cache)
    clone_path = source._checkout()

    # Simulate a stale clone at the same path but for a different origin.
    git(clone_path, "remote", "set-url", "origin", as_remote(other_upstream))

    docs = {d.relative_path for d in source.documents()}
    assert docs == {"a.md"}  # re-cloned from the configured url, not the stale origin
    assert git(clone_path, "remote", "get-url", "origin").strip() == as_remote(upstream)


def test_cross_test_stale_clone_leakage_scenario(
    upstream: Path, other_upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the cross-file test flakiness surfaced while
    stress-testing #47: two GitSource instances sharing a source `name`
    (as two independent test modules or configs might) under the shared,
    root-derived default cache must never leak a clone between them."""
    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))

    first = GitSource(name="handbook", url=as_remote(upstream))
    second = GitSource(name="handbook", url=as_remote(other_upstream))
    first_docs = {d.relative_path for d in first.documents()}
    second_docs = {d.relative_path for d in second.documents()}
    assert first_docs == {"a.md"}
    assert second_docs == {"b.md"}


def test_generational_snapshot_never_stages_the_git_cache(
    upstream: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generational layout (#47) snapshots `bundles/` and the ledger
    only; the git clone cache under `<root>/ingest/cache/` must stay outside
    every staged `generations/<id>/` directory."""
    from okf_mcp.ingest.generations import stage_generation

    root = tmp_path / "kb"
    root.mkdir()
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    source = GitSource(name="handbook", url=as_remote(upstream))
    list(source.documents())

    staged = stage_generation(root)
    assert not (staged / "ingest" / "cache").exists()
