from __future__ import annotations

import re

from .config import settings


def redact_error(value: object) -> str:
    text = str(value)
    for name in type(settings).model_fields:
        if any(part in name for part in ("token", "secret", "api_key")):
            secret = str(getattr(settings, name) or "")
            if secret:
                text = text.replace(secret, "[REDACTED]")
    text = re.sub(r"(?i)(Bearer\s+)[^\s\"']+", r"\1[REDACTED]", text)
    return text
