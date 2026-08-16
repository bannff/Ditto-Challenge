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
`Path.resolve`), and no Unicode look-alike dash can slip past the flag branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class AcceptanceRejected(ValueError):
    """The declared acceptance command is not allowed to run."""


@dataclass(frozen=True)
class RunnerPolicy:
    """The complete set of things one runner may be handed."""

    # Flags accepted with no value of their own.
    flags: frozenset[str] = frozenset()
    # Flags that consume the following token (or use --flag=value).
    valued_flags: frozenset[str] = frozenset()
    # May positional arguments (test paths) be supplied? Default off: a row has to opt in.
    allows_paths: bool = False
    # Arguments we always prepend ourselves. Never offered to the ticket.
    forced_args: tuple[str, ...] = ()


_PYTEST = RunnerPolicy(
    # --co/--collect-only is deliberately absent: collection exits 0 on a fully red suite,
    # and a gate that cannot fail is not a gate.
    flags=frozenset({"-q", "--quiet", "-v", "-vv", "--verbose", "-x", "--exitfirst",
                     "-s", "--no-header", "-ra"}),
    valued_flags=frozenset({"-k", "-m", "--maxfail", "--tb"}),
    allows_paths=True,
    # Neutralise ini `addopts`: a repo-resident pytest.ini could otherwise inject --co and
    # silence a red suite. `-o` is not in the ticket-facing flag sets, so only we can set it.
    forced_args=("-o", "addopts="),
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


def resolve(args: list[str], *, in_jail: Path | None = None) -> list[str]:
    """Validate `args` and return the argv to actually execute (with any forced args).

    Validation runs against the ticket's own tokens first; ours are prepended afterwards so
    a ticket can never reach a forced flag by supplying it itself.
    """
    validate(args, in_jail=in_jail)
    policy = POLICIES[args[0]]
    return [args[0], *policy.forced_args, *args[1:]]


def validate(args: list[str], *, in_jail: Path | None = None) -> None:
    """Raise AcceptanceRejected unless every token in `args` is permitted.

    `in_jail` is the worktree root; when given, every positional path must resolve inside
    it, so a command cannot collect and execute code from outside the worktree.
    """
    if not args:
        raise AcceptanceRejected("empty command")
    for token in args:
        if not (token.isascii() and token.isprintable()):
            raise AcceptanceRejected(f"token is not printable ASCII: {token!r}")

    runner, tail = args[0], args[1:]
    policy = POLICIES.get(runner)
    if policy is None:
        raise AcceptanceRejected(f"runner not allowed: {runner!r}")

    awaiting: str | None = None  # the flag whose value the next token supplies
    for token in tail:
        if awaiting is not None:  # consumed by the preceding valued flag
            awaiting = None
            continue

        if token.startswith("-"):
            name, sep, _ = token.partition("=")
            if name in policy.flags and not sep:
                continue
            if name in policy.valued_flags:
                awaiting = None if sep else name
                continue
            raise AcceptanceRejected(f"flag not allowed for {runner}: {token!r}")

        if not policy.allows_paths:
            raise AcceptanceRejected(f"argument not allowed for {runner}: {token!r}")
        _check_path(token, in_jail)

    if awaiting is not None:
        raise AcceptanceRejected(f"flag is missing its value: {awaiting!r}")


def _check_path(token: str, in_jail: Path | None) -> None:
    if in_jail is None:
        return
    # Resolve both sides: a caller may hand us an unresolved root, and on macOS /tmp is a
    # symlink, so comparing a resolved child against an unresolved root rejects everything.
    root = in_jail.resolve()
    candidate = Path(token)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise AcceptanceRejected(f"path escapes the worktree: {token!r}")
