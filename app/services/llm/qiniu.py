import json
import httpx

from ...config import settings


class QiniuError(RuntimeError):
    pass


def chat_json(**kwargs):

    url = settings.qiniu_base_url.rstrip("/")

    if not url.endswith("/chat/completions"):
        url += "/chat/completions"

    headers = {
        "Authorization": f"Bearer {settings.qiniu_api_token}",
        "Content-Type": "application/json",
    }

    messages = []

    if kwargs.get("system"):
        messages.append(
            {
                "role": "system",
                "content": kwargs["system"],
            }
        )

    messages.append(
        {
            "role": "user",
            "content": json.dumps(
                kwargs.get("payload", {}),
                ensure_ascii=False,
            ),
        }
    )

    body = {
        "model": settings.qiniu_model,
        "messages": messages,
        "temperature": 0.2,
    }

    try:
        with httpx.Client(
            timeout=120,
            trust_env=False,
        ) as client:
            response = client.post(
                url,
                headers=headers,
                json=body,
            )

    except Exception as e:
        raise QiniuError(
            f"Qiniu connection failed: {e}"
        )

    if response.status_code != 200:
        raise QiniuError(
            f"Qiniu HTTP {response.status_code}: {response.text}"
        )

    try:
        data = response.json()
    except Exception:
        raise QiniuError(
            f"Qiniu invalid json response: {response.text}"
        )

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        raise QiniuError(
            f"Qiniu response format invalid: {data}"
        )

    try:
        return json.loads(content)
    except Exception:
        raise QiniuError(
            f"Qiniu content is not valid JSON: {content}"
        )
