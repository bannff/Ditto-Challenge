"""Per-run git worktree — the isolation jail and trust boundary.

Every change happens on a dedicated branch inside a dedicated worktree, never on the
target's checked-out branch. Paths are confined to the worktree root, commands run
without a shell, git hooks/fsmonitor from the target repo are disabled, and child
processes get a scrubbed environment so they can't read our credentials. This is where
untrusted ticket input is contained, in code.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

BRANCH_PREFIX = "autodev/"

_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Disable anything in a target repo that turns a plain git command into code execution.
_GIT_HARDENING = [
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=",
    "-c", "protocol.ext.allow=never",
]

# Acceptance commands are operator/target-declared test runners, not arbitrary programs.
ACCEPTANCE_ALLOWLIST = frozenset(
    {"pytest", "py.test", "python", "python3", "make", "npm", "npx", "yarn", "pnpm",
     "go", "cargo", "tox", "node", "unittest"}
)
_CODE_EVAL_FLAGS = {"-c", "-e", "--command", "--eval"}


class WorktreeError(RuntimeError):
    pass


class WorktreeEscape(WorktreeError):
    """Raised when a path or operation tries to leave the worktree."""


def _safe_env() -> dict[str, str]:
    """Minimal environment for child processes — no AWS/Bedrock creds, no .env values."""
    keep = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TERM", "SystemRoot")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def _git(repo: Path, *args: str, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *_GIT_HARDENING, "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=_safe_env(),
    )


class Worktree:
    def __init__(self, repo: Path, root: Path, branch: str):
        self.repo = repo
        self.root = root.resolve()
        self.branch = branch

    @classmethod
    def create(cls, repo: Path | str, run_id: str, base_dir: Path) -> Worktree:
        if not _RUN_ID_RE.match(run_id):
            raise WorktreeError(f"invalid run_id: {run_id!r}")
        repo = Path(repo).resolve()
        if not (repo / ".git").exists():
            raise WorktreeError(f"{repo} is not a git repository")
        base_dir = base_dir.resolve()
        root = (base_dir / run_id).resolve()
        if root.parent != base_dir:  # defense in depth against a crafted run_id
            raise WorktreeError(f"run_id escapes base dir: {run_id!r}")
        branch = f"{BRANCH_PREFIX}{run_id}"
        base_dir.mkdir(parents=True, exist_ok=True)
        _git(repo, "worktree", "prune")  # clear any stale record so create is idempotent
        r = _git(repo, "worktree", "add", "-b", branch, str(root), "HEAD")
        if r.returncode != 0:
            raise WorktreeError(f"worktree add failed: {r.stderr.strip()}")
        return cls(repo, root, branch)

    def safe_path(self, path: str | Path) -> Path:
        """Resolve a path and guarantee it stays inside the worktree, or raise.
        Callers must use the returned resolved path, not the original, to avoid TOCTOU."""
        candidate = Path(path)
        base = candidate if candidate.is_absolute() else self.root / candidate
        resolved = base.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise WorktreeEscape(f"path escapes worktree: {path}")
        return resolved

    def run(self, args: list[str], *, timeout: int = 300) -> CommandResult:
        if not args:
            raise WorktreeError("empty command")
        try:
            proc = subprocess.run(
                args,
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                shell=False,
                env=_safe_env(),
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, f"timed out after {timeout}s")
        return CommandResult(proc.returncode, (proc.stdout + proc.stderr).strip())

    def run_acceptance(self, command: str, *, timeout: int = 300) -> CommandResult:
        """Run a declared acceptance command. The runner must be allowlisted and the
        string is split (never handed to a shell), so a ticket cannot smuggle in
        arbitrary programs or shell operators."""
        try:
            args = shlex.split(command)
        except ValueError as e:
            raise WorktreeError(f"unparseable command: {e}") from e
        if not args or args[0] not in ACCEPTANCE_ALLOWLIST:
            raise WorktreeError(f"command not allowed: {command!r}")
        is_interpreter = args[0] in {"python", "python3", "node"}
        if is_interpreter and any(a in _CODE_EVAL_FLAGS for a in args[1:]):
            raise WorktreeError("inline code execution is not allowed")
        return self.run(args, timeout=timeout)

    def is_clean(self) -> bool:
        r = _git(self.root, "status", "--porcelain")
        return r.returncode == 0 and r.stdout.strip() == ""

    def diff(self) -> str:
        return _git(self.root, "diff", "HEAD").stdout

    def remove(self) -> None:
        rm = _git(self.repo, "worktree", "remove", "--force", str(self.root))
        _git(self.repo, "branch", "-D", self.branch)
        _git(self.repo, "worktree", "prune")
        if rm.returncode != 0 and self.root.exists():
            raise WorktreeError(f"worktree remove failed: {rm.stderr.strip()}")
