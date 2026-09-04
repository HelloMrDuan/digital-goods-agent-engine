from .qiniu import chat_json as qiniu_chat
from ..sensenova import chat_json as sense_chat

def chat_json(**kwargs):
    try:
        return qiniu_chat(**kwargs)
    except Exception:
        return sense_chat(**kwargs)
