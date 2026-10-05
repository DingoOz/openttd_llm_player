"""Provider-neutral conversation interface.

A provider owns one append-only conversation. The agent never edits earlier messages; when
the context budget is exhausted it calls begin() again with a fresh briefing. Append-only
histories keep prompt caches warm (Ollama's KV cache, Anthropic/OpenAI prompt caching) and are
required by models that bind their reasoning blocks to the exact conversation prefix.
"""

from __future__ import annotations

import base64
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ModelConfig


class ProviderError(RuntimeError):
    pass


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict            # JSON Schema of type "object"


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict
    error: str | None = None    # set when the model's arguments could not be parsed


@dataclass
class ToolResult:
    call: ToolCall
    text: str
    ok: bool = True


@dataclass
class Turn:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop: str = "end"           # end | tool_use | length | refusal
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0


def image_b64(path: Path) -> tuple[str, str]:
    media = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    return media, base64.standard_b64encode(path.read_bytes()).decode("ascii")


class Provider(ABC):
    def __init__(self, cfg: ModelConfig):
        self.cfg = cfg

    @abstractmethod
    def begin(self, system: str, tools: list[ToolSpec]) -> None:
        """Start a new conversation, discarding the previous one."""

    @abstractmethod
    def add_user(self, text: str, images: list[Path] | None = None) -> None: ...

    @abstractmethod
    def add_tool_results(self, results: list[ToolResult]) -> None: ...

    @abstractmethod
    def generate(self) -> Turn:
        """Run the model on the conversation so far and append its reply."""


# --------------------------------------------------------------------------------------
# Prompt-based tool calling, for models or servers without native function calling.

JSON_TOOL_INSTRUCTIONS = """
# How to call tools
You cannot call functions natively. To act, end your reply with exactly one fenced JSON block:

```json
{"actions": [{"tool": "<tool name>", "args": {<arguments>}}]}
```

List several actions to run them in order. Reply without a JSON block only if you want to do nothing this turn.

# Tools
"""


def json_tool_prompt(tools: list[ToolSpec]) -> str:
    lines = [JSON_TOOL_INSTRUCTIONS.strip(), ""]
    for t in tools:
        lines.append(f"- {t.name}: {t.description}\n  args schema: {json.dumps(t.parameters, separators=(',', ':'))}")
    return "\n".join(lines)


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_json_actions(text: str) -> list[ToolCall]:
    """Extract tool calls from a reply written under JSON_TOOL_INSTRUCTIONS."""
    candidates = _FENCE.findall(text)
    if not candidates:
        start = text.rfind('{"actions"')
        if start >= 0:
            candidates = [text[start:]]
    for raw in reversed(candidates):
        try:
            obj, _ = json.JSONDecoder().raw_decode(raw)
        except json.JSONDecodeError:
            continue
        actions = obj.get("actions") if isinstance(obj, dict) else None
        if not isinstance(actions, list):
            continue
        calls = []
        for i, a in enumerate(actions):
            if isinstance(a, dict) and isinstance(a.get("tool"), str):
                args = a.get("args", {})
                calls.append(ToolCall(id=f"json_{i}", name=a["tool"], args=args if isinstance(args, dict) else {},
                                      error=None if isinstance(args, dict) else "args must be an object"))
        return calls
    if "```" in text or '"actions"' in text:
        return [ToolCall(id="json_0", name="(unparseable)", args={},
                         error='your JSON block could not be parsed; use {"actions": [{"tool": ..., "args": {...}}]}')]
    return []


def results_as_text(results: list[ToolResult]) -> str:
    return "Tool results:\n" + "\n".join(f"[{i + 1}] {r.call.name}: {r.text}" for i, r in enumerate(results))


def parse_arguments(raw) -> tuple[dict, str | None]:
    """Native tool-call arguments arrive as a dict or a JSON string, depending on the server."""
    if isinstance(raw, dict):
        return raw, None
    if raw in (None, ""):
        return {}, None
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}, "arguments were not valid JSON"
    return (value, None) if isinstance(value, dict) else ({}, "arguments must be a JSON object")
