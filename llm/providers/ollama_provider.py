# llm/providers/ollama_provider.py
import requests

from llm.providers.base import LLMProvider


class OllamaProvider(LLMProvider):
    def __init__(self, model="llama3.1", host="http://localhost:11434"):
        self.model = model
        self.host = host

    def generate(self, system, messages):
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}] + messages,
            "format": "json",   # constrains output to valid JSON, if the model supports it
            "stream": False,
        }
        resp = requests.post(f"{self.host}/api/chat", json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json()["message"]["content"]