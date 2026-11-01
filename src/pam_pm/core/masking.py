"""Mask PII and secrets before any model call (F8). Pure functions only."""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_INTL_RE = re.compile(r"\+\d[\d\s().-]{6,}\d")
PHONE_NA_RE = re.compile(r"(?<!\d)\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")
SLACK_TOKEN_RE = re.compile(r"xox[a-z]-[A-Za-z0-9-]+")
APP_TOKEN_RE = re.compile(r"xapp-[A-Za-z0-9-]+")
GITHUB_TOKEN_RE = re.compile(r"(?:github_pat|ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]+")
BEARER_RE = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/=]+")
BASE64_RUN_RE = re.compile(r"\b[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])")


def _looks_secret(value: str) -> bool:
    """True when a base64 run has mixed case plus a digit (PAT-like).

    This keeps plain long words and lowercase hex such as git SHAs intact.
    """
    return (
        any(c.islower() for c in value)
        and any(c.isupper() for c in value)
        and any(c.isdigit() for c in value)
    )


def _mask_base64(match: re.Match[str]) -> str:
    return "[SECRET_REDACTED]" if _looks_secret(match.group(0)) else match.group(0)


def mask_text(text: str) -> str:
    """Return text with emails, phones, and tokens replaced by placeholders."""
    masked = BEARER_RE.sub("Bearer [TOKEN_REDACTED]", text)
    masked = SLACK_TOKEN_RE.sub("[SLACK_TOKEN_REDACTED]", masked)
    masked = APP_TOKEN_RE.sub("[APP_TOKEN_REDACTED]", masked)
    masked = GITHUB_TOKEN_RE.sub("[GITHUB_TOKEN_REDACTED]", masked)
    masked = EMAIL_RE.sub("[EMAIL_REDACTED]", masked)
    masked = PHONE_INTL_RE.sub("[PHONE_REDACTED]", masked)
    masked = PHONE_NA_RE.sub("[PHONE_REDACTED]", masked)
    return BASE64_RUN_RE.sub(_mask_base64, masked)
