"""Refuse a write that would copy a one-time code, a reset or sign-in link, or a credential out of an email."""
from __future__ import annotations

import re
from typing import Callable, Optional

DIGITS = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
CODE_WORDS = re.compile(r"code|verification|otp|passcode", re.IGNORECASE)
NEAR = 40
URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
SENSITIVE_URL_PART = re.compile(r"reset|token|verify|magic", re.IGNORECASE)
TRAILING = ".,;:!?"
# Hermes's redaction also covers these. They are repeated so the refusal holds where redaction is turned off.
CREDENTIAL = re.compile(r"sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}")


def codes(email_text: str) -> set:
    found = set()
    for match in DIGITS.finditer(email_text):
        window = email_text[max(0, match.start() - NEAR): match.end() + NEAR]
        if CODE_WORDS.search(window):
            found.add(match.group(0))
    return found


def sensitive_urls(email_text: str) -> set:
    found = set()
    for match in URL.finditer(email_text):
        url = match.group(0).rstrip(TRAILING)
        rest = url.split("://", 1)[1]
        path_and_query = rest[rest.find("/"):] if "/" in rest else ""
        if SENSITIVE_URL_PART.search(path_and_query):
            found.add(url)
    return found


def refusal(write: str, email_text: str, redact: Callable[[str], str]) -> Optional[str]:
    """The reason to refuse `write`, which never repeats the value, or None to allow it."""
    written = set(DIGITS.findall(write))
    if written & codes(email_text):
        return "it holds a number that the email gives next to a code word, such as a one-time code"
    if any(url in write for url in sensitive_urls(email_text)):
        return "it holds a link from the email whose path or query names a reset, token, verify, or magic link"
    if CREDENTIAL.search(write) or redact(write) != write:
        return "it holds a string that the factory's secret redaction would hide, such as an API key or password"
    return None
