from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from threading import Event
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    structured_output: bool
    streaming: bool
    cancellation: bool
    keep_alive: bool
    local: bool


class Provider(ABC):
    name: str
    model: str
    capabilities: ProviderCapabilities

    @abstractmethod
    def health(self) -> dict[str, Any]: ...

    @abstractmethod
    def list_models(self) -> list[str]: ...

    @abstractmethod
    def generate_text(
        self,
        system: str,
        messages: list[dict[str, str]],
        *,
        request_id: str,
        cancel_event: Event,
    ) -> str: ...

    @abstractmethod
    def generate_structured(
        self,
        system: str,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        *,
        request_id: str,
        cancel_event: Event,
    ) -> str: ...

    @abstractmethod
    def cancel(self, request_id: str) -> None: ...
