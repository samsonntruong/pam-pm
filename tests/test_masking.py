"""Masking: emails, phones, and tokens are redacted with no leftovers."""

from pam_pm.core.masking import mask_text

EMAIL = "ana.smith@example.com"
PHONE_INTL = "+1 (415) 555-0132"
PHONE_PLAIN = "415-555-0199"
SLACK_TOKEN = "xoxb-fake1234567890-fakeabcdefghij"
APP_TOKEN = "xapp-fake1-fake-APP1234567890"
GITHUB_TOKEN = "github_pat_fake0123456789abcdefABCDEF"
BEARER_TOKEN = "fak3.Bearer.Token-Value_123"
ADO_PAT = "AbC123xYzAbC123xYzAbC123xYzAbC123xYz0987"


def test_masks_email_phone_and_tokens() -> None:
    raw = (
        f"Contact {EMAIL} or {PHONE_INTL} or {PHONE_PLAIN}. "
        f"Slack {SLACK_TOKEN} app {APP_TOKEN} gh {GITHUB_TOKEN} "
        f"auth Bearer {BEARER_TOKEN} ado {ADO_PAT}."
    )
    masked = mask_text(raw)
    for secret in (
        EMAIL,
        PHONE_INTL,
        PHONE_PLAIN,
        SLACK_TOKEN,
        APP_TOKEN,
        GITHUB_TOKEN,
        BEARER_TOKEN,
        ADO_PAT,
    ):
        assert secret not in masked, secret
    assert "[EMAIL_REDACTED]" in masked
    assert masked.count("[PHONE_REDACTED]") == 2
    assert "[SLACK_TOKEN_REDACTED]" in masked
    assert "[APP_TOKEN_REDACTED]" in masked
    assert "[GITHUB_TOKEN_REDACTED]" in masked
    assert "Bearer [TOKEN_REDACTED]" in masked
    assert "[SECRET_REDACTED]" in masked


def test_leaves_plain_text_and_hex_alone() -> None:
    assert mask_text("hello world") == "hello world"
    sha = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
    assert mask_text(f"commit {sha} looks good") == f"commit {sha} looks good"
