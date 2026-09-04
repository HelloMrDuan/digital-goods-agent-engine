from typing import Any, Protocol

class LLMProvider(Protocol):
    def chat_json(self, *, operation: str, entity_type: str, entity_id: int|None, system: str, payload: dict[str, Any], max_new_tokens:int=1800)->dict[str,Any]: ...

