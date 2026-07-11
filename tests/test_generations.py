"""Generational atomic publish + pinned/hot-reload serving (issue #47)."""

from pathlib import Path

import pytest
from conftest import git

from okf_mcp.ingest.cli import main as ingest_main
from okf_mcp.ingest.generations import GenerationValidationError
from okf_mcp.server import build_server


def _write_bundle_skeleton(root: Path) -> Path:
    kb = root / "bundles" / "kb"
    kb.mkdir(parents=True)
    (kb / "index.md").write_text("---\ntype: Index\nscope_default: [public]\n---\n# KB\n")
    return kb


def _write_config(root: Path, source_repo: Path, *, generations: bool, extra: str = "") -> None:
    lines = "generations: true\n" if generations else ""
    lines += extra
    lines += (
        "sources:\n"
        f"  - name: gen-handbook\n    type: git\n    url: {source_repo}\n    target: kb\n"
    )
    (root / "ingest.yaml").write_text(lines)


def current_generation(root: Path) -> str:
    return (root / "generations" / "CURRENT").read_text(encoding="utf-8").strip()


def add_note(repo: Path, name: str, title: str) -> None:
    (repo / f"{name}.md").write_text(
        f"---\ntype: Note\ntitle: {title}\ndescription: d\n---\n\nBody of {title}.\n"
    )
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", f"add {name}")


@pytest.fixture()
def gen_ws(source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A git-backed knowledge root with `generations: true`."""
    root = tmp_path / "knowledge"
    kb = _write_bundle_skeleton(root)
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "init knowledge repo")
    _write_config(root, source_repo, generations=True)
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)
    monkeypatch.delenv("OKF_HOT_RELOAD", raising=False)
    return {"root": root, "kb": kb, "repo": source_repo}


# (a) first sync stages a generation and flips CURRENT ------------------


def test_first_sync_publishes_generation_and_flips_current(gen_ws: dict) -> None:
    assert ingest_main(["sync"]) == 0
    root = gen_ws["root"]
    generation_id = current_generation(root)
    gen_dir = root / "generations" / generation_id
    assert gen_dir.is_dir()
    assert (gen_dir / "bundles" / "kb" / "plain.md").is_file()
    assert (gen_dir / "ingest" / "ledger.yaml").is_file()


# (b) second sync advances CURRENT and retains the old generation -------


def test_second_sync_advances_generation_and_retains_old(gen_ws: dict) -> None:
    assert ingest_main(["sync"]) == 0
    root = gen_ws["root"]
    first_id = current_generation(root)

    add_note(gen_ws["repo"], "extra", "Extra")
    assert ingest_main(["sync"]) == 0
    second_id = current_generation(root)

    assert second_id != first_id
    assert (root / "generations" / first_id).is_dir()
    assert (root / "generations" / second_id / "bundles" / "kb" / "extra.md").is_file()
    # the old generation's content is untouched by the new one
    assert (root / "generations" / first_id / "bundles" / "kb" / "plain.md").is_file()
    assert not (root / "generations" / first_id / "bundles" / "kb" / "extra.md").exists()


# (c) a failed/aborted run never flips CURRENT ---------------------------


def test_source_raising_mid_apply_leaves_current_unchanged(
    gen_ws: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert ingest_main(["sync"]) == 0
    root = gen_ws["root"]
    first_id = current_generation(root)

    import okf_mcp.ingest.cli as cli_mod

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(cli_mod, "_apply_source", boom)
    add_note(gen_ws["repo"], "extra2", "Extra2")

    with pytest.raises(RuntimeError, match="kaboom"):
        ingest_main(["sync"])

    assert current_generation(root) == first_id
    remaining = {p.name for p in (root / "generations").iterdir() if p.is_dir()}
    assert remaining == {first_id}  # the orphaned staged generation was discarded


def test_validation_failure_leaves_current_unchanged(
    gen_ws: dict, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    assert ingest_main(["sync"]) == 0
    root = gen_ws["root"]
    first_id = current_generation(root)

    import okf_mcp.ingest.generations as generations_mod

    def reject(staged: Path) -> None:
        raise GenerationValidationError("staged tree is broken")

    monkeypatch.setattr(generations_mod, "validate_generation", reject)

    assert ingest_main(["sync"]) == 2
    assert "generation rejected" in capsys.readouterr().err
    assert current_generation(root) == first_id
    remaining = {p.name for p in (root / "generations").iterdir() if p.is_dir()}
    assert remaining == {first_id}


# (d) non-git knowledge root round-trip ----------------------------------


def test_generations_publish_and_serve_without_git(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "knowledge"
    _write_bundle_skeleton(root)
    _write_config(root, source_repo, generations=True)  # deliberately no `git init`
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)

    assert ingest_main(["sync"]) == 0
    assert not (root / ".git").exists()
    generation_id = current_generation(root)
    assert (root / "generations" / generation_id / "bundles" / "kb" / "plain.md").is_file()


@pytest.mark.anyio
async def test_serving_reads_pinned_generation_without_git(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "knowledge"
    _write_bundle_skeleton(root)
    _write_config(root, source_repo, generations=True)
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)

    assert ingest_main(["sync"]) == 0
    server = build_server(scopes=[])
    result = await server.call_tool("get_concept", {"concept_id": "/plain"})
    assert "Just prose" in result[0].text


# (e) pinning at startup + hot-reload seam -------------------------------


@pytest.mark.anyio
async def test_server_pins_at_startup_then_hot_reloads(gen_ws: dict) -> None:
    assert ingest_main(["sync"]) == 0
    server = build_server(scopes=[])
    result = await server.call_tool("get_concept", {"concept_id": "/plain"})
    assert "Just prose" in result[0].text

    # publish a second generation "behind" the already-built server
    add_note(gen_ws["repo"], "extra3", "Extra3")
    assert ingest_main(["sync"]) == 0

    # still pinned to the generation resolved at build time
    with pytest.raises(Exception, match="Unknown concept id"):
        await server.call_tool("get_concept", {"concept_id": "/extra3"})

    assert server.maybe_reload() is True
    result = await server.call_tool("get_concept", {"concept_id": "/extra3"})
    assert "Body of Extra3" in result[0].text

    # nothing changed — a second check is a no-op
    assert server.maybe_reload() is False


# (f) retention prunes beyond `generations_keep` -------------------------


def test_retention_prunes_beyond_keep(gen_ws: dict) -> None:
    _write_config(gen_ws["root"], gen_ws["repo"], generations=True, extra="generations_keep: 2\n")
    ids = []
    for i in range(4):
        add_note(gen_ws["repo"], f"note{i}", f"N{i}")
        assert ingest_main(["sync"]) == 0
        ids.append(current_generation(gen_ws["root"]))

    remaining = {p.name for p in (gen_ws["root"] / "generations").iterdir() if p.is_dir()}
    assert remaining == set(ids[-2:])
    assert current_generation(gen_ws["root"]) == ids[-1]


# (g) legacy root without generations is untouched -----------------------


def test_legacy_root_without_generations_keeps_writing_in_place(
    source_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "knowledge"
    kb = _write_bundle_skeleton(root)
    git(root, "init", "--quiet")
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", "init knowledge repo")
    _write_config(root, source_repo, generations=False)
    monkeypatch.setenv("OKF_KNOWLEDGE_ROOT", str(root))
    monkeypatch.delenv("OKF_BUNDLE_DIRS", raising=False)
    monkeypatch.delenv("OKF_BUNDLE_DIR", raising=False)

    assert ingest_main(["sync"]) == 0
    assert not (root / "generations").exists()
    assert (kb / "plain.md").is_file()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
