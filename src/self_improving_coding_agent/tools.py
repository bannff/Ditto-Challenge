"""Run-scoped tools — the agent's hands, jailed to a worktree.

The agent acts only through these. Each file tool resolves its path through
Worktree.safe_path, so a path escaping the worktree is refused here, at the tool, not
left to the model's goodwill. Escapes return an error string so the agent gets feedback
instead of crashing the run.
"""

from __future__ import annotations

from strands import tool

from .worktree import Worktree, WorktreeError


def make_worktree_tools(wt: Worktree) -> list:
    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the worktree."""
        try:
            resolved = wt.safe_path(path)
        except WorktreeError as e:
            return f"refused: {e}"
        if not resolved.is_file():
            return f"no such file: {path}"
        return resolved.read_text()

    @tool
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a text file inside the worktree."""
        try:
            resolved = wt.safe_path(path)
        except WorktreeError as e:
            return f"refused: {e}"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content)
        return f"wrote {path} ({len(content)} bytes)"

    @tool
    def list_files(path: str = ".") -> str:
        """List files and directories inside the worktree at the given relative path."""
        try:
            resolved = wt.safe_path(path)
        except WorktreeError as e:
            return f"refused: {e}"
        if not resolved.exists():
            return f"no such path: {path}"
        if resolved.is_file():
            return path
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    return [read_file, write_file, list_files]
