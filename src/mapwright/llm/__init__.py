"""Language model access: a small provider interface, Gemini, a local model through
Ollama, and a response cache."""

from .cache import CachedProvider, CacheMiss
from .provider import LLMError, LLMRequest, LLMResponse, Provider, Usage


def make_provider(model: str | None = None) -> Provider:
    """The live provider for a model name: ``ollama/...`` runs locally, anything else is Gemini."""
    from .ollama import OllamaProvider, is_ollama

    if is_ollama(model):
        return OllamaProvider(model)
    from .gemini import GeminiProvider

    return GeminiProvider(model or None)


__all__ = [
    "CacheMiss",
    "CachedProvider",
    "LLMError",
    "LLMRequest",
    "LLMResponse",
    "Provider",
    "Usage",
    "make_provider",
]
