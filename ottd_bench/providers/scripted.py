"""A fixed, non-LLM policy that plays through the same tool interface as a model.

It builds one bus line between the two best-placed towns and then waits. Use it to check the
harness end to end without any model, as a reference score for a scenario, and to confirm
that a scenario replays identically (two scripted runs must produce the same metrics).
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import Provider, ToolCall, ToolResult, ToolSpec, Turn

_TILE = re.compile(r"\((\d+),(\d+)\)")


class ScriptedProvider(Provider):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.state = "start"
        self.towns: list[tuple[str, int, int, int]] = []     # name, x, y, population
        self.pair: list[tuple[int, int]] = []                # target tile per stop
        self.stops: list[tuple[int, int, int] | None] = [None, None]   # x, y, station_id
        self.depot: tuple[int, int] | None = None
        self.depot_try: tuple[int, int] | None = None
        self.vehicle: int | None = None
        self.last: list[ToolResult] = []
        self.n = 0

    def begin(self, system: str, tools: list[ToolSpec]) -> None:
        pass

    def add_user(self, text: str, images: list[Path] | None = None) -> None:
        # The first briefing lists the largest towns as "  Name: centre (x,y), population N".
        if not self.towns:
            for m in re.finditer(r"^\s+(.+?): centre \((\d+),(\d+)\), population (\d+)$", text, re.MULTILINE):
                self.towns.append((m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))))

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self.last = results

    def _call(self, name: str, **args) -> ToolCall:
        self.n += 1
        return ToolCall(id=f"s{self.n}", name=name, args=args)

    def generate(self) -> Turn:
        calls = self._decide()
        return Turn(text=f"[scripted: {self.state}]", tool_calls=calls, stop="tool_use" if calls else "end")

    def _decide(self) -> list[ToolCall]:
        if self.state == "start":
            # Best pair: large towns, far enough apart to pay, close enough to afford the road.
            best = None
            for i, a in enumerate(self.towns):
                for b in self.towns[i + 1:]:
                    dist = abs(a[1] - b[1]) + abs(a[2] - b[2])
                    if 15 <= dist <= 70:
                        score = min(a[3], b[3]) * dist
                        if best is None or score > best[0]:
                            best = (score, a, b)
            if best is None:
                self.state = "idle"
                return [self._call("wait", days=60)]
            self.pair = [(best[1][1], best[1][2]), (best[2][1], best[2][2])]
            self.state = "stops"
            return [self._call("view_map"), self._call("look", x=self.pair[0][0], y=self.pair[0][1], zoom="close")] + \
                   [self._call("build_road_stop", x=x, y=y, kind="bus") for x, y in self.pair]

        if self.state == "stops":
            # Results arrive in the order the calls were made, one per stop that is still missing.
            pending = [i for i, s in enumerate(self.stops) if s is None]
            results = [r for r in self.last if r.call.name == "build_road_stop"]
            retries = []
            for idx, r in zip(pending, results):
                if r.ok:
                    sid = int(re.search(r"station_id (\d+)", r.text).group(1))
                    self.stops[idx] = (r.call.args["x"], r.call.args["y"], sid)
                    continue
                hint = _TILE.search(r.text.split("would work:")[-1]) if "would work:" in r.text else None
                if hint is None:
                    self.state = "idle"
                    return [self._call("wait", days=60)]
                retries.append(self._call("build_road_stop", x=int(hint.group(1)), y=int(hint.group(2)), kind="bus"))
            if retries:
                return retries
            self.state = "road"
            (x1, y1, _), (x2, y2, _) = self.stops
            self.depot_try = (x1, y1)
            return [self._call("build_road", x1=x1, y1=y1, x2=x2, y2=y2),
                    self._call("build_road_depot", x=x1, y=y1)]   # fails on purpose: the error names valid sites

        if self.state == "road":
            road = next(r for r in self.last if r.call.name == "build_road")
            depot = next(r for r in self.last if r.call.name == "build_road_depot")
            if not road.ok:
                self.state = "idle"
                return [self._call("wait", days=60)]
            hint = _TILE.search(depot.text.split("would work:")[-1]) if "would work:" in depot.text else None
            if hint is None:
                self.state = "idle"
                return [self._call("wait", days=60)]
            self.depot = (int(hint.group(1)), int(hint.group(2)))
            self.state = "depot"
            return [self._call("build_road_depot", x=self.depot[0], y=self.depot[1]),
                    self._call("get_info", topic="bus_and_truck_models")]

        if self.state == "depot":
            models = next(r for r in self.last if r.call.name == "get_info")
            buses = [line.split(" | ") for line in models.text.splitlines()[1:] if " | PASS | " in line]
            if not self.last[0].ok or not buses:
                self.state = "idle"
                return [self._call("wait", days=60)]
            engine = int(max(buses, key=lambda row: int(row[3]))[0])   # largest capacity
            self.state = "bought"
            return [self._call("buy_vehicle", depot_x=self.depot[0], depot_y=self.depot[1], engine_id=engine,
                               station_ids=[self.stops[0][2], self.stops[1][2]]),
                    self._call("write_notes", content=f"Bus line between stations {self.stops[0][2]} and {self.stops[1][2]}; "
                                                      f"depot at {self.depot}.")]

        if self.state == "bought":
            bought = self.last[0]
            self.state = "idle"
            if bought.ok:
                self.vehicle = int(re.search(r"vehicle_id (\d+)", bought.text).group(1))
                return [self._call("clone_vehicle", vehicle_id=self.vehicle, count=3), self._call("wait", days=30)]

        return [self._call("wait", days=60)]
