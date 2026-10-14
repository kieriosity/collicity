"""Log redaction (SEC-03).

A backstop, not the control: secrets are kept out of logs by never logging them (access tokens
live only in the gateway's memory; settings hold secrets as ``SecretStr``). This formatter scrubs
anything that slips through, including exception tracebacks.
"""

import logging
import re
from typing import override

REDACTED = "[REDACTED]"

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Credentials embedded in URLs: scheme://user:password@host
    (re.compile(r"(://[^:/@\s]+:)[^@\s]+(@)"), rf"\1{REDACTED}\2"),
    # Authorization headers
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"), rf"\1 {REDACTED}"),
    # JSON Web Tokens, including OAuth access and ID tokens
    (re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"), REDACTED),
    # key=value and "key": "value" forms of well-known secret names
    (
        re.compile(
            r"(?i)\b((?:access|refresh|id)_token|client_secret|password|passwd|secret|api[_-]?key)"
            r"""(["']?\s*[:=]\s*["']?)[^\s"'&,;}]+"""
        ),
        rf"\1\2{REDACTED}",
    ),
    # Provider API keys: Anthropic, WorkOS, and other sk-style keys
    (re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"), REDACTED),
    (re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9]{8,}"), REDACTED),
    (re.compile(r"\bsk-[A-Za-z0-9]{20,}"), REDACTED),
    # AWS access key IDs
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), REDACTED),
)


def redact(text: str) -> str:
    """Replace anything that looks like a credential with a marker."""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFormatter(logging.Formatter):
    """A formatter that redacts the fully formatted record, traceback included."""

    @override
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: int | str = logging.INFO) -> None:
    """Route the root logger through one redacting stream handler."""
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
