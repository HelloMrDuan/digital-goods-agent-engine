from __future__ import annotations

from typing import Any

from ...config import settings
from .qiniu import chat_json_with_meta as qiniu_chat_with_meta
from ..sensenova import chat_json as sense_chat


class LLMRouterError(RuntimeError):
    pass


def chat_json_with_meta(**kwargs) -> tuple[dict[str, Any], dict[str, Any]]:
    errors: list[str] = []

    if settings.is_qiniu_configured:
        try:
            return qiniu_chat_with_meta(**kwargs)
        except Exception as exc:
            errors.append(f"Qiniu: {exc}")
    else:
        errors.append("Qiniu: not configured")

    if settings.is_sensenova_configured:
        try:
            result = sense_chat(**kwargs)
            usage = result.pop("_usage", {}) if isinstance(result, dict) else {}
            return result, {
                "provider": "SenseNova",
                "model": settings.sensenova_model,
                "usage": usage,
            }
        except Exception as exc:
            errors.append(f"SenseNova: {exc}")
    else:
        errors.append("SenseNova: not configured")

    raise LLMRouterError("All configured LLM providers failed. " + " | ".join(errors))


def chat_json(**kwargs) -> dict[str, Any]:
    result, _ = chat_json_with_meta(**kwargs)
    return result
