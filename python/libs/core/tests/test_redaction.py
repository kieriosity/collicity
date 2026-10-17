import logging
import sys

import pytest

from collicity_core.redaction import REDACTED, RedactingFormatter, redact

JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.c2lnbmF0dXJlLWJ5dGVz"


@pytest.mark.parametrize(
    ("text", "secret"),
    [
        ("postgresql+psycopg://app_user:hunter2-pw@db:5432/collicity", "hunter2-pw"),
        ("Authorization: Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
        (f"token={JWT}", JWT),
        (f"id token {JWT} expired", JWT),
        ('{"refresh_token": "0.AX4A-rt-value"}', "0.AX4A-rt-value"),
        ("client_secret=s3cr3t-value&grant_type=x", "s3cr3t-value"),
        ("password: correct-horse", "correct-horse"),
        ("key sk-ant-api03-AbCdEfGhIjKlMnOp", "sk-ant-api03-AbCdEfGhIjKlMnOp"),
        ("WORKOS_API_KEY=sk_test_a1B2c3D4e5F6", "sk_test_a1B2c3D4e5F6"),
        ("aws AKIAIOSFODNN7EXAMPLE used", "AKIAIOSFODNN7EXAMPLE"),
    ],
)
def test_redact_removes_credentials(text: str, secret: str):
    result = redact(text)
    assert secret not in result
    assert REDACTED in result


@pytest.mark.parametrize(
    "text",
    [
        "run 0192a3b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b moved to Waiting: approval",
        "tenant acme reserved 44000 micro-USD",
        "https://graph.microsoft.com/v1.0/me/messages",
    ],
)
def test_redact_leaves_ordinary_text_alone(text: str):
    assert redact(text) == text


def test_formatter_redacts_message_and_traceback():
    formatter = RedactingFormatter("%(message)s")
    try:
        raise RuntimeError(f"refresh failed: refresh_token={JWT}")
    except RuntimeError:
        record = logging.LogRecord(
            "t", logging.ERROR, __file__, 1, "calling %s", ("Bearer abcdefghijkl12",), None
        )
        record.exc_info = sys.exc_info()
    output = formatter.format(record)
    assert JWT not in output
    assert "abcdefghijkl12" not in output
    assert "RuntimeError" in output
