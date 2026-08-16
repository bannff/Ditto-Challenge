"""Run-scoped tools — the agent's hands, jailed to a worktree.

The agent acts only through these. Each file tool resolves its path through
Worktree.safe_path, so a path escaping the worktree is refused here, at the tool, not
left to the model's goodwill. Escapes return an error string so the agent gets feedback
instead of crashing the run.
"""

from __future__ import annotations

import functools
import os
import stat
from collections.abc import Callable
from pathlib import Path

from strands import tool

from .worktree import MAX_READ_BYTES, MAX_WRITE_BYTES, Worktree, WorktreeError

# What `write_text` used to produce. Source files, not programs.
_NEW_FILE_MODE = 0o644


def _refuses(fn: Callable[..., str]) -> Callable[..., str]:
    """Turn a filesystem failure into feedback instead of a crashed run.

    The docstring above promised this and only `WorktreeError` delivered it, so an ordinary
    mistake took the whole run down: `write_file("service.py/evil.py")` raised `FileExistsError`
    from `parent.mkdir`, and a NUL in a path raised `ValueError` out of `resolve()`. Others in
    the same class: writing onto a directory, over a read-only file, a name past NAME_MAX,
    reading a binary file (`UnicodeDecodeError`), listing an unreadable directory.

    One wrapper rather than a `try` per call, because the answer is identical for all of them
    and near-duplicate handlers drift. The caught set is an explicit tuple and deliberately
    *not* `Exception`: swallowing everything would convert our own bugs into a plausible
    refusal string the model then retries against, and would hide them from the ledger. The
    exception type is included for the same reason — a genuine defect should read like one.
    """

    @functools.wraps(fn)
    def guarded(*args, **kwargs) -> str:
        try:
            return fn(*args, **kwargs)
        except (WorktreeError, OSError, UnicodeDecodeError, ValueError) as e:
            return f"refused: {type(e).__name__}: {e}"

    return guarded


def _open_regular_for_write(path: Path) -> int:
    """A write fd, or raise, without ever blocking or following a link.

    `write_text` on a FIFO with no reader blocks forever — before raising, so no handler catches
    it, and `write_file` has no timeout. That was the only unbounded operation in this set.
    `O_NONBLOCK` turns it into an immediate ENXIO; `O_NOFOLLOW` closes the same class for a
    symlink planted between `safe_path` and the write. The `S_ISREG` check is what makes the
    guarantee positive rather than a list of things we remembered to exclude.

    Two details that are easy to get wrong and both land in the *target's* history:

    - the mode is explicit. `os.open` with `O_CREAT` and no mode defaults to 0o777-minus-umask,
      so every file the agent wrote would be committed `100755` — source files marked executable
      in a diff we ship to a repo we don't own. `write_text` gave 0o644, and so must this.
    - truncation happens *after* the type check, not via `O_TRUNC`, so nothing is destroyed
      before we know what we opened.
    """
    fd = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, _NEW_FILE_MODE
    )
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorktreeError(f"not a regular file: {path}")
        os.ftruncate(fd, 0)
    except BaseException:
        os.close(fd)
        raise
    return fd


def make_worktree_tools(wt: Worktree) -> list:
    @tool
    @_refuses
    def read_file(path: str) -> str:
        """Read a UTF-8 text file inside the worktree."""
        resolved = wt.safe_path(path)
        if not resolved.is_file():
            return f"no such file: {path}"
        # Bounded for the same reason the gate's output is: a large file the gate dropped in the
        # jail would otherwise go straight into the model's context.
        data = resolved.read_bytes()[: MAX_READ_BYTES + 1]
        if len(data) > MAX_READ_BYTES:
            head = data[:MAX_READ_BYTES].decode("utf-8", "replace")
            return f"{head}\n[... truncated at {MAX_READ_BYTES} bytes ...]"
        return data.decode("utf-8")

    @tool
    @_refuses
    def write_file(path: str, content: str) -> str:
        """Create or overwrite a text file inside the worktree."""
        resolved = wt.safe_path(path)
        # Bounded before the write, not after: what lands here is committed into the *target's*
        # object store and stays there, so an oversized write is a durable cost to a repo we
        # don't own. Refused with the limit in the message so the agent can split the file
        # rather than retrying the same call — a truncated write would be worse than none,
        # since the gate would then verify a file the agent didn't intend.
        encoded = content.encode("utf-8", errors="replace")
        if len(encoded) > MAX_WRITE_BYTES:
            return (
                f"refused: {len(encoded)} bytes exceeds the "
                f"{MAX_WRITE_BYTES}-byte limit for one file"
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        fd = _open_regular_for_write(resolved)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
        return f"wrote {path} ({len(content)} bytes)"

    @tool
    @_refuses
    def list_files(path: str = ".") -> str:
        """List files and directories inside the worktree at the given relative path."""
        resolved = wt.safe_path(path)
        if not resolved.exists():
            return f"no such path: {path}"
        if resolved.is_file():
            return path
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir())
        return "\n".join(entries) if entries else "(empty)"

    return [read_file, write_file, list_files]
