"""Checks that need neither OpenTTD nor a model.

Run with: PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 .venv/bin/python -m pytest tests/
(the variable keeps unrelated system-wide pytest plugins, e.g. from ROS, from being loaded)
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from ottd_bench.config import ModelConfig, Scenario
from ottd_bench.episode import _fmt_date
from ottd_bench.providers.base import ToolCall, ToolResult, ToolSpec, parse_json_actions
from ottd_bench.tools import _check_args
from ottd_bench.vision import MapGeometry, render_overview, render_view

SCHEMA = {"type": "object",
          "properties": {"x": {"type": "integer"}, "kind": {"type": "string", "enum": ["bus", "truck"]},
                         "ids": {"type": "array", "items": {"type": "integer"}}},
          "required": ["x", "kind"]}


def test_date_matches_openttd():
    assert _fmt_date(726833) == "1990-01-01"   # value reported by the game script for a 1990 start


def test_args_are_coerced_and_checked():
    assert _check_args({"x": "12", "kind": "bus", "ids": 3, "junk": 1}, SCHEMA) == ({"x": 12, "kind": "bus", "ids": [3]}, None)
    assert _check_args({"x": 12.0, "kind": "bus"}, SCHEMA)[0]["x"] == 12
    assert "one of" in _check_args({"x": 1, "kind": "tram"}, SCHEMA)[1]
    assert "missing" in _check_args({"x": 1}, SCHEMA)[1]
    assert "integer" in _check_args({"x": "north", "kind": "bus"}, SCHEMA)[1]


def test_json_tool_mode_parsing():
    text = 'I will build.\n```json\n{"actions": [{"tool": "look", "args": {"x": 1, "y": 2}}, {"tool": "wait", "args": {"days": 5}}]}\n```'
    calls = parse_json_actions(text)
    assert [(c.name, c.args) for c in calls] == [("look", {"x": 1, "y": 2}), ("wait", {"days": 5})]
    assert parse_json_actions("Nothing to do this turn.") == []
    assert parse_json_actions("```json\n{broken\n```")[0].error


def test_world_screenshot_geometry():
    # 64x64 map, sea level everywhere: the size OpenTTD 15.3 actually produces is 3969x2135.
    geo = MapGeometry(64, 64, ["0" * 64] * 64)
    assert geo.expected_size() == (3969, 2135)
    # +x moves down-left, +y moves down-right, one tile is 32 px across and 16 px down.
    x0, y0 = geo.world_px(10, 10)
    assert geo.world_px(11, 10) == (x0 - 32, y0 + 16)
    assert geo.world_px(10, 11) == (x0 + 32, y0 + 16)
    # A raised tile is drawn higher on screen.
    hill = [row[:] for row in [list("0" * 64) for _ in range(64)]]
    for yy in (10, 11):
        for xx in (10, 11):
            hill[yy][xx] = "2"
    raised = MapGeometry(64, 64, ["".join(r) for r in hill])
    assert raised.world_px(10, 10)[1] == y0 - 16


def test_rendering_runs():
    geo = MapGeometry(64, 64, ["0" * 64] * 64)
    world = Image.new("P", geo.expected_size())
    view = render_view(world, geo, 20, 20, "close", marks=[(21, 20, "station 0")])
    assert view.size == (1024, 768)
    towns = [{"name": "Testville", "x": 20, "y": 20}]
    minimap = Image.new("RGB", (64, 64))
    minimap.putpixel((0, 40), (255, 0, 0))    # OpenTTD's column 0 is tile x = 63
    overview = render_overview(minimap, geo, towns, [], [{"id": 0, "x": 21, "y": 20}])
    assert overview.width > 64 * 8
    # x reads left to right in the overview: tile (63, 40) is on the right, tile (0, 40) is not red.
    scale, margin = 12, 38
    assert overview.getpixel((margin + 63 * scale + 6, margin + 40 * scale + 6)) == (255, 0, 0)
    assert overview.getpixel((margin + 6, margin + 40 * scale + 6)) != (255, 0, 0)


def test_scenarios_and_models_load():
    for name in ("smoke", "bus_starter", "regional"):
        assert Scenario.load(name).game.map_size in (64, 128, 256, 512)
    for key in ModelConfig.all_keys():
        assert ModelConfig.load(key).provider in ("ollama", "openai", "anthropic", "scripted")


def test_video_caption_wrapping():
    from PIL import ImageDraw
    from ottd_bench.video import _wrap
    from ottd_bench.vision import _font

    draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    font = _font(16)
    text = "word " * 200
    lines = _wrap(draw, text, font, 300, 3)
    assert len(lines) == 3 and lines[-1].endswith("...")
    assert all(draw.textlength(line, font=font) <= 300 for line in lines)
    assert _wrap(draw, "short", font, 300, 3) == ["short"]
    assert _wrap(draw, "", font, 300, 3) == [""]


def test_bridge_matches_replies_by_id():
    """A slow request can be pending while a quick one is answered (filming relies on this)."""
    from collections import deque
    from ottd_bench.bridge import Bridge

    class FakeAdmin:
        def __init__(self):
            self.gamescript, self.console, self.sent = deque(), deque(), []
        def send_gamescript(self, obj):
            self.sent.append(obj)
        def poll(self):
            pass
        def pump(self, deadline):
            raise TimeoutError

    admin = FakeAdmin()
    b = Bridge(admin)
    slow = b.send("advance", days=3)
    quick = b.send("company")
    assert b.poll(slow) is None
    admin.gamescript.extend([{"id": quick, "seq": 0, "last": False, "d": '{"ok": tr'},
                             {"id": quick, "seq": 1, "last": True, "d": 'ue, "money": 5}'}])
    assert b.wait(quick, 1) == {"ok": True, "money": 5}
    assert b.poll(slow) is None
    admin.gamescript.append({"id": slow, "seq": 0, "last": True, "d": '{"ok": true, "date": "1990-01-04"}'})
    assert b.poll(slow) == {"ok": True, "date": "1990-01-04"}


# ---------------------------------------------------------------- provider adapters, offline

class _OpenAIStub(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append(body)
        reply = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "abc", "type": "function", "function": {"name": "wait", "arguments": '{"days": 3}'}}]}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 7}}
        data = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def test_openai_adapter_request_and_reply(tmp_path: Path):
    from ottd_bench.providers.openai_compat import OpenAICompatProvider

    server = HTTPServer(("127.0.0.1", 0), _OpenAIStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        img = tmp_path / "a.jpg"
        Image.new("RGB", (8, 8)).save(img)
        cfg = ModelConfig(key="t", provider="openai", model="m", base_url=f"http://127.0.0.1:{server.server_port}/v1")
        p = OpenAICompatProvider(cfg)
        p.begin("sys", [ToolSpec("wait", "wait", {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]})])
        p.add_user("hello", [img])
        turn = p.generate()
        assert [(c.id, c.name, c.args) for c in turn.tool_calls] == [("abc", "wait", {"days": 3})]
        assert (turn.input_tokens, turn.output_tokens, turn.stop) == (100, 7, "tool_use")
        p.add_tool_results([ToolResult(turn.tool_calls[0], "ok")])
        p.generate()
    finally:
        server.shutdown()
    first, second = _OpenAIStub.seen[-2:]
    assert first["tools"][0]["function"]["name"] == "wait"
    assert first["messages"][1]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    # The second request replays the assistant's call and answers it by id, after the unchanged prefix.
    assert second["messages"][:2] == first["messages"]
    assert second["messages"][2]["tool_calls"][0]["id"] == "abc"
    assert second["messages"][3] == {"role": "tool", "tool_call_id": "abc", "content": "ok"}


def test_anthropic_adapter_request_and_reply(tmp_path: Path, monkeypatch):
    from ottd_bench.providers.anthropic_provider import AnthropicProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    img = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(img)
    cfg = ModelConfig(key="t", provider="anthropic", model="claude-opus-5-5", max_tokens=16000, options={"effort": "medium"})
    p = AnthropicProvider(cfg)
    captured = []

    def fake_create(**kwargs):
        captured.append({**kwargs, "messages": list(kwargs["messages"])})
        return SimpleNamespace(
            content=[SimpleNamespace(type="thinking", thinking="", signature="s"),
                     SimpleNamespace(type="text", text="Building."),
                     SimpleNamespace(type="tool_use", id="tu_1", name="wait", input={"days": 3})],
            stop_reason="tool_use", stop_details=None,
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=90, cache_creation_input_tokens=0))

    monkeypatch.setattr(p.client.messages, "create", fake_create)
    p.begin("sys", [ToolSpec("wait", "wait", {"type": "object", "properties": {"days": {"type": "integer"}}, "required": ["days"]})])
    p.add_user("hello", [img])
    turn = p.generate()
    assert (turn.text, turn.stop, turn.input_tokens, turn.cached_tokens) == ("Building.", "tool_use", 100, 90)
    assert [(c.id, c.name, c.args) for c in turn.tool_calls] == [("tu_1", "wait", {"days": 3})]
    p.add_tool_results([ToolResult(ToolCall("tu_1", "wait", {"days": 3}), "ok")])
    p.generate()

    first, second = captured
    assert first["output_config"] == {"effort": "medium"} and "thinking" not in first and "temperature" not in first
    assert first["tools"][0]["input_schema"]["required"] == ["days"]
    assert first["messages"][0]["content"][0]["type"] == "image"
    # The reply's blocks (thinking included) are replayed untouched, and results reference the tool_use id.
    assert second["messages"][1]["content"][0].type == "thinking"
    assert second["messages"][2]["content"] == [{"type": "tool_result", "tool_use_id": "tu_1", "content": "ok", "is_error": False}]
