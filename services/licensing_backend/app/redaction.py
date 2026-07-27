from __future__ import annotations

import logging
import re


SENSITIVE_KEY = re.compile(
    r"(?i)(token|secret|password|private[_-]?key|entitlement|activation[_-]?code|invite[_-]?code)"
)
CODE = re.compile(r"\b(?:CA-[A-Z0-9-]{8,}|BETA-[A-Z0-9-]{8,})\b", re.IGNORECASE)
BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~-]+")


def redact_text(value: str) -> str:
    value = CODE.sub("[REDACTED-CODE]", value)
    value = BEARER.sub("Bearer [REDACTED]", value)
    return value


def redact(value):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if SENSITIVE_KEY.search(str(key)) else redact(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


class SecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(str(record.msg))
        if record.args:
            record.args = redact(record.args)
        return True
