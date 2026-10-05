"""One benchmark episode: a model plays a scenario from start to end date.

Time model: the game is paused whenever the model is thinking or acting. Every model turn is
followed by a fixed number of game days, and the model can ask for more with wait(). A slow
model and a fast model therefore get exactly the same game.
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import time
from dataclasses import asdict
from datetime import date as _date
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw

from . import __version__
from .bridge import BridgeError
from .config import ModelConfig, Scenario
from .providers import Provider, ProviderError, ToolResult, Turn, make_provider
from .server import OpenTTDServer
from .tools import GameTools, format_industries, format_towns
from .video import Recorder
from .vision import MapGeometry, _font, render_overview, render_view, save_for_model

GUIDE = Path(__file__).with_name("guide.md")

SYSTEM_PROMPT = """\
You are playing OpenTTD, a transport business simulation, as the manager of the company "LLM Transport".

# Goal
{goal}
Your score is your net worth when the episode ends: cash plus the resale value of your vehicles, minus your loan. Roads and stations count for nothing by themselves; they pay off only through the income they bring.

# How turns work
- The game is paused while you think. Each reply you send is one turn, and you have {max_steps} turns.
- In a turn you may call up to {max_calls} tools. They run in the order you give them, and you see the results at the start of your next turn.
- Only tool calls change anything. A reply that describes what you intend to do, without the tool calls, does nothing and still uses up the turn. Make the calls in the same reply; to deliberately do nothing, call wait.
- After every turn the game runs for {days_per_step} days. Call wait(days) to let more time pass when there is nothing useful to do.
- The episode ends on {end_date}, or when your turns run out.

# Seeing the map
- Every turn includes at least one image. look(x, y, zoom) returns the real game view around a tile; view_map() returns a top-down map of the whole world.
- Tiles are integer (x, y) pairs from (0,0) to ({max_x},{max_y}). The images carry coordinate labels: in look images each white label "x,y" sits on the tile it names; in view_map the grid lines are numbered along the top (x) and left (y) edges.
- Terrain, roads and free land are only visible in the images. Use them to choose where to build, and use inspect_tile(x, y) to confirm what a single tile is.

# How to earn money
Vehicles are paid when they deliver cargo. Passengers travel between towns; raw materials go from the industry that produces them to an industry that accepts them. Longer trips and bigger towns pay more.
A working bus line needs, in this order:
1. A bus stop in each of two towns: build_road_stop on a straight piece of road near the town centre.
2. A road between the two stops: build_road(x1, y1, x2, y2) finds a route and builds whatever is missing.
3. A depot: build_road_depot on empty land directly beside a road that is part of the route.
4. Buses: get_info("bus_and_truck_models") lists what you can buy; buy_vehicle with station_ids gives a bus its route and starts it.
One vehicle rarely pays for a route. Add more with clone_vehicle when stops have cargo waiting.

# Money
You start with a loan. Building and buying cost cash; set_loan borrows more, up to the limit, or repays. Interest and vehicle running costs are charged all the time, so borrowed cash that sits idle loses money. A company whose cash stays negative goes bankrupt and the episode ends.

# Files
You have a workspace of text files (list_files, read_file). guide.md is a reference manual for the game. notes.md is your own memory: write_notes replaces its content, and it is shown to you again whenever your earlier conversation is no longer available, so keep your plan, station ids and useful coordinates there.
"""


def _money(v: int) -> str:
    return f"-£{-v:,}" if v < 0 else f"£{v:,}"


def _fmt_date(days: int) -> str:
    """OpenTTD counts days from 1 January of year 0 (a leap year); Python's ordinal 1 is 0001-01-01."""
    return _date.fromordinal(days - 365).isoformat()


class Episode:
    def __init__(self, scenario: Scenario, model: ModelConfig, run_dir: Path, *,
                 provider: Provider | None = None, skip_probe: bool = False, record: bool = False,
                 log: Callable[[str], None] | None = None):
        self.scenario = scenario
        self.model = model
        self.run_dir = run_dir = Path(run_dir).resolve()
        self.log = log or (lambda line: print(line, flush=True))   # flush: runs are often piped or backgrounded
        self.skip_probe = skip_probe
        self.record = record
        self.recorder: Recorder | None = None
        self.video: Path | None = None
        self.workspace = run_dir / "workspace"
        self.images_dir = run_dir / "images"
        self.provider = provider or make_provider(model)

        self.server: OpenTTDServer | None = None
        self.bridge = None
        self.geo: MapGeometry | None = None
        self.towns: list[dict] = []
        self.industries: list[dict] = []
        self.tools: GameTools | None = None

        self.step = 0
        self.start_days = 0
        self.days = 0
        self.date = ""
        self.camera = (0, 0, "medium")
        self.world_dirty = True
        self._world: Image.Image | None = None
        self._images_this_step = 0
        self._pending_events: list[str] = []
        self._transcript = None
        self._timeline = None
        self.counters = dict(turns=0, tool_calls=0, tool_errors=0, invalid_calls=0, idle_turns=0, nudges=0, images_sent=0,
                             look_calls=0, view_map_calls=0, input_tokens=0, output_tokens=0, cached_tokens=0,
                             llm_seconds=0.0, segments=0)
        self.probes: dict = {}

    # ------------------------------------------------------------------ run

    def run(self) -> dict:
        t_start = time.time()
        for d in (self.run_dir, self.workspace, self.images_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._transcript = open(self.run_dir / "transcript.jsonl", "w")
        outcome = "completed"
        error = None
        status: dict = {}
        try:
            if not self.skip_probe:
                self._probe_number()
            self._start_game()
            if not self.skip_probe:
                self._probe_game_view()
            outcome = self._play()
        except (ProviderError, BridgeError, RuntimeError) as e:
            outcome, error = "aborted", f"{type(e).__name__}: {e}"
            self.log(f"!! aborted: {error}")
        finally:
            try:
                status = self._final_status()
            except Exception as e:   # the server may already be gone; keep whatever we have
                self.log(f"!! could not read final state: {e}")
            if self.recorder is not None and status:
                try:
                    self.recorder.end_card(status)
                except Exception as e:   # a video problem must never cost the run its results
                    self.log(f"!! could not film the end card: {e}")
            if self.server is not None:
                try:
                    if status:
                        shutil.copy(self.server.save("final"), self.run_dir / "final.sav")
                except Exception as e:
                    self.log(f"!! could not save the final game: {e}")
                self.server.stop()
            if self._timeline is not None:
                self._timeline.close()
            if self.recorder is not None:
                self.log("encoding video ...")
                self.video = self.recorder.finish()
        metrics = self._metrics(status, outcome, error, time.time() - t_start)
        (self.run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
        self._record({"t": "end", "outcome": outcome, "error": error})
        self._transcript.close()
        self._write_summary(metrics)
        return metrics

    # ---------------------------------------------------------------- setup

    def _start_game(self) -> None:
        self.server = OpenTTDServer(self.run_dir / "ottd", self.scenario)
        self.log(f"starting OpenTTD (seed {self.scenario.game.seed}, {self.scenario.game.map_size}x{self.scenario.game.map_size}) ...")
        self.bridge = self.server.start()
        info = self.server.info
        self.start_days = self.days = info["days"]
        self.date = info["date"]
        heights = self.bridge.call("heights", timeout=120)["rows"]
        self.geo = MapGeometry(info["map_x"], info["map_y"], heights, bool(info["freeform_edges"]))
        self.towns = self.bridge.call("towns")["towns"]
        self.industries = self.bridge.call("industries")["industries"]
        if not self.towns:
            raise RuntimeError("the generated map has no towns; change the scenario's seed or town setting")
        biggest = max(self.towns, key=lambda t: t["population"])
        self.camera = (biggest["x"], biggest["y"], "medium")
        self.tools = GameTools(self)

        shutil.copy(GUIDE, self.workspace / "guide.md")
        (self.workspace / "towns.txt").write_text(format_towns(self.towns) + "\n")
        (self.workspace / "industries.txt").write_text(format_industries(self.industries) + "\n")
        (self.workspace / "notes.md").write_text("")
        (self.workspace / "action_log.txt").write_text("")

        (self.run_dir / "run.json").write_text(json.dumps({
            "harness_version": __version__,
            "openttd": self.server.admin.welcome.get("revision"),
            "scenario": asdict(self.scenario),
            "model": {k: v for k, v in asdict(self.model).items()},
            "map": {"x": info["map_x"], "y": info["map_y"], "towns": len(self.towns), "industries": len(self.industries)},
            "start_date": self.date,
        }, indent=2))
        self._timeline = open(self.run_dir / "timeline.csv", "w", newline="")
        csv.writer(self._timeline).writerow(["step", "date", "day", "money", "loan", "net_worth", "vehicles", "stations",
                                             "tool_calls", "tool_errors", "input_tokens", "output_tokens", "llm_seconds"])
        self.log(f"game ready: {len(self.towns)} towns, {len(self.industries)} industries, start {self.date}")
        if self.record:
            self.recorder = Recorder(self)
            self.recorder.title()

    # --------------------------------------------------------------- probes

    def _ask(self, question: str, image: Path) -> str:
        self.provider.begin("You are a careful visual assistant. Answer briefly and exactly.", [])
        self.provider.add_user(question, [image])
        turn = self.provider.generate()
        self._count(turn)
        self.counters["images_sent"] += 1
        return turn.text.strip()

    def _probe_number(self) -> None:
        """Refuse to run a model that cannot actually see: every model here must use vision."""
        rng = random.Random(self.scenario.game.seed)
        number = str(rng.randint(1000, 9999))
        img = Image.new("RGB", (512, 256), (30, 60, 30))
        draw = ImageDraw.Draw(img)
        draw.rectangle((30, 40, 150, 200), fill=(200, 60, 60))
        draw.ellipse((360, 50, 490, 180), fill=(60, 90, 200))
        draw.text((256, 128), number, font=_font(84), fill=(255, 255, 255), anchor="mm", stroke_width=3, stroke_fill=(0, 0, 0))
        path = save_for_model(img, self.images_dir / "probe_number.jpg")
        self.log(f"vision check ({self.model.key}) ...")
        answer = self._ask("What four-digit number is written in this image? Reply with the number only.", path)
        ok = number in "".join(ch for ch in answer if ch.isdigit())
        self.probes["reads_number"] = ok
        self._record({"t": "probe", "name": "reads_number", "expected": number, "answer": answer[:200], "ok": ok})
        if not ok:
            raise ProviderError(
                f"model '{self.model.key}' failed the vision check (expected {number}, answered {answer[:80]!r}). "
                "This benchmark requires a vision-capable model with image input enabled.")
        self.log("  vision check passed")

    def _probe_game_view(self) -> None:
        """Can the model read real game graphics? Recorded as a metric, not a gate."""
        town = max(self.towns, key=lambda t: t["population"])
        camera = self.camera
        path = self.render_look(town["x"], town["y"], "close", name="probe_town")
        self.camera = camera   # the probe must not change what the model sees on its first turn
        answer = self._ask("This is a screenshot of the game OpenTTD. A town's name is written on the map next to its "
                           "population in brackets. What is the town's name? Reply with the name only.", path)
        # The game's pixel font invites one-letter misreads ("Brøndingville"); score closeness, not equality.
        want = town["name"].lower()
        words = answer.lower().replace(",", " ").split()
        spans = [" ".join(words[i:i + n]) for n in range(1, 4) for i in range(len(words) - n + 1)] or [""]
        similarity = max(SequenceMatcher(None, want, span).ratio() for span in spans)
        ok = similarity >= 0.8
        self.probes["reads_town_sign"] = ok
        self.probes["town_sign_similarity"] = round(similarity, 2)
        self._record({"t": "probe", "name": "reads_town_sign", "expected": town["name"], "answer": answer[:200],
                      "similarity": round(similarity, 2), "ok": ok})
        self.log(f"  game-view check: {'passed' if ok else 'FAILED'} (expected {town['name']}, got {answer[:60]!r})")

    # ----------------------------------------------------------------- play

    def _play(self) -> str:
        ep = self.scenario.episode
        end_date = _fmt_date(self.start_days + ep.max_days)
        system = SYSTEM_PROMPT.format(
            goal=self.scenario.goal or "Build a profitable transport company.",
            max_steps=ep.max_steps, max_calls=ep.max_tool_calls_per_step, days_per_step=ep.days_per_step,
            end_date=end_date, max_x=self.geo.size_x - 1, max_y=self.geo.size_y - 1)
        self._record({"t": "system", "text": system, "tools": [s.name for s in self.tools.specs()]})

        new_segment = True
        seg_chars = seg_images = 0
        carried_images: list[tuple[Path, str]] = []
        last_note = ""
        budget = int(self.model.context_tokens * 0.8)

        for self.step in range(1, ep.max_steps + 1):
            status = self.bridge.call("company")
            if not status.get("exists"):
                self.log("company went bankrupt")
                return "bankrupt"
            if self.days_left() <= 0:
                return "completed"

            if new_segment:
                self.provider.begin(system, self.tools.specs())
                self.counters["segments"] += 1
            self._images_this_step = 0
            images = carried_images or [(self.render_look(*self.camera, name=f"step{self.step:03d}_0_auto"),
                                         f"game view centred on tile ({self.camera[0]},{self.camera[1]}), zoom {self.camera[2]} (where you last looked)")]
            text = self._observation(status, images, new_segment, last_note, end_date)
            self.provider.add_user(text, [p for p, _ in images])
            self.counters["images_sent"] += len(images)
            self._record({"t": "observation", "step": self.step, "text": text, "images": [str(p.relative_to(self.run_dir)) for p, _ in images]})
            self._pending_events = []
            new_segment = False
            if self.recorder is not None:
                self.recorder.status = status
                self.recorder.show_inputs(images)

            turn = self.provider.generate()
            self._count(turn)
            self.counters["turns"] += 1
            nudged = False
            if not turn.tool_calls and turn.stop != "refusal" and ep.nudge_idle_turns:
                # Small models often announce a plan and stop. One reminder, inside the same turn.
                nudged = True
                self.counters["nudges"] += 1
                self.provider.add_user(
                    "You have not called any tool, so nothing has happened in the game yet. Make your tool calls now, "
                    "in this reply, without repeating your plan. If you really want to do nothing this turn, call wait.")
                retry = self.provider.generate()
                self._count(retry)
                turn = Turn(text=f"{turn.text}\n{retry.text}".strip(), tool_calls=retry.tool_calls, stop=retry.stop,
                            input_tokens=retry.input_tokens, output_tokens=turn.output_tokens + retry.output_tokens,
                            cached_tokens=retry.cached_tokens, seconds=turn.seconds + retry.seconds)
            self._record({"t": "turn", "step": self.step, "text": turn.text, "stop": turn.stop, "nudged": nudged,
                          "tool_calls": [{"name": c.name, "args": c.args, "error": c.error} for c in turn.tool_calls],
                          "input_tokens": turn.input_tokens, "output_tokens": turn.output_tokens, "seconds": round(turn.seconds, 1)})

            carried_images = []
            results: list[ToolResult] = []
            errors = 0
            self._images_this_step = 0
            for i, call in enumerate(turn.tool_calls):
                if i >= ep.max_tool_calls_per_step:
                    results.append(ToolResult(call, f"skipped: only {ep.max_tool_calls_per_step} tool calls run per turn", ok=False))
                    continue
                outcome = self.tools.run(call)
                results.append(ToolResult(call, outcome.text, outcome.ok))
                carried_images.extend(outcome.images)
                self.counters["tool_calls"] += 1
                if call.name in ("look", "view_map") and outcome.ok:
                    self.counters[f"{call.name}_calls"] += 1
                if not outcome.ok:
                    errors += 1
                    self.counters["tool_errors"] += 1
                    if call.error or outcome.text.startswith(("error: unknown tool", "error: missing required", "error: argument")):
                        self.counters["invalid_calls"] += 1
                self._log_action(call, outcome.text, outcome.ok)
            if results:
                self.provider.add_tool_results(results)
            else:
                self.counters["idle_turns"] += 1
            if self.recorder is not None:
                self.recorder.show_actions(turn, results)

            if turn.stop == "refusal":
                last_note = "Your previous reply was refused by the model provider's safety system; nothing was done."
            elif turn.stop == "length" and not turn.tool_calls:
                last_note = "Your previous reply hit the output limit before any tool call; nothing was done. Be brief and call tools."
            elif not turn.tool_calls:
                last_note = "You called no tools last turn, so nothing happened in the game. Act by calling tools."
            else:
                last_note = ""

            self.log(f"[{self.step:>3}/{ep.max_steps}] {self.date}  cash {_money(status['money']):>10}  worth {_money(status['net_worth']):>10}  "
                     f"veh {status['vehicles']:>2}  | {len(turn.tool_calls)} calls, {errors} errors  | "
                     f"{turn.input_tokens}+{turn.output_tokens} tok {turn.seconds:.0f}s  | "
                     + (", ".join(c.name for c in turn.tool_calls) or "(no action)"))
            csv.writer(self._timeline).writerow([self.step, self.date, self.days - self.start_days, status["money"], status["loan"],
                                                 status["net_worth"], status["vehicles"], status["stations"], len(turn.tool_calls), errors,
                                                 turn.input_tokens, turn.output_tokens, round(turn.seconds, 1)])
            self._timeline.flush()

            if self.days_left() > 0:
                self._pending_events.extend(self.advance(min(ep.days_per_step, self.days_left())))
            # Providers that report no usage fall back to a rough size estimate.
            seg_chars += len(text) + len(turn.text) + sum(len(r.text) for r in results)
            seg_images += len(images)
            used = turn.input_tokens + turn.output_tokens or seg_chars // 3 + seg_images * 1200
            if used > budget:
                new_segment = True
                seg_chars = seg_images = 0

        if self.days_left() > 0 and ep.run_out_clock:
            # Out of turns: let the clock run so every episode is scored on the same game date.
            self.log(f"turns used up; running the remaining {self.days_left()} game days ...")
            while self.days_left() > 0:
                self.advance(min(30, self.days_left()))
                if not self.bridge.call("company").get("exists"):
                    return "bankrupt"
        return "completed" if self.days_left() <= 0 else "out_of_turns"

    def _observation(self, status: dict, images: list[tuple[Path, str]], new_segment: bool, note: str, end_date: str) -> str:
        ep = self.scenario.episode
        lines = [f"== Turn {self.step} of {ep.max_steps} | date {self.date} | {self.days_left()} days until the episode ends ({end_date}) =="]
        lines.append(self.company_text(status))
        if self._pending_events:
            lines.append("Since your last turn: " + "; ".join(self._pending_events))
        if note:
            lines.append(note)
        for i, (_, caption) in enumerate(images, 1):
            lines.append(f"Image {i}: {caption}")
        if new_segment:
            lines.append("")
            if self.step > 1:
                lines.append("Your earlier conversation is no longer available. This is what carries over:")
            notes = (self.workspace / "notes.md").read_text().strip()
            lines.append("notes.md:\n" + (notes[:3000] if notes else "(empty)"))
            recent = (self.workspace / "action_log.txt").read_text().strip().splitlines()[-15:]
            if recent:
                lines.append("Your most recent actions (full list in action_log.txt):\n" + "\n".join(recent))
            lines.append("Largest towns (all towns and industries are in towns.txt and industries.txt):")
            for t in sorted(self.towns, key=lambda t: -t["population"])[:8]:
                lines.append(f"  {t['name']}: centre ({t['x']},{t['y']}), population {t['population']}")
        return "\n".join(lines)

    # ------------------------------------------------- used by the tool layer

    def company_text(self, s: dict) -> str:
        return (f"Cash {_money(s['money'])} | loan {_money(s['loan'])} of max {_money(s['max_loan'])} | net worth (score) {_money(s['net_worth'])} | "
                f"vehicles {s['vehicles']} | stations {s['stations']} | this quarter: income {_money(s['income_this_quarter'])}, "
                f"costs {_money(-s['expenses_this_quarter'])}, cargo delivered {s['delivered_this_quarter']} | "
                f"last quarter: income {_money(s['income_last_quarter'])}, costs {_money(-s['expenses_last_quarter'])}")

    def days_left(self) -> int:
        return self.start_days + self.scenario.episode.max_days - self.days

    def advance(self, days: int) -> list[str]:
        res = self.recorder.film_advance(days) if self.recorder is not None else self.bridge.advance(days)
        if not res.get("ok"):
            raise BridgeError(f"could not advance time: {res.get('error')}")
        self.days, self.date = res["days"], res["date"]
        self.world_dirty = True
        events = [self._event_text(e) for e in res["events"]]
        self._record({"t": "advance", "step": self.step, "days": days, "date": self.date, "events": events})
        return events

    @staticmethod
    def _event_text(e: dict) -> str:
        kind = e["type"]
        if kind == "first_vehicle_arrived":
            return f"{e['date']}: vehicle {e['vehicle_id']} reached station {e['station_id']} for the first time"
        if kind == "vehicle_crashed":
            return f"{e['date']}: vehicle {e['vehicle_id']} CRASHED at ({e['x']},{e['y']})"
        if kind == "company_in_trouble":
            return f"{e['date']}: WARNING your cash is negative; the company goes bankrupt if this continues"
        return f"{e['date']}: {kind.replace('_', ' ')}" + (f" (industry {e['industry_id']})" if "industry_id" in e else "")

    def image_budget_left(self) -> bool:
        return self._images_this_step < self.scenario.episode.max_images_per_step

    def _world_image(self) -> Image.Image:
        if self.world_dirty or self._world is None:
            path = self.server.screenshot("giant", "world")
            with Image.open(path) as im:
                im.load()
                self._world = im.copy()
            path.unlink()   # whole-map screenshots are large; only the crops are kept
            self.world_dirty = False
        return self._world

    def render_look(self, x: int, y: int, zoom: str, name: str | None = None) -> Path:
        owned = self.bridge.call("stations")
        marks = [(s["x"], s["y"], f"station {s['id']}") for s in owned["stations"]]
        marks += [(d["x"], d["y"], "depot") for d in owned["depots"] if d["kind"] == "road_depot"]
        img = render_view(self._world_image(), self.geo, x, y, zoom, marks=marks)
        self.camera = (x, y, zoom)
        self._images_this_step += 1
        name = name or f"step{self.step:03d}_{self._images_this_step}_look_{x}_{y}_{zoom}"
        return save_for_model(img, self.images_dir / f"{name}.jpg", self.model.image_max_side)

    def render_overview(self) -> Path:
        path = self.server.screenshot("minimap", "minimap")
        with Image.open(path) as im:
            minimap = im.convert("RGB")
        stations = self.bridge.call("stations")["stations"]
        img = render_overview(minimap, self.geo, self.towns, self.industries, stations)
        self._images_this_step += 1
        return save_for_model(img, self.images_dir / f"step{self.step:03d}_{self._images_this_step}_map.jpg", self.model.image_max_side)

    # -------------------------------------------------------------- records

    def _count(self, turn: Turn) -> None:
        c = self.counters
        c["input_tokens"] += turn.input_tokens
        c["output_tokens"] += turn.output_tokens
        c["cached_tokens"] += turn.cached_tokens
        c["llm_seconds"] += turn.seconds

    def _record(self, obj: dict) -> None:
        self._transcript.write(json.dumps(obj) + "\n")
        self._transcript.flush()

    def _log_action(self, call, text: str, ok: bool) -> None:
        args = ", ".join(f"{k}={v}" for k, v in call.args.items() if k != "content")
        short = " ".join(text.split())
        line = f"turn {self.step} {self.date} {call.name}({args}) -> {short[:200]}"
        with open(self.workspace / "action_log.txt", "a") as f:
            f.write(line + "\n")
        self._record({"t": "tool", "step": self.step, "name": call.name, "args": call.args, "ok": ok, "result": text})

    def _final_status(self) -> dict:
        if self.bridge is None:
            return {}
        status = self.bridge.call("company")
        if status.get("exists"):
            status["vehicle_list"] = self.bridge.call("vehicles")["vehicles"]
        return status

    def _metrics(self, s: dict, outcome: str, error: str | None, wall: float) -> dict:
        c = self.counters
        exists = bool(s.get("exists"))
        vehicles = s.get("vehicle_list", [])
        calls = c["tool_calls"]
        return {
            "run": self.run_dir.name,
            "harness_version": __version__,
            "scenario": self.scenario.name,
            "model": self.model.key,
            "model_id": self.model.model,
            "provider": self.model.provider,
            "outcome": outcome,
            "error": error,
            # Primary score: cash - loan + resale value of vehicles. Starts at 0; a company that
            # does nothing ends slightly below zero (loan interest).
            "score_net_worth": s.get("net_worth") if exists else None,
            "vehicle_value": s.get("vehicle_value") if exists else None,
            "game_company_value": s.get("value") if exists else None,
            "money": s.get("money") if exists else None,
            "loan": s.get("loan") if exists else None,
            "vehicles": len(vehicles),
            "stations": s.get("stations", 0) if exists else 0,
            "vehicles_in_profit": sum(1 for v in vehicles if v["profit_this_year"] + v["profit_last_year"] > 0),
            "vehicle_profit_total": sum(v["profit_this_year"] + v["profit_last_year"] for v in vehicles),
            "game_days": self.days - self.start_days,
            "end_date": self.date,
            "turns": c["turns"],
            "idle_turns": c["idle_turns"],
            "nudges": c["nudges"],
            "tool_calls": calls,
            "tool_errors": c["tool_errors"],
            "tool_error_rate": round(c["tool_errors"] / calls, 3) if calls else None,
            "invalid_calls": c["invalid_calls"],
            "look_calls": c["look_calls"],
            "view_map_calls": c["view_map_calls"],
            "images_sent": c["images_sent"],
            "vision_probes": self.probes,
            "context_segments": c["segments"],
            "input_tokens": c["input_tokens"],
            "output_tokens": c["output_tokens"],
            "cached_tokens": c["cached_tokens"],
            "llm_seconds": round(c["llm_seconds"], 1),
            "wall_seconds": round(wall, 1),
            "video": self.video.name if self.video else None,
        }

    def _write_summary(self, m: dict) -> None:
        def cash(v):
            return "n/a" if v is None else _money(v)
        lines = [
            f"# {m['model']} on {m['scenario']}",
            "",
            f"- Outcome: **{m['outcome']}**" + (f" ({m['error']})" if m["error"] else ""),
            f"- Score (net worth): **{cash(m['score_net_worth'])}**",
            f"- Cash {cash(m['money'])}, loan {cash(m['loan'])}, {m['vehicles']} vehicles ({m['vehicles_in_profit']} in profit), {m['stations']} stations",
            f"- Game time: {m['game_days']} days, ended {m['end_date']}",
            f"- Turns: {m['turns']} ({m['nudges']} needed a reminder to act, {m['idle_turns']} stayed without action); "
            f"tool calls {m['tool_calls']}, errors {m['tool_errors']}, malformed {m['invalid_calls']}",
            f"- Vision: probes {m['vision_probes']}, {m['images_sent']} images sent, look x{m['look_calls']}, view_map x{m['view_map_calls']}",
            f"- Tokens: {m['input_tokens']:,} in / {m['output_tokens']:,} out; model time {m['llm_seconds']:.0f}s of {m['wall_seconds']:.0f}s wall",
            "",
            "Files: `transcript.jsonl` (everything the model saw and did), `timeline.csv`, `images/`, `workspace/`, `final.sav` (open it in OpenTTD)"
            + (f", `{m['video']}`." if m.get("video") else "."),
        ]
        (self.run_dir / "summary.md").write_text("\n".join(lines) + "\n")
