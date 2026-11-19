"""1.2.52: nothing token-like ever reaches a log file, a console, an error message or a report.

redact(text) masks:
  * Google access / refresh tokens (ya29..., 1//...), client secrets (GOCSPX-...), API keys (AIza...)
  * Telegram bot tokens (123456789:AA...), Meta / Instagram tokens (EAA..., IGQV..., IGAA...)
  * GitHub tokens (ghp_..., github_pat_...), "Bearer ..." headers, JWTs
  * key=value / "key": "value" pairs whose key names a secret (access_token, refresh_token,
    client_secret, password, api_key, sessionid, ...), in URLs, JSON and plain text alike.

install() wraps sys.stdout / sys.stderr (the pythonw log file included) and adds a logging
filter, so third-party libraries are covered too. SharkyClips' own secrets live only in
~/.sharkyclips, encrypted (secretstore.py); this is the last line of defence.
"""

from __future__ import annotations

import io
import logging
import re
import sys
from typing import Any

MASK = "[redacted]"

_SECRET_KEYS = (r"access_token|refresh_token|id_token|client_secret|app_secret|api_key|apikey|api-key|"
                r"password|passwd|secret|sessionid|session_id|sc_session|auth_token|bot_token|"
                r"x-sharkyclips-token|authorization|code_verifier|verifier|private_key")

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ya29\.[0-9A-Za-z_\-]{10,}"), MASK),
    (re.compile(r"\b1//[0-9A-Za-z_\-]{20,}"), MASK),
    (re.compile(r"GOCSPX-[0-9A-Za-z_\-]{10,}"), MASK),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"), MASK),
    (re.compile(r"\b\d{6,12}:AA[0-9A-Za-z_\-]{30,}"), MASK),  # Telegram bot token
    (re.compile(r"\bbot\d{6,12}:[0-9A-Za-z_\-]{30,}"), "bot" + MASK),  # in a Telegram API URL
    (re.compile(r"\b(?:EAA|IGQV|IGAA)[0-9A-Za-z_\-]{20,}"), MASK),
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[0-9A-Za-z]{30,}|\bgithub_pat_[0-9A-Za-z_]{30,}"), MASK),
    (re.compile(r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}"), MASK),  # JWT
    (re.compile(r"(?i)\b(bearer|basic)\s+[0-9A-Za-z._~+/\-]{12,}=*"), r"\1 " + MASK),
    # "access_token": "...", 'client_secret': '...'
    (re.compile(rf"(?i)([\"'](?:{_SECRET_KEYS})[\"']\s*:\s*[\"'])[^\"']+([\"'])"), r"\1" + MASK + r"\2"),
    # access_token=... in URLs / form bodies / text; also "Authorization: ..."
    (re.compile(rf"(?i)\b((?:{_SECRET_KEYS})\s*[=:]\s*)(?![\"'\[])[^\s&\"',;}}]+"), r"\1" + MASK),
    # ?code=... of an OAuth redirect
    (re.compile(r"([?&](?:code|state)=)[^&\s\"']+"), r"\1" + MASK),
]


def redact(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return text
    for pat, repl in _PATTERNS:
        text = pat.sub(repl, text)
    return text


class RedactingStream(io.TextIOBase):
    """A text stream that masks secrets before they reach the real one."""

    def __init__(self, inner: Any):
        self._inner = inner

    def write(self, s: str) -> int:  # type: ignore[override]
        self._inner.write(redact(s))
        return len(s)

    def flush(self) -> None:
        try:
            self._inner.flush()
        except (OSError, ValueError):
            pass

    def isatty(self) -> bool:
        try:
            return bool(self._inner.isatty())
        except (OSError, ValueError, AttributeError):
            return False

    def fileno(self) -> int:
        return self._inner.fileno()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._inner, "encoding", "utf-8")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        clean = redact(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def install() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if stream is not None and not isinstance(stream, RedactingStream):
            setattr(sys, name, RedactingStream(stream))
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for f in root.filters):
        root.addFilter(RedactingFilter())
    for h in root.handlers:
        if not any(isinstance(f, RedactingFilter) for f in h.filters):
            h.addFilter(RedactingFilter())
