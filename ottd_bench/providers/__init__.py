from __future__ import annotations

from ..config import ModelConfig
from .base import Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn

__all__ = ["Provider", "ProviderError", "ToolCall", "ToolResult", "ToolSpec", "Turn", "make_provider"]


def make_provider(cfg: ModelConfig) -> Provider:
    if cfg.provider == "ollama":
        from .ollama import OllamaProvider
        return OllamaProvider(cfg)
    if cfg.provider == "openai":
        from .openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(cfg)
    if cfg.provider == "anthropic":
        from .anthropic_provider import AnthropicProvider
        return AnthropicProvider(cfg)
    if cfg.provider == "scripted":
        from .scripted import ScriptedProvider
        return ScriptedProvider(cfg)
    raise ProviderError(f"unknown provider '{cfg.provider}' for model '{cfg.key}' (use ollama, openai, anthropic or scripted)")
