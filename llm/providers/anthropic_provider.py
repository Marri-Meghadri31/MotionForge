# llm/providers/anthropic_provider.py
from anthropic import Anthropic

from llm.providers.base import LLMProvider


class AnthropicProvider(LLMProvider):
    def __init__(self, model="claude-sonnet-5", api_key=None):
        self.client = Anthropic(api_key=api_key)
        self.model = model

    def generate(self, system, messages):
        response = self.client.messages.create(
            model=self.model, max_tokens=2000, system=system, messages=messages
        )
        return "".join(b.text for b in response.content if b.type == "text")