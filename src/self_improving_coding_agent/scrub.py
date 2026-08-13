"""Regex redaction applied at every persistence/log boundary.

Agents work with real data; only what gets persisted or logged passes through here.
Patterns are data — add a rule by adding a tuple, not by editing control flow.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED_CC]"),
    (
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b(\s*[=:]\s*)"
            r"['\"]?[A-Za-z0-9/+_.\-]{8,}['\"]?"
        ),
        r"\1\2[REDACTED_SECRET]",
    ),
    # Dates of birth: keep only the year.
    (re.compile(r"\b(\d{4})-\d{2}-\d{2}\b"), r"\1"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/(\d{4})\b"), r"\1"),
]


def scrub_text(text: str) -> str:
    for pattern, replacement in _RULES:
        text = pattern.sub(replacement, text)
    return text
