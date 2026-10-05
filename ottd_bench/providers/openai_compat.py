"""Any server speaking the OpenAI chat-completions dialect.

Covers OpenAI, OpenRouter, Gemini's OpenAI endpoint, Groq, Mistral, and local servers such as
llama.cpp's llama-server, LM Studio and vLLM.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

from .base import (Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn, image_b64,
                   json_tool_prompt, parse_arguments, parse_json_actions, results_as_text)


class OpenAICompatProvider(Provider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.base_url = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        self.json_mode = cfg.tool_mode == "json"
        headers = {}
        if cfg.api_key_env:
            if not cfg.api_key:
                raise ProviderError(f"environment variable {cfg.api_key_env} is not set (needed for model '{cfg.key}')")
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        self.http = httpx.Client(headers=headers, timeout=httpx.Timeout(cfg.options.get("timeout", 900), connect=15))
        self.messages: list[dict] = []
        self.tools: list[dict] = []

    def begin(self, system: str, tools: list[ToolSpec]) -> None:
        if self.json_mode and tools:
            system = system + "\n\n" + json_tool_prompt(tools)
        self.messages = [{"role": "system", "content": system}]
        self.tools = [] if self.json_mode else [
            {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
            for t in tools
        ]

    def add_user(self, text: str, images: list[Path] | None = None) -> None:
        if not images:
            self.messages.append({"role": "user", "content": text})
            return
        parts: list[dict] = []
        for p in images:
            media, data = image_b64(p)
            parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{data}"}})
        parts.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": parts})

    def add_tool_results(self, results: list[ToolResult]) -> None:
        if self.json_mode:
            self.messages.append({"role": "user", "content": results_as_text(results)})
            return
        for r in results:
            self.messages.append({"role": "tool", "tool_call_id": r.call.id, "content": r.text})

    def generate(self) -> Turn:
        opts = self.cfg.options
        body: dict = {"model": self.cfg.model, "messages": self.messages}
        # OpenAI's reasoning models only accept max_completion_tokens; most other servers only max_tokens.
        body[opts.get("max_tokens_param", "max_tokens")] = self.cfg.max_tokens
        if self.cfg.temperature is not None:
            body["temperature"] = self.cfg.temperature
        if self.tools:
            body["tools"] = self.tools
        body.update(opts.get("extra_body", {}))

        t0 = time.monotonic()
        data = self._post(body)
        choice = data["choices"][0]
        msg = choice.get("message", {})
        text = msg.get("content") or ""
        if isinstance(text, list):   # some servers return content parts
            text = "".join(part.get("text", "") for part in text if isinstance(part, dict))

        calls: list[ToolCall] = []
        if self.json_mode:
            calls = parse_json_actions(text)
            self.messages.append({"role": "assistant", "content": text})
        else:
            raw_calls = msg.get("tool_calls") or []
            for i, tc in enumerate(raw_calls):
                fn = tc.get("function", {})
                args, err = parse_arguments(fn.get("arguments"))
                calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), args=args, error=err))
            reply: dict = {"role": "assistant", "content": text}
            if raw_calls:
                # Replay the calls with ids filled in, so every result can be matched to its call.
                reply["tool_calls"] = [
                    {"id": c.id, "type": "function",
                     "function": {"name": c.name, "arguments": json.dumps(c.args)}} for c in calls
                ]
            self.messages.append(reply)

        usage = data.get("usage") or {}
        finish = choice.get("finish_reason")
        return Turn(
            text=text,
            tool_calls=calls,
            stop={"length": "length", "content_filter": "refusal"}.get(finish, "tool_use" if calls else "end"),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0) or 0,
            seconds=time.monotonic() - t0,
        )

    def _post(self, body: dict) -> dict:
        last = ""
        for attempt in range(5):
            try:
                resp = self.http.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.HTTPError as e:
                last = repr(e)
            else:
                if resp.status_code == 200:
                    return resp.json()
                last = f"HTTP {resp.status_code}: {resp.text[:400]}"
                if resp.status_code not in (408, 409, 429) and resp.status_code < 500:
                    raise ProviderError(f"{self.base_url} rejected the request: {last}")
            time.sleep(min(2 ** attempt, 20))
        raise ProviderError(f"{self.base_url} kept failing: {last}")
