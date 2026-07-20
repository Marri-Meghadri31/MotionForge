from __future__ import annotations

import os
from threading import Event
from typing import Any

from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.providers.base import Provider, ProviderCapabilities


class AnthropicProvider(Provider):
    name = "anthropic"
    capabilities = ProviderCapabilities(False, False, False, False, False)

    def __init__(self, model: str = "claude-sonnet-4-5", api_key: str | None = None, *, timeout: float = 90) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.timeout = timeout

    def _client(self):
        if not self.api_key:
            raise MotionForgeError(ErrorCode.MODEL_UNAVAILABLE, "An Anthropic API key is not configured.")
        from anthropic import Anthropic

        return Anthropic(api_key=self.api_key, timeout=self.timeout, max_retries=1)

    def health(self) -> dict[str, Any]:
        return {"available": bool(self.api_key), "models": [self.model] if self.api_key else []}

    def list_models(self) -> list[str]:
        return [self.model] if self.api_key else []

    def generate_text(self, system: str, messages: list[dict[str, str]], *, request_id: str, cancel_event: Event) -> str:
        if cancel_event.is_set():
            raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
        try:
            response = self._client().messages.create(
                model=self.model,
                max_tokens=4_000,
                temperature=0.1,
                system=system,
                messages=messages,
            )
            if cancel_event.is_set():
                raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
            return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        except MotionForgeError:
            raise
        except Exception as error:
            raise MotionForgeError(ErrorCode.MODEL_UNAVAILABLE, "The Anthropic model is unavailable.", details=str(error), retriable=True) from error

    def generate_structured(self, system: str, messages: list[dict[str, str]], schema: dict[str, Any], *, request_id: str, cancel_event: Event) -> str:
        schema_instruction = f"\nReturn JSON matching this exact JSON Schema:\n{schema}"
        return self.generate_text(system + schema_instruction, messages, request_id=request_id, cancel_event=cancel_event)

    def cancel(self, request_id: str) -> None:
        return None
