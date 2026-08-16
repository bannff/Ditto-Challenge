"""Deterministic refusal gate. Refusal is a correct outcome, enforced in code before any
worktree is touched — not left to a model. This reference denylist is data; swap the
patterns for a different use case.
"""

from __future__ import annotations

import re
import shlex

from .acceptance_policy import AcceptanceRejected, normalize, validate
from .contracts import Ticket

_MIN_REQUEST_CHARS = 15

_UNSAFE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(
        r"\b(exfiltrat|leak|steal|read|dump|print|email|send|extract|harvest)\w*\b.{0,60}"
        r"\b(secret|credential|api[ _-]?key|access[ _-]?key|token|\.env|environment)\b",
        re.I,
    ), "asks to read or exfiltrate secrets"),
    (re.compile(r"\breverse shell\b|\bback\s?door\b|\bexfiltrat", re.I),
     "asks to install a backdoor or exfiltration channel"),
    (re.compile(
        r"\b(disabl|remov|delete|drop|bypass|skip|turn off)\w*\b.{0,40}"
        r"\b(test|check|guard|safety|validation)",
        re.I,
    ), "asks to remove or disable the project's own checks"),
    (re.compile(
        r"\brm\s+-rf\b|\b(delete|wipe|drop|destroy)\b.{0,30}"
        r"\b(everything|all files|repo|repository|database|suite)\b",
        re.I,
    ), "asks for a destructive operation"),
    (re.compile(r"\b(force[- ]?push|push|commit)\b.{0,30}\b(main|master|production|prod)\b", re.I),
     "asks to act on a protected branch"),
    (re.compile(r"\.\./|/etc/|~/\.ssh|\bcurl\b|\bwget\b", re.I),
     "references resources outside the target repo"),
]


def _unrunnable_gate(command: str) -> str | None:
    """Why the declared gate could never run, if so.

    Checked here, before a worktree or a single token is spent, so an unrunnable gate is a
    stated refusal rather than a late crash. The jail-relative path check can only happen
    once a worktree exists, so it stays in run_acceptance.
    """
    try:
        args = shlex.split(command)
    except ValueError as e:
        return f"acceptance command cannot be parsed: {e}"
    try:
        validate(normalize(args))
    except AcceptanceRejected as e:
        return f"acceptance command is not allowed: {e}"
    return None


def should_refuse(ticket: Ticket) -> str | None:
    text = (ticket.request or "").strip()
    if len(text) < _MIN_REQUEST_CHARS:
        return "underspecified: the request is too short to act on safely"
    if ticket.acceptance_command:
        reason = _unrunnable_gate(ticket.acceptance_command)
        if reason:
            return f"unsafe: {reason}"
    # acceptance_command is untrusted ticket input too — scan it with the same denylist so
    # a hostile runner can't slip past the request gate. The allowlist in run_acceptance is
    # the primary control; this is defense-in-depth for smuggled curl/wget/path-traversal.
    targets = [text, ticket.acceptance_command] if ticket.acceptance_command else [text]
    for pattern, reason in _UNSAFE_PATTERNS:
        if any(pattern.search(t) for t in targets):
            return f"unsafe: {reason}"
    return None
