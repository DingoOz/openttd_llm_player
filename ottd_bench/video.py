"""Film an episode.

The video has three kinds of shot per turn:

* what the model was shown (its input images, coordinate labels included)
* what it said and did, over the game view after its actions were applied
* a timelapse of the game while time passes

The model's thinking time is not in the video: the game is paused then, so there is nothing to see.
Frames come from the game's own renderer (whole-map screenshots, cropped), not a screen capture.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw

from .bridge import BridgeError
from .vision import _font

if TYPE_CHECKING:
    from .episode import Episode
    from .providers import ToolResult, Turn

W, H = 1280, 720
BAR = 46
GREEN, RED, YELLOW, GREY, WHITE = (90, 220, 110), (255, 95, 85), (255, 225, 60), (175, 175, 175), (255, 255, 255)


def _money(v: int) -> str:
    return f"-£{-v:,}" if v < 0 else f"£{v:,}"


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    """Word-wrap to `width` pixels; text that needs more than `max_lines` ends in an ellipsis."""
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if not line or draw.textlength(trial, font=font) <= width:
            line = trial
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        last = lines[max_lines - 1]
        while last and draw.textlength(last + " ...", font=font) > width:
            last = last.rsplit(" ", 1)[0] if " " in last else ""
        lines = lines[:max_lines - 1] + [(last + " ...").strip()]
    return lines or [""]


def _dim(img: Image.Image, box: tuple[int, int, int, int], strength: float = 0.72) -> None:
    """Darken a rectangle so text on top of the game view stays readable."""
    region = img.crop(box)
    img.paste(Image.blend(region, Image.new("RGB", region.size, (0, 0, 0)), strength), box)


class Recorder:
    def __init__(self, ep: "Episode", timelapse_fps: int = 16, capture_interval: float = 0.4):
        self.ep = ep
        self.frames_dir = ep.run_dir / "video_frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.timelapse_fps = timelapse_fps
        self.capture_interval = capture_interval
        self.shots: list[tuple[Path, float]] = []          # (frame file, seconds on screen)
        self.status: dict = {}
        self.owned: list[tuple[int, int]] = []             # tiles of the company's stations and depots
        self.attention: list[tuple[int, int, bool]] = []   # tiles the model just acted on, and whether it worked
        self.cam: tuple[float, float, float] | None = None # centre x, centre y (world pixels), scale
        self.last_action = ""

    # ------------------------------------------------------------- plumbing

    def _add(self, img: Image.Image, seconds: float) -> None:
        path = self.frames_dir / f"{len(self.shots):06d}.jpg"
        img.save(path, "JPEG", quality=90)
        self.shots.append((path, seconds))

    def _world(self) -> Image.Image:
        path = self.ep.server.screenshot("giant", "film")
        with Image.open(path) as im:
            im.load()
            world = im.copy()
        path.unlink()
        return world

    def _aim(self, caption_height: int = 0) -> None:
        """Frame everything the company owns plus whatever the model just touched, in the part of
        the picture not covered by the top bar or a caption box of `caption_height` pixels."""
        geo = self.ep.geo
        tiles = self.owned + [(x, y) for x, y, _ in self.attention]
        if not tiles:
            tiles = [self.ep.camera[:2]]
        pts = [geo.world_px(x, y) for x, y in tiles]
        margin = 230
        x0, x1 = min(p[0] for p in pts) - margin, max(p[0] for p in pts) + margin
        y0, y1 = min(p[1] for p in pts) - margin, max(p[1] for p in pts) + margin
        scale = max(0.3, min(1.0, W / (x1 - x0), (H - BAR - caption_height) / (y1 - y0)))
        self.cam = ((x0 + x1) / 2, (y0 + y1) / 2 - (BAR - caption_height) / (2 * scale), scale)

    def _view(self, world: Image.Image) -> Image.Image:
        if self.cam is None:
            self._aim()
        cx, cy, scale = self.cam
        box = (round(cx - W / (2 * scale)), round(cy - H / (2 * scale)),
               round(cx + W / (2 * scale)), round(cy + H / (2 * scale)))
        view = world.crop(box).convert("RGB")
        return view.resize((W, H), Image.LANCZOS) if view.size != (W, H) else view

    def _to_screen(self, x: int, y: int) -> tuple[float, float]:
        cx, cy, scale = self.cam
        wx, wy = self.ep.geo.world_px(x, y)
        return (wx - cx) * scale + W / 2, (wy - cy) * scale + H / 2

    def _hud(self, img: Image.Image, date: str) -> ImageDraw.ImageDraw:
        _dim(img, (0, 0, W, BAR), 0.8)
        draw = ImageDraw.Draw(img)
        ep = self.ep
        draw.text((14, BAR / 2), f"{ep.model.model} plays OpenTTD", font=_font(17), fill=WHITE, anchor="lm")
        turn = f"turn {ep.step}/{ep.scenario.episode.max_steps}" if ep.step else ""
        draw.text((W / 2, BAR / 2), f"{date}    {turn}", font=_font(20), fill=YELLOW, anchor="mm")
        s = self.status
        if s:
            worth = s["net_worth"]
            right = f"cash {_money(s['money'])}   vehicles {s['vehicles']}   net worth "
            wx = W - 14 - draw.textlength(_money(worth), font=_font(17))
            draw.text((wx, BAR / 2), _money(worth), font=_font(17), fill=GREEN if worth >= 0 else RED, anchor="lm")
            draw.text((wx, BAR / 2), right, font=_font(17), fill=WHITE, anchor="rm")
        return draw

    def _refresh_owned(self) -> None:
        res = self.ep.bridge.call("stations")
        self.owned = [(s["x"], s["y"]) for s in res["stations"]] + [(d["x"], d["y"]) for d in res["depots"]]

    # ---------------------------------------------------------------- shots

    def title(self) -> None:
        ep = self.ep
        img = Image.new("RGB", (W, H), (12, 18, 28))
        draw = ImageDraw.Draw(img)
        heading = "OpenTTD, scripted reference player" if ep.model.provider == "scripted" else "OpenTTD, played by a vision LLM"
        draw.text((W / 2, 200), heading, font=_font(44), fill=WHITE, anchor="mm")
        draw.text((W / 2, 262), f"model: {ep.model.model}     scenario: {ep.scenario.name}", font=_font(24), fill=YELLOW, anchor="mm")
        y = 330
        for line in _wrap(draw, "Goal given to the model: " + " ".join(ep.scenario.goal.split()), _font(20), W - 260, 4):
            draw.text((W / 2, y), line, font=_font(20), fill=(215, 215, 215), anchor="mm")
            y += 30
        if ep.model.provider == "scripted":
            notes = ["This run is the built-in scripted player, not a model: a fixed policy using the same tools.",
                     "Each turn: what a model would be shown, then what the script does, then time passing."]
        else:
            notes = ["The model sees screenshots and acts through tool calls. Nothing here is scripted.",
                     "The game is paused while the model thinks; that waiting is cut from this video.",
                     "Each turn: what the model is shown, then what it says and does, then time passing."]
        y += 34
        for line in notes:
            draw.text((W / 2, y), line, font=_font(17), fill=GREY, anchor="mm")
            y += 27
        self._add(img, 6.0)

    def show_inputs(self, images: list[tuple[Path, str]]) -> None:
        """The images handed to the model this turn, exactly as it gets them."""
        img = Image.new("RGB", (W, H), (0, 0, 0))
        cell_w, cell_h = W // len(images), H - BAR
        for i, (path, _) in enumerate(images):
            with Image.open(path) as im:
                pic = im.convert("RGB")
            fit = min((cell_w - 8) / pic.width, (cell_h - 8) / pic.height)   # fill the cell, enlarging small inputs
            pic = pic.resize((round(pic.width * fit), round(pic.height * fit)), Image.LANCZOS)
            img.paste(pic, (i * cell_w + (cell_w - pic.width) // 2, BAR + (cell_h - pic.height) // 2))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, W, BAR), fill=(40, 40, 70))
        draw.text((14, BAR / 2), f"Turn {self.ep.step}: what the model is shown", font=_font(20), fill=WHITE, anchor="lm")
        caption = "   |   ".join(c.split(" (where")[0] for _, c in images)
        draw.text((W - 14, BAR / 2), _wrap(draw, caption, _font(14), 700, 1)[0], font=_font(14), fill=GREY, anchor="rm")
        self._add(img, 2.4)

    def show_actions(self, turn: "Turn", results: list["ToolResult"]) -> None:
        """What the model said and did, over the game as it now stands."""
        self.attention = []
        for r in results:
            a = r.call.args
            for kx, ky in (("x", "y"), ("x1", "y1"), ("x2", "y2"), ("depot_x", "depot_y")):
                if isinstance(a.get(kx), int) and isinstance(a.get(ky), int):
                    if 0 <= a[kx] < self.ep.geo.size_x and 0 <= a[ky] < self.ep.geo.size_y:
                        self.attention.append((a[kx], a[ky], r.ok))
        self._refresh_owned()
        self.status = self.ep.bridge.call("company")

        # Lay the caption out first: the camera is aimed at whatever space it leaves.
        draw = ImageDraw.Draw(Image.new("RGB", (8, 8)))
        body, small = _font(18), _font(16)
        pad, width = 18, W - 36
        said = " ".join(turn.text.replace("*", "").replace("#", "").split())
        lines: list[tuple[str, tuple, object, tuple | None]] = []   # text, colour, font, bullet colour
        lines.append((f"Turn {self.ep.step}: the model says", GREY, small, None))
        for line in _wrap(draw, said or "(no commentary)", body, width, 3):
            lines.append((line, WHITE, body, None))
        if not results:
            lines.append(("No tool calls: nothing happens in the game this turn.", RED, body, None))
        for r in results[:5]:
            args = ", ".join(f"{k}={v}" for k, v in r.call.args.items() if k != "content")
            text = f"{r.call.name}({args})  ->  {' '.join(r.text.split())}"
            for j, line in enumerate(_wrap(draw, text, small, width - 26, 2)):
                lines.append((line, WHITE if r.ok else (255, 200, 195), small, (GREEN if r.ok else RED) if j == 0 else (0, 0, 0, 0)))
        if len(results) > 5:
            lines.append((f"... and {len(results) - 5} more calls", GREY, small, None))

        height = sum(26 if f is body else 23 for _, _, f, _ in lines) + 2 * pad - 6
        top = H - height
        self._aim(caption_height=height)
        img = self._view(self._world())
        draw = ImageDraw.Draw(img)
        _, _, scale = self.cam
        for x, y, ok in self.attention:
            sx, sy = self._to_screen(x, y)
            hw, hh = 32 * scale, 16 * scale
            draw.polygon([(sx, sy - hh), (sx + hw, sy), (sx, sy + hh), (sx - hw, sy)], outline=GREEN if ok else RED, width=3)
        _dim(img, (0, top, W, H))
        draw = ImageDraw.Draw(img)
        y = top + pad
        for text, colour, font, bullet in lines:
            x = pad
            if bullet is not None:
                if len(bullet) == 3:
                    draw.ellipse((pad + 2, y + 4, pad + 14, y + 16), fill=bullet)
                x = pad + 26
            draw.text((x, y), text, font=font, fill=colour)
            y += 26 if font is body else 23
        self._hud(img, self.ep.date)

        passive = ("inspect_tile", "get_info", "read_file", "list_files", "write_notes", "look", "view_map", "wait")
        acted = [r for r in results if r.call.name not in passive]
        if acted:   # otherwise keep showing the last thing that changed the game
            self.last_action = f"last action: {acted[-1].call.name} ({'ok' if acted[-1].ok else 'failed'})"
        words = len(said.split()) + sum(len(r.text.split()) for r in results[:5])
        self._add(img, max(3.5, min(9.0, 2.0 + words / 14)))   # long enough to read

    def _live_frame(self, date: str) -> None:
        img = self._view(self._world())
        if self.last_action:
            draw = ImageDraw.Draw(img)
            w = int(draw.textlength(self.last_action, font=_font(15))) + 24
            _dim(img, (0, H - 30, w, H), 0.65)
            ImageDraw.Draw(img).text((12, H - 15), self.last_action, font=_font(15), fill=GREY, anchor="lm")
        self._hud(img, date)
        self._add(img, 1 / self.timelapse_fps)

    def film_advance(self, days: int) -> dict:
        """Run the clock for `days`, grabbing frames while the game is live. Returns the advance reply."""
        from .episode import _fmt_date

        ep = self.ep
        self.attention = []
        self._aim()
        rid = ep.bridge.send("advance", days=int(days))
        deadline = time.monotonic() + ep.bridge.advance_timeout(days) * 3   # screenshots slow the game down
        next_frame, frames = time.monotonic(), 0
        while True:
            reply = ep.bridge.poll(rid)
            if reply is not None:
                break
            now = time.monotonic()
            if now > deadline:
                raise BridgeError("game script did not finish advancing time while filming")
            if now < next_frame:
                time.sleep(0.03)
                continue
            next_frame = now + self.capture_interval
            if frames % 5 == 4:
                status = ep.bridge.call("company")   # allowed while the clock runs; keeps the cash figure live
                if status.get("exists"):
                    self.status = status
            date = _fmt_date(ep.server.admin.date_days) if ep.server.admin.date_days else ep.date
            self._live_frame(date)
            frames += 1
        if reply.get("ok"):
            self._live_frame(reply["date"])
        return reply

    def end_card(self, metrics_like: dict) -> None:
        try:
            self._refresh_owned()
            self.attention = []
            self._aim()
            img = self._view(self._world())
        except Exception:
            img = Image.new("RGB", (W, H), (12, 18, 28))
        _dim(img, (190, 150, W - 190, H - 150), 0.82)
        draw = ImageDraw.Draw(img)
        s = metrics_like
        draw.text((W / 2, 200), "End of episode", font=_font(38), fill=WHITE, anchor="mm")
        if s.get("exists"):
            worth = s["net_worth"]
            draw.text((W / 2, 280), f"net worth {_money(worth)}", font=_font(46), fill=GREEN if worth >= 0 else RED, anchor="mm")
            rows = [f"cash {_money(s['money'])}     loan {_money(s['loan'])}     vehicles {s['vehicles']}     stations {s['stations']}",
                    f"{self.ep.counters['turns']} turns     {self.ep.counters['tool_calls']} tool calls     "
                    f"{self.ep.counters['tool_errors']} failed     {self.ep.counters['images_sent']} images shown to the model",
                    "score = cash - loan + resale value of vehicles; it starts at 0"]
        else:
            draw.text((W / 2, 280), "the company no longer exists", font=_font(40), fill=RED, anchor="mm")
            rows = []
        y = 360
        for i, row in enumerate(rows):
            draw.text((W / 2, y), row, font=_font(20 if i < 2 else 16), fill=WHITE if i < 2 else GREY, anchor="mm")
            y += 44
        draw.text((W / 2, H - 185), f"{self.ep.model.model}  ·  {self.ep.scenario.name}  ·  ended {self.ep.date}", font=_font(17), fill=YELLOW, anchor="mm")
        self._add(img, 7.0)

    # --------------------------------------------------------------- encode

    def finish(self, keep_frames: bool = False) -> Path | None:
        if not self.shots:
            return None
        listing = self.frames_dir / "shots.txt"
        with open(listing, "w") as f:
            for path, seconds in self.shots:
                f.write(f"file '{path.name}'\nduration {seconds:.4f}\n")
            f.write(f"file '{self.shots[-1][0].name}'\n")   # the concat demuxer ignores the final duration otherwise
        out = self.ep.run_dir / "video.mp4"
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing),
               "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-crf", "21", "-preset", "medium",
               "-movflags", "+faststart", str(out)]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, cwd=self.frames_dir, timeout=1800)
        except (OSError, subprocess.TimeoutExpired) as e:
            self.ep.log(f"!! could not encode the video ({e}); frames are in {self.frames_dir}")
            return None
        if done.returncode != 0 or not out.exists():
            self.ep.log(f"!! ffmpeg failed: {done.stderr.strip()[:300]}; frames are in {self.frames_dir}")
            return None
        if not keep_frames:
            shutil.rmtree(self.frames_dir, ignore_errors=True)
        return out
