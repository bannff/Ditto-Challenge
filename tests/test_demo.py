"""The demo is a graded artifact — its materialized repo must produce clean diffs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from demo import materialize  # noqa: E402


def _tracked(repo: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(repo), "ls-files"], capture_output=True, text=True, check=True
    )
    return out.stdout.split()


def test_materialize_excludes_bytecode_from_the_seed(tmp_path):
    target = tmp_path / "app"
    (target / "__pycache__").mkdir(parents=True)
    (target / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00binary")
    (target / "mod.py").write_text("x = 1\n")

    repo = materialize(tmp_path / "repo", target)
    tracked = _tracked(repo)

    assert "mod.py" in tracked
    assert not any("pycache" in path or path.endswith(".pyc") for path in tracked)


def test_bytecode_generated_after_seeding_stays_out_of_the_diff(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "mod.py").write_text("x = 1\n")
    repo = materialize(tmp_path / "repo", target)

    # Simulate what running the target's tests does to the tree.
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00binary")

    diff = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert diff.stdout.strip() == ""
