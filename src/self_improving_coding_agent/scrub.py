"""Regex redaction applied at every persistence/log boundary.

Agents work with real data; only what gets persisted or logged passes through here.
Patterns are data — add a rule by adding a tuple, not by editing control flow.
"""

from __future__ import annotations

import re

_RULES: list[tuple[re.Pattern[str], str]] = [
    # High-entropy secrets first, by shape — these need no surrounding keyword to catch.
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?"
            r"-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED_PRIVATE_KEY]",
    ),
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
        "[REDACTED_JWT]",
    ),
    (re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"), "Bearer [REDACTED_TOKEN]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[REDACTED_AWS_KEY]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,16}\b"), "[REDACTED_CC]"),
    # key=value / key: value — the key name may carry a prefix/suffix
    # (aws_secret_access_key, x-api-key, github_token), so match the sensitive word
    # anywhere in the key token, not just as a standalone word.
    (
        re.compile(
            r"(?i)\b([\w.\-]*(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key)"
            r"[\w.\-]*)(\s*[=:]\s*)['\"]?[A-Za-z0-9/+_.\-]{6,}['\"]?"
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
