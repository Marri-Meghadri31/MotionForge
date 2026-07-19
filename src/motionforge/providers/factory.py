from __future__ import annotations

from motionforge.providers.base import Provider


def build_provider(name: str, model: str | None = None, *, timeout: float = 90) -> Provider:
    if name == "ollama":
        from motionforge.providers.ollama import OllamaProvider

        return OllamaProvider(model=model or "llama3.1", read_timeout=timeout)
    if name == "anthropic":
        from motionforge.providers.anthropic import AnthropicProvider

        return AnthropicProvider(model=model or "claude-sonnet-4-5", timeout=timeout)
    raise ValueError(f"unknown provider '{name}'")
