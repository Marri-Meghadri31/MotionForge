# llm/providers/base.py
from abc import ABC, abstractmethod

class LLMProvider(ABC):
    @abstractmethod
    def generate(self, system: str, messages: list[dict]) -> str:
        """Returns raw text response (expected to be JSON)."""