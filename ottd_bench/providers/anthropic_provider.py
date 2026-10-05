"""Claude through the official Anthropic SDK."""

from __future__ import annotations

import time
from pathlib import Path

import anthropic

from .base import Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn, image_b64


class AnthropicProvider(Provider):
    def __init__(self, cfg):
        super().__init__(cfg)
        if cfg.temperature is not None:
            # Current Claude models reject sampling parameters and the SDK no longer has the argument.
            raise ProviderError(f"model '{cfg.key}': temperature cannot be set for Anthropic models; remove it from models.yaml")
        kwargs: dict = {"max_retries": 4}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        # Only pass a key when one was explicitly configured and present. Otherwise the SDK
        # resolves credentials itself (ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, `ant auth login`).
        if cfg.api_key_env and cfg.api_key:
            kwargs["api_key"] = cfg.api_key
        self.client = anthropic.Anthropic(**kwargs)
        self.system = ""
        self.tools: list[dict] = []
        self.messages: list[dict] = []

    def begin(self, system: str, tools: list[ToolSpec]) -> None:
        # A fresh list, never an edited one: replacing the whole history replays no earlier
        # thinking blocks, which is the only client-side compaction that keeps them valid.
        self.system = system
        self.tools = [{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools]
        self.messages = []

    def add_user(self, text: str, images: list[Path] | None = None) -> None:
        content: list[dict] = []
        for p in images or []:
            media, data = image_b64(p)
            content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
        content.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": content})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        # All results of one assistant turn go back in a single user message.
        self.messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": r.call.id, "content": r.text, "is_error": not r.ok} for r in results
        ]})

    def generate(self) -> Turn:
        opts = self.cfg.options
        kwargs: dict = {
            "model": self.cfg.model,
            "max_tokens": self.cfg.max_tokens,
            "system": self.system,
            "messages": self.messages,
            # Caches the growing prefix; each turn only pays full price for what is new.
            "cache_control": {"type": "ephemeral"},
        }
        if self.tools:
            kwargs["tools"] = self.tools
        # Model-specific knobs come from models.yaml: current Opus/Sonnet take output_config.effort,
        # Haiku 4.5 rejects it. Nothing is sent unless configured.
        if "effort" in opts:
            kwargs["output_config"] = {"effort": opts["effort"]}
        if "thinking" in opts:
            kwargs["thinking"] = opts["thinking"]

        t0 = time.monotonic()
        try:
            resp = self.client.messages.create(**kwargs)
        except anthropic.AuthenticationError as e:
            raise ProviderError("Anthropic credentials were rejected. Set ANTHROPIC_API_KEY or run `ant auth login`.") from e
        except anthropic.BadRequestError as e:
            raise ProviderError(f"Anthropic rejected the request (check options for '{self.cfg.key}' in models.yaml): {e.message}") from e
        except anthropic.NotFoundError as e:
            raise ProviderError(f"unknown Anthropic model '{self.cfg.model}'") from e
        except anthropic.RateLimitError as e:
            raise ProviderError(f"Anthropic rate limit persisted after retries: {e.message}") from e
        except anthropic.APIStatusError as e:
            raise ProviderError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise ProviderError(f"cannot reach the Anthropic API: {e!r}") from e

        # Replay the reply's blocks unchanged (thinking blocks included).
        self.messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "refusal":
            # Deliberately no server-side fallback: a benchmark must not silently swap the model under test.
            category = resp.stop_details.category if resp.stop_details else None
            return Turn(text=f"[model refused; category={category}]", stop="refusal",
                        input_tokens=resp.usage.input_tokens, output_tokens=resp.usage.output_tokens,
                        seconds=time.monotonic() - t0)

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(id=b.id, name=b.name, args=b.input if isinstance(b.input, dict) else {})
                 for b in resp.content if b.type == "tool_use"]
        usage = resp.usage
        cached = getattr(usage, "cache_read_input_tokens", 0) or 0
        written = getattr(usage, "cache_creation_input_tokens", 0) or 0
        return Turn(
            text=text,
            tool_calls=calls,
            stop={"max_tokens": "length", "tool_use": "tool_use"}.get(resp.stop_reason, "end"),
            input_tokens=usage.input_tokens + cached + written,
            output_tokens=usage.output_tokens,
            cached_tokens=cached,
            seconds=time.monotonic() - t0,
        )
