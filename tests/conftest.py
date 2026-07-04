"""Shared fixtures: a small git repo acting as an ingest source."""

import subprocess
from pathlib import Path

import pytest

NOTE = """---
type: Note
title: MRR tips
---

# MRR tips

Watch the grain.
"""

PLAIN = "# Just prose\n\nNo frontmatter at all.\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t.test", "-c", "user.name=t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture()
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "handbook"
    (repo / "notes").mkdir(parents=True)
    (repo / "notes" / "mrr-tips.md").write_text(NOTE)
    (repo / "plain.md").write_text(PLAIN)
    git(repo, "init", "--quiet")
    git(repo, "add", ".")
    git(repo, "commit", "--quiet", "-m", "seed")
    return repo
