from __future__ import annotations

import json
import os
from threading import Event, Lock
from typing import Any

import requests

from motionforge.errors import ErrorCode, MotionForgeError
from motionforge.providers.base import Provider, ProviderCapabilities


class OllamaProvider(Provider):
    name = "ollama"
    capabilities = ProviderCapabilities(True, False, True, True, True)

    def __init__(
        self,
        model: str = "llama3.1",
        base_url: str | None = None,
        *,
        connect_timeout: float = 5,
        read_timeout: float = 90,
        keep_alive: str = "10m",
        max_output_tokens: int = 4_000,
        retries: int = 1,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434").rstrip("/")
        self.timeout = (connect_timeout, read_timeout)
        self.keep_alive = keep_alive
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self._sessions: dict[str, requests.Session] = {}
        self._lock = Lock()
        self._schema_format_supported: bool | None = None

    def health(self) -> dict[str, Any]:
        try:
            response = requests.get(f"{self.base_url}/api/tags", timeout=(1, 2))
            response.raise_for_status()
            models = [item.get("name", "") for item in response.json().get("models", [])]
            return {"available": True, "models": models, "selectedModelAvailable": self.model in models}
        except requests.RequestException as error:
            return {"available": False, "error": str(error), "models": []}

    def list_models(self) -> list[str]:
        health = self.health()
        return list(health.get("models", []))

    def _generate(
        self,
        system: str,
        messages: list[dict[str, str]],
        output_format: str | dict[str, Any] | None,
        *,
        request_id: str,
        cancel_event: Event,
    ) -> str:
        if cancel_event.is_set():
            raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "keep_alive": self.keep_alive,
            "options": {"temperature": 0.1, "num_predict": self.max_output_tokens, "seed": 0},
        }
        if output_format is not None:
            payload["format"] = output_format
        session = requests.Session()
        with self._lock:
            self._sessions[request_id] = session
        try:
            last_error: Exception | None = None
            for attempt in range(self.retries + 1):
                if cancel_event.is_set():
                    raise MotionForgeError(ErrorCode.CANCELLED, "Compilation was cancelled.")
                try:
                    response = session.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout)
                    response.raise_for_status()
                    content = response.json().get("message", {}).get("content")
                    if not isinstance(content, str):
                        raise ValueError("Ollama returned no message content")
                    return content
                except (requests.RequestException, ValueError) as error:
                    last_error = error
                    if attempt >= self.retries:
                        break
            raise MotionForgeError(
                ErrorCode.MODEL_UNAVAILABLE,
                "The selected Ollama model is unavailable.",
                details=str(last_error),
                retriable=True,
            )
        finally:
            with self._lock:
                self._sessions.pop(request_id, None)
            session.close()

    def generate_text(self, system: str, messages: list[dict[str, str]], *, request_id: str, cancel_event: Event) -> str:
        return self._generate(system, messages, None, request_id=request_id, cancel_event=cancel_event)

    def generate_structured(self, system: str, messages: list[dict[str, str]], schema: dict[str, Any], *, request_id: str, cancel_event: Event) -> str:
        if self._schema_format_supported is False:
            instruction = "\nReturn JSON matching this exact JSON Schema:\n" + json.dumps(schema, separators=(",", ":"))
            return self._generate(system + instruction, messages, "json", request_id=request_id, cancel_event=cancel_event)
        try:
            result = self._generate(system, messages, schema, request_id=request_id, cancel_event=cancel_event)
            self._schema_format_supported = True
            return result
        except MotionForgeError as error:
            # Older Ollama servers reject a schema object in `format` with
            # HTTP 400. Detect that capability once and retain JSON mode as a
            # bounded fallback while keeping the exact schema in the prompt.
            if error.code != ErrorCode.MODEL_UNAVAILABLE or "400" not in str(error.details):
                raise
            self._schema_format_supported = False
            instruction = "\nReturn JSON matching this exact JSON Schema:\n" + json.dumps(schema, separators=(",", ":"))
            return self._generate(system + instruction, messages, "json", request_id=request_id, cancel_event=cancel_event)

    def cancel(self, request_id: str) -> None:
        with self._lock:
            session = self._sessions.get(request_id)
        if session is not None:
            session.close()
