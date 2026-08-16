import re

from hypothesis import given
from hypothesis import strategies as st

from self_improving_coding_agent.scrub import scrub_text

SECRET_MARKERS = (
    "[REDACTED_SSN]", "[REDACTED_AWS_KEY]", "[REDACTED_CC]", "[REDACTED_SECRET]",
    "[REDACTED_PRIVATE_KEY]", "[REDACTED_JWT]", "[REDACTED_TOKEN]",
)


def test_redacts_ssn():
    assert "123-45-6789" not in scrub_text("ssn is 123-45-6789 ok")
    assert "[REDACTED_SSN]" in scrub_text("ssn is 123-45-6789 ok")


def test_redacts_aws_key():
    out = scrub_text("key AKIAIOSFODNN7EXAMPLE here")
    assert "AKIAIOSFODNN7EXAMPLE" not in out
    assert "[REDACTED_AWS_KEY]" in out


def test_redacts_secret_keeps_key_name():
    out = scrub_text('api_key = "s3cr3tValue123456"')
    assert "s3cr3tValue123456" not in out
    assert "api_key" in out
    assert "[REDACTED_SECRET]" in out


def test_dob_keeps_year_only():
    assert scrub_text("dob 1990-05-12") == "dob 1990"
    assert scrub_text("dob 05/12/1990") == "dob 1990"


def test_redacts_private_key_block():
    key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc123+/xyz\n-----END RSA PRIVATE KEY-----"
    out = scrub_text(f"leaked:\n{key}\nend")
    assert "MIIEabc123" not in out
    assert "[REDACTED_PRIVATE_KEY]" in out


def test_redacts_jwt():
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVC12.eyJzdWI6MTIzNDU2.SflKxwRJSMeKKF2QT4"
    out = scrub_text(f"auth {jwt}")
    assert jwt not in out
    assert "[REDACTED_JWT]" in out


def test_redacts_provider_tokens():
    for tok in (
        "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "xoxb-123456789012-abcdefABCDEF",
        "sk-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    ):
        out = scrub_text(f"token is {tok} ok")
        assert tok not in out
        assert "[REDACTED_TOKEN]" in out


def test_redacts_bearer_token():
    out = scrub_text("Authorization: Bearer abcdef0123456789XYZ")
    assert "abcdef0123456789XYZ" not in out
    assert "[REDACTED_TOKEN]" in out


def test_redacts_prefixed_secret_key_name():
    out = scrub_text("aws_secret_access_key=wJalrXUtnFEMI+K7MDENGbPxRfiCYEXAMPLEKEY")
    assert "wJalrXUtnFEMI" not in out
    assert "aws_secret_access_key" in out
    assert "[REDACTED_SECRET]" in out


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=200))
def test_scrub_is_idempotent(text):
    once = scrub_text(text)
    assert scrub_text(once) == once


@given(
    a=st.integers(100, 999),
    b=st.integers(10, 99),
    c=st.integers(1000, 9999),
)
def test_any_ssn_shape_is_removed(a, b, c):
    raw = f"user {a}-{b}-{c} record"
    out = scrub_text(raw)
    assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", out)
