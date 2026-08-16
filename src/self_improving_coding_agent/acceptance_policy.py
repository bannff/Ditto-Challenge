"""What an acceptance command is allowed to be.

`acceptance_command` is untrusted ticket text that our own code executes, so the policy is
data: one row per runner, and a pure `validate()` the worktree enforces. Adding a runner
means adding a row, not editing control flow.

Three deliberate choices:

*Small surface.* pytest is the only runner. `make` is a shell interpreter (`-C`, `-f`),
`tox`/`go`/`cargo` fetch from the network, `node` and the `npx` family run remote code, and
a bare interpreter is worse still: `python3 gate.py` would let a ticket nominate any script
as the gate, and `python3 -m unittest <dotted.name>` names a *module* resolved through
`sys.path`, which a path-based jail cannot bound by construction. `python -m pytest` is
folded onto the pytest row by `normalize()`, so the common spelling still works.

*Fail closed.* Anything the policy doesn't name is refused — an escape nobody thought of is
denied by omission rather than allowed by it. That includes the `allows_paths` default: a
fail-open default inside a fail-closed table is how a runner ends up quietly accepting a
script.

*Total.* Every token must be printable ASCII, so nothing reaches the path check that could
raise something other than `AcceptanceRejected` (an embedded NUL used to crash
`Path.resolve`), and no Unicode look-alike dash can slip past the flag branch. A positional
must also *look* like a test path, so "not a flag" is no longer enough to be treated as one —
and tokens that change the parser's own mode (`@argfile`, `--`) are refused rather than
interpreted, because past that point our reading of the argv and pytest's diverge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


class AcceptanceRejected(ValueError):
    """The declared acceptance command is not allowed to run."""


# pytest's parser sets fromfile_prefix_chars="@", so argparse replaces any token whose first
# character is "@" with the lines of that file — before parsing, at any position, and
# recursively. That is an argv splice: it defeats every flag decision here and can append a
# later `-o addopts=` that overrides ours. No token may start with it.
_ARGFILE_PREFIX = "@"

# What a pytest positional may look like: a path, optionally with ::node::ids. Traversal is
# still the jail check's job — `..` matches this shape and _check_path refuses it.
_PYTEST_POSITIONAL = re.compile(
    r"""^
    /?                                        # absolute is allowed; _check_path bounds it
    [A-Za-z0-9_.][A-Za-z0-9_.\-]*             # first segment
    (?:/[A-Za-z0-9_.][A-Za-z0-9_.\-]*)*       # further segments
    /?                                        # trailing slash on a directory
    (?:::[A-Za-z0-9_][A-Za-z0-9_.\-\[\] ]*)*  # ::TestClass::test_method[case-1]
    $""",
    re.VERBOSE,
)


@dataclass(frozen=True)
class RunnerPolicy:
    """The complete set of things one runner may be handed."""

    # Flags accepted with no value of their own.
    flags: frozenset[str] = frozenset()
    # Flags that consume the following token (or use --flag=value).
    valued_flags: frozenset[str] = frozenset()
    # What a positional may look like. None = this runner takes no positionals, so a row opts
    # in by saying what a path *is* rather than by flipping a bool.
    positional_pattern: re.Pattern[str] | None = None
    # Arguments we always prepend ourselves. Never offered to the ticket.
    forced_args: tuple[str, ...] = ()
    # Forced args templated on the worktree root; rendered with {jail} by resolve().
    jail_args: tuple[str, ...] = ()
    # Forced args templated on the run's private HOME; rendered with {home} by resolve().
    home_args: tuple[str, ...] = ()
    # How this runner is told which config file to use, if it supports being pinned.
    config_flag: str | None = None


_PYTEST = RunnerPolicy(
    # --co/--collect-only is deliberately absent: collection exits 0 on a fully red suite,
    # and a gate that cannot fail is not a gate.
    flags=frozenset({"-q", "--quiet", "-v", "-vv", "--verbose", "-x", "--exitfirst",
                     "-s", "--no-header", "-ra"}),
    valued_flags=frozenset({"-k", "-m", "--maxfail", "--tb"}),
    positional_pattern=_PYTEST_POSITIONAL,
    # We pin the ini keys that decide what runs, and what state is left behind. pytest applies
    # `-o` last-wins and prepends ini/env options *before* the command line, so ours win — and
    # with `@`, `-o`, `-p` and `--` all refused above, nothing can append a later override.
    #   addopts:         a repo ini could otherwise inject --co and silence a red suite.
    #   testpaths:       a repo ini could otherwise choose the test set for a bare `pytest`
    #                    and quietly hide the failing test.
    #   cache_dir:       keeps .pytest_cache out of the worktree, so the diff and is_clean()
    #                    still mean what they say. Pointed at the run's own HOME rather than
    #                    disabling cacheprovider: unregistering the plugin makes `cache_dir`
    #                    an unknown key, which a target that sets it *and* treats warnings as
    #                    errors turns into an INTERNALERROR — a legitimate repo made
    #                    unrunnable by our own hardening.
    #   log_file:        an out-of-jail *write* primitive, not just a stray file. pytest
    #                    os.makedirs() the parent and opens the path itself, and with
    #                    log_file_mode=a plus log_file_format=%(message)s the content is the
    #                    test's own logging calls — i.e. append arbitrary text to ~/.zshenv.
    #                    Nothing the gate needs; we capture the child's output ourselves.
    forced_args=(
        "-o", "addopts=",
        "-o", "testpaths=",
        "-o", "log_file=",
    ),
    # Rendered against the run's private HOME, which is outside the jail.
    home_args=("-o", "cache_dir={home}/pytest_cache"),
    # confcutdir bounds conftest.py *execution* to the worktree: a config file in any ancestor
    # directory otherwise moves rootdir above the jail and pytest imports that directory's
    # conftest.py. rootdir keeps ids and the report anchored to the worktree.
    jail_args=("--rootdir={jail}", "--confcutdir={jail}"),
    config_flag="-c",
)

# Where pytest looks for configuration, in its own precedence order, and what marks a file as
# actually carrying pytest settings (an empty pyproject.toml must not shadow a real tox.ini).
PYTEST_CONFIG_FILES: tuple[tuple[str, str | None], ...] = (
    ("pytest.ini", None),
    ("pyproject.toml", "[tool.pytest.ini_options]"),
    ("tox.ini", "[pytest]"),
    ("setup.cfg", "[tool:pytest]"),
)

POLICIES: dict[str, RunnerPolicy] = {
    "pytest": _PYTEST,
    "py.test": _PYTEST,
}


def normalize(args: list[str]) -> list[str]:
    """Fold `python -m pytest ...` into `pytest ...`.

    Same execution, so one policy should govern both — otherwise a module launch would be
    validated under an interpreter's rules and pytest's own escape flags (`--rootdir`, `-p`)
    would go unchecked.
    """
    if len(args) >= 3 and args[0] in {"python", "python3"} and args[1] == "-m" \
            and args[2] == "pytest":
        return ["pytest", *args[3:]]
    return args


def resolve(
    args: list[str],
    *,
    in_jail: Path | None = None,
    config: Path | None = None,
    home: Path | None = None,
) -> list[str]:
    """Validate `args` and return the argv to actually execute.

    The ticket's own tokens are validated first; ours are added afterwards so a ticket can
    never reach a forced flag by supplying it itself. Positionals are rewritten to the
    canonical path we validated — pytest does not resolve symlinks, so executing the token
    as written would let `/tmp/...` (a symlink on macOS, or a symlinked worktree base) sit
    outside a resolved `--confcutdir` and pull in an ancestor's conftest.
    """
    tail = validate(args, in_jail=in_jail)
    policy = POLICIES[args[0]]
    pinned: tuple[str, ...] = ()
    if in_jail is not None:
        jail = str(in_jail.resolve())
        pinned = tuple(a.format(jail=jail) for a in policy.jail_args)
    if home is not None:
        pinned = (*pinned, *(a.format(home=str(home)) for a in policy.home_args))
    # A config file we choose: `--rootdir` pins only rootdir, so pytest would still walk up
    # and adopt an ancestor's ini (whose `pythonpath` alone can poison sys.path).
    if config is not None and policy.config_flag:
        pinned = (policy.config_flag, str(config), *pinned)
    return [args[0], *policy.forced_args, *pinned, *tail]


def validate(args: list[str], *, in_jail: Path | None = None) -> list[str]:
    """Raise AcceptanceRejected unless every token is permitted; return the checked tail.

    `in_jail` is the worktree root; when given, every positional path must resolve inside it,
    so a command cannot collect and execute code from outside the worktree. The returned tail
    carries positionals in their canonical (jail-relative) form.
    """
    if not args:
        raise AcceptanceRejected("empty command")
    for token in args:
        if not (token.isascii() and token.isprintable()):
            raise AcceptanceRejected(f"token is not printable ASCII: {token!r}")
        # Checked over every token, not just positionals: argparse expands argfiles across the
        # whole argv before parsing, so `-k @file` splices just as `@file` does. The attached
        # `--flag=@file` spelling is not expanded today (only a token whose first character is
        # `@`), and pytest usage-errors it — but it still names a file we never want near the
        # gate, so refuse it here rather than depend on the runner to fail closed.
        if token.startswith(_ARGFILE_PREFIX) or token.partition("=")[2].startswith(
            _ARGFILE_PREFIX
        ):
            raise AcceptanceRejected(f"argument-file token is not allowed: {token!r}")

    runner, tail = args[0], args[1:]
    policy = POLICIES.get(runner)
    if policy is None:
        raise AcceptanceRejected(f"runner not allowed: {runner!r}")

    checked: list[str] = []
    awaiting: str | None = None  # the flag whose value the next token supplies
    for token in tail:
        if awaiting is not None:  # consumed by the preceding valued flag
            awaiting = None
            checked.append(token)
            continue

        if token.startswith("-"):
            name, sep, _ = token.partition("=")
            if name in policy.flags and not sep:
                checked.append(token)
                continue
            if name in policy.valued_flags:
                awaiting = None if sep else name
                checked.append(token)
                continue
            raise AcceptanceRejected(f"flag not allowed for {runner}: {token!r}")

        if policy.positional_pattern is None:
            raise AcceptanceRejected(f"argument not allowed for {runner}: {token!r}")
        if not policy.positional_pattern.fullmatch(token):
            raise AcceptanceRejected(f"not a test path for {runner}: {token!r}")
        checked.append(_canonical_path(token, in_jail))

    if awaiting is not None:
        raise AcceptanceRejected(f"flag is missing its value: {awaiting!r}")
    return checked


def _canonical_path(token: str, in_jail: Path | None) -> str:
    """Confine a positional to the worktree and return the form we will execute.

    A `::node::id` suffix is split off first so only the path part is resolved, then
    re-attached. The result is relative to the worktree (children run with cwd there), so what
    pytest sees is the same path we checked rather than a spelling that resolves elsewhere.
    """
    if in_jail is None:
        return token
    path_part, sep, node_id = token.partition("::")
    root = in_jail.resolve()
    candidate = Path(path_part)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise AcceptanceRejected(f"path escapes the worktree: {token!r}")
    relative = "." if resolved == root else str(resolved.relative_to(root))
    return f"{relative}{sep}{node_id}" if sep else relative
