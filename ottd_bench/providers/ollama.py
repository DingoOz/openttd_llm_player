"""Local models through Ollama's native /api/chat (needed for num_ctx and think control)."""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from .base import (Provider, ProviderError, ToolCall, ToolResult, ToolSpec, Turn, image_b64,
                   json_tool_prompt, parse_arguments, parse_json_actions, results_as_text)


class OllamaProvider(Provider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.base_url = (cfg.base_url or "http://localhost:11434").rstrip("/")
        self.json_mode = cfg.tool_mode == "json"
        # Local inference can be slow (CPU fallback, long prompts); be patient.
        self.http = httpx.Client(timeout=httpx.Timeout(cfg.options.get("timeout", 3600), connect=10))
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
        msg: dict = {"role": "user", "content": text}
        if images:
            msg["images"] = [image_b64(p)[1] for p in images]
        self.messages.append(msg)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        if self.json_mode:
            self.messages.append({"role": "user", "content": results_as_text(results)})
            return
        for r in results:
            self.messages.append({"role": "tool", "content": r.text, "tool_name": r.call.name})

    def generate(self) -> Turn:
        opts = self.cfg.options
        options = {"num_ctx": opts.get("num_ctx", 16384), "num_predict": self.cfg.max_tokens}
        if self.cfg.temperature is not None:
            options["temperature"] = self.cfg.temperature
        options.update(opts.get("ollama", {}))   # raw Ollama options, e.g. {num_gpu: 0} to force CPU
        body = {
            "model": self.cfg.model,
            "messages": self.messages,
            "stream": False,
            "options": options,
            "keep_alive": opts.get("keep_alive", "30m"),
        }
        if self.tools:
            body["tools"] = self.tools
        if "think" in opts:
            body["think"] = opts["think"]

        t0 = time.monotonic()
        try:
            resp = self.http.post(f"{self.base_url}/api/chat", json=body)
        except httpx.HTTPError as e:
            raise ProviderError(f"cannot reach Ollama at {self.base_url}: {e!r}") from e
        if resp.status_code != 200:
            detail = resp.text[:400]
            hint = ""
            if "does not support tools" in detail:
                hint = " -> set tool_mode: json for this model in models.yaml"
            elif "does not support thinking" in detail:
                hint = " -> remove options.think for this model in models.yaml"
            elif "out of memory" in detail or "cudaMalloc" in detail:
                hint = " -> GPU memory is full: free VRAM, lower options.num_ctx, or force CPU with options.ollama.num_gpu: 0"
            raise ProviderError(f"Ollama returned {resp.status_code}: {detail}{hint}")
        data = resp.json()
        msg = data.get("message", {})
        text = msg.get("content") or ""

        calls: list[ToolCall] = []
        if self.json_mode:
            calls = parse_json_actions(text)
            self.messages.append({"role": "assistant", "content": text})
        else:
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                args, err = parse_arguments(fn.get("arguments"))
                calls.append(ToolCall(id=tc.get("id") or f"call_{i}", name=fn.get("name", ""), args=args, error=err))
            # Reasoning text is not replayed: it only costs context and the chat template drops it anyway.
            reply = {"role": "assistant", "content": text}
            if msg.get("tool_calls"):
                reply["tool_calls"] = msg["tool_calls"]
            self.messages.append(reply)

        return Turn(
            text=text,
            tool_calls=calls,
            stop="length" if data.get("done_reason") == "length" else ("tool_use" if calls else "end"),
            input_tokens=data.get("prompt_eval_count", 0),
            output_tokens=data.get("eval_count", 0),
            seconds=time.monotonic() - t0,
        )
