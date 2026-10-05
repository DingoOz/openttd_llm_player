"""The tools a model plays with: schemas, argument checking, and plain-text results."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from .providers.base import ToolCall, ToolSpec

if TYPE_CHECKING:
    from .episode import Episode

INT = {"type": "integer"}


def _obj(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


def _money(v: int) -> str:
    return f"-£{-v:,}" if v < 0 else f"£{v:,}"


@dataclass
class Outcome:
    text: str
    ok: bool = True
    images: list[tuple[Path, str]] = field(default_factory=list)   # (file, caption)


class GameTools:
    def __init__(self, ep: "Episode"):
        self.ep = ep
        self.air = "air" in ep.scenario.transport
        self._handlers: dict[str, Callable[[dict], Outcome]] = {}
        self._specs: list[ToolSpec] = []
        self._register()

    # ------------------------------------------------------------ registry

    def _add(self, name: str, description: str, params: dict, handler: Callable[[dict], Outcome]) -> None:
        self._specs.append(ToolSpec(name, description, params))
        self._handlers[name] = handler

    def specs(self) -> list[ToolSpec]:
        return self._specs

    def _register(self) -> None:
        mx, my = self.ep.geo.size_x - 1, self.ep.geo.size_y - 1
        xy = f"Tile coordinates are integers, x 0..{mx} and y 0..{my}."

        self._add("look", "See the game around a tile: returns a screenshot of the real game view centred on (x, y) "
                  "with tile coordinates drawn on it. zoom 'close' shows about 20 tiles across and labels every 2nd tile, "
                  "'medium' about 40 tiles, 'far' about 80 tiles. The image arrives with your next turn.",
                  _obj({"x": INT, "y": INT, "zoom": {"type": "string", "enum": ["close", "medium", "far"]}}, ["x", "y"]),
                  self._look)
        self._add("view_map", "See the whole world from above: returns a top-down map with a labelled coordinate grid, "
                  "town names, industries and your stations. The image arrives with your next turn.",
                  _obj({}, []), self._view_map)

        topics = ["company", "towns", "industries", "vehicles", "stations", "bus_and_truck_models"]
        if self.air:
            topics.append("aircraft_models")
        self._add("get_info", "Exact facts from the game as text. Topics: company (cash, loan, value, income); towns and "
                  "industries (ids, positions, what they produce and accept); vehicles and stations (yours, with profit "
                  "and waiting cargo); bus_and_truck_models" + (" and aircraft_models" if self.air else "") +
                  " (what you can buy, with engine_id and price).",
                  _obj({"topic": {"type": "string", "enum": topics}}, ["topic"]), self._get_info)
        self._add("inspect_tile", "What is on one tile: its kind (clear, road, house, water, industry, ...), owner, "
                  "whether it is flat and buildable, and which cargo a station there would accept and supply. " + xy,
                  _obj({"x": INT, "y": INT}, ["x", "y"]), self._inspect)

        self._add("list_files", "List the text files in your workspace.", _obj({}, []), self._list_files)
        self._add("read_file", "Read a text file from your workspace, for example guide.md or notes.md.",
                  _obj({"name": {"type": "string"}}, ["name"]), self._read_file)
        self._add("write_notes", "Replace the content of notes.md, your persistent memory. Keep plans, station ids and "
                  "useful coordinates here.", _obj({"content": {"type": "string"}}, ["content"]), self._write_notes)

        self._add("build_road", "Connect two tiles by road. Finds a route, reuses existing roads and builds only what is "
                  "missing. Each end must be a road tile, one of your road stops, or empty land. No bridges or tunnels.",
                  _obj({"x1": INT, "y1": INT, "x2": INT, "y2": INT}, ["x1", "y1", "x2", "y2"]), self._build_road)
        self._add("build_road_stop", "Build a bus stop (passengers) or truck stop (all other cargo) on a straight road "
                  "tile, or on flat empty land. Returns the new station_id and what the stop accepts and supplies. "
                  "On empty land, axis chooses the direction the road runs through the stop.",
                  _obj({"x": INT, "y": INT, "kind": {"type": "string", "enum": ["bus", "truck"]},
                        "axis": {"type": "string", "enum": ["x", "y"]}}, ["x", "y", "kind"]), self._build_stop)
        self._add("build_road_depot", "Build a road vehicle depot on empty land that touches a road tile. Vehicles are "
                  "bought at depots. The depot must be connected by road to the stops its vehicles will serve.",
                  _obj({"x": INT, "y": INT}, ["x", "y"]), self._build_depot)
        if self.air:
            self._add("build_airport", "Build an airport; (x, y) is the corner with the lowest coordinates of a flat, empty "
                      "rectangle. Sizes: small 4x3, commuter 5x4, large 6x6. Aircraft are bought at the airport itself.",
                      _obj({"x": INT, "y": INT, "type": {"type": "string", "enum": ["small", "commuter", "large", "metropolitan", "international"]}},
                           ["x", "y", "type"]), self._build_airport)
        self._add("remove", "Remove what you built on a tile (stop, depot, airport) or clear the tile.",
                  _obj({"x": INT, "y": INT}, ["x", "y"]), self._remove)

        self._add("buy_vehicle", "Buy a vehicle at your depot" + (" or airport" if self.air else "") + " located at "
                  "(depot_x, depot_y). Give station_ids (two or more of your stations, in visiting order) to set its "
                  "route and start it immediately. Buses need bus stops, trucks need truck stops.",
                  _obj({"depot_x": INT, "depot_y": INT, "engine_id": INT,
                        "station_ids": {"type": "array", "items": INT}}, ["depot_x", "depot_y", "engine_id"]), self._buy)
        self._add("set_orders", "Replace a vehicle's route: it will visit station_ids in order, forever. Stations listed "
                  "in full_load_at make it wait there until full.",
                  _obj({"vehicle_id": INT, "station_ids": {"type": "array", "items": INT},
                        "full_load_at": {"type": "array", "items": INT}}, ["vehicle_id", "station_ids"]), self._orders)
        self._add("start_vehicle", "Start a vehicle that is waiting in a depot. It needs a route first.",
                  _obj({"vehicle_id": INT}, ["vehicle_id"]), self._start)
        self._add("clone_vehicle", "Buy copies of an existing vehicle with the same route and start them. Uses the depot "
                  "nearest to the original unless you give depot_x and depot_y.",
                  _obj({"vehicle_id": INT, "count": INT, "depot_x": INT, "depot_y": INT}, ["vehicle_id", "count"]), self._clone)
        self._add("sell_vehicle", "Sell a vehicle. If it is on the road it is sent to a depot first; call again once it has arrived.",
                  _obj({"vehicle_id": INT}, ["vehicle_id"]), self._sell)

        self._add("set_loan", "Set your total loan to this amount (rounded down to a multiple of 10,000). Raise it to get "
                  "cash, lower it to stop paying interest.", _obj({"amount": INT}, ["amount"]), self._loan)
        limit = self.ep.scenario.episode.max_wait_days
        self._add("wait", f"Let the game run for this many days (1 to {limit}) before your next turn, in addition to the "
                  "days that pass automatically. Use it when vehicles need time to earn.",
                  _obj({"days": INT}, ["days"]), self._wait)

    # --------------------------------------------------------- dispatching

    def run(self, call: ToolCall) -> Outcome:
        if call.error:
            return Outcome(f"error: {call.error}", ok=False)
        handler = self._handlers.get(call.name)
        if handler is None:
            return Outcome(f"error: unknown tool '{call.name}'. Available tools: {', '.join(self._handlers)}", ok=False)
        spec = next(s for s in self._specs if s.name == call.name)
        args, problem = _check_args(call.args, spec.parameters)
        if problem:
            return Outcome(f"error: {problem}", ok=False)
        return handler(args)

    def _call(self, cmd: str, changes_world: bool = False, timeout: float = 60.0, **args) -> dict:
        res = self.ep.bridge.call(cmd, timeout=timeout, **args)
        if changes_world and res.get("ok"):
            self.ep.world_dirty = True
        return res

    @staticmethod
    def _fail(res: dict) -> Outcome:
        parts = [f"error: {res.get('error', 'failed')}"]
        if res.get("hint"):
            parts.append(f"hint: {res['hint']}")
        if res.get("nearby_valid_tiles"):
            parts.append("nearby tiles that would work: " + ", ".join(f"({x},{y})" for x, y in res["nearby_valid_tiles"]))
        elif "nearby_valid_tiles" in res:
            parts.append("no suitable tile within a few tiles; look at the area first")
        for key in ("stopped_at", "tiles_built", "note"):
            if key in res:
                parts.append(f"{key}: {res[key]}")
        return Outcome("; ".join(parts), ok=False)

    # ------------------------------------------------------------- vision

    def _look(self, a: dict) -> Outcome:
        geo = self.ep.geo
        if not (0 <= a["x"] < geo.size_x and 0 <= a["y"] < geo.size_y):
            return Outcome(f"error: tile is outside the map (x 0..{geo.size_x - 1}, y 0..{geo.size_y - 1})", ok=False)
        if not self.ep.image_budget_left():
            return Outcome("error: image limit for this turn reached; look again next turn", ok=False)
        zoom = a.get("zoom", "medium")
        path = self.ep.render_look(a["x"], a["y"], zoom)
        caption = f"game view centred on tile ({a['x']},{a['y']}), zoom {zoom}"
        return Outcome(f"ok: {caption} is attached to your next turn", images=[(path, caption)])

    def _view_map(self, a: dict) -> Outcome:
        if not self.ep.image_budget_left():
            return Outcome("error: image limit for this turn reached; ask again next turn", ok=False)
        path = self.ep.render_overview()
        return Outcome("ok: top-down world map is attached to your next turn", images=[(path, "top-down map of the whole world")])

    # -------------------------------------------------------------- facts

    def _get_info(self, a: dict) -> Outcome:
        topic = a["topic"]
        if topic == "company":
            return Outcome(self.ep.company_text(self._call("company")))
        if topic == "towns":
            return Outcome(format_towns(self._call("towns")["towns"]))
        if topic == "industries":
            return Outcome(format_industries(self._call("industries")["industries"]))
        if topic == "vehicles":
            return Outcome(format_vehicles(self._call("vehicles")["vehicles"]))
        if topic == "stations":
            res = self._call("stations")
            return Outcome(format_stations(res["stations"], res["depots"]))
        kind = "aircraft" if topic == "aircraft_models" else "road"
        return Outcome(format_engines(self._call("engines", kind=kind)["engines"], kind))

    def _inspect(self, a: dict) -> Outcome:
        res = self._call("tile", x=a["x"], y=a["y"])
        if not res.get("ok"):
            return self._fail(res)
        c = res["catchment"]
        bits = [f"tile ({a['x']},{a['y']}): {res['kind']}", f"owner {res['owner']}",
                "flat" if res["flat"] else "sloped", "buildable" if res["buildable"] else "not buildable",
                f"height {res['height']}", f"nearest town {res['nearest_town']}"]
        if "industry" in res:
            bits.append(f"industry {res['industry']}")
        if "station_id" in res:
            bits.append(f"station_id {res['station_id']}")
        if "can_place_bus_stop" in res:
            bits.append("a road stop can be built here" if res["can_place_bus_stop"] else "no road stop possible here (junction, bend or dead end)")
        bits.append(f"a station here would accept [{', '.join(c['accepts']) or 'nothing'}] and supply [{', '.join(c['supplies']) or 'nothing'}]")
        return Outcome("; ".join(bits))

    # -------------------------------------------------------------- files

    def _list_files(self, a: dict) -> Outcome:
        files = sorted(p for p in self.ep.workspace.iterdir() if p.is_file())
        return Outcome("\n".join(f"{p.name} ({p.stat().st_size} bytes)" for p in files))

    def _read_file(self, a: dict) -> Outcome:
        path = self.ep.workspace / Path(a["name"]).name   # no directory traversal
        if not path.is_file():
            return Outcome(f"error: no file '{a['name']}'. Files: {', '.join(sorted(p.name for p in self.ep.workspace.iterdir()))}", ok=False)
        text = path.read_text()
        limit = 6000
        if len(text) > limit:
            text = text[-limit:] if path.name == "action_log.txt" else text[:limit]
            text += f"\n[file is longer; showing {'the last' if path.name == 'action_log.txt' else 'the first'} {limit} characters]"
        return Outcome(text or "(empty)")

    def _write_notes(self, a: dict) -> Outcome:
        (self.ep.workspace / "notes.md").write_text(a["content"])
        return Outcome(f"ok: notes.md saved ({len(a['content'])} characters)")

    # ------------------------------------------------------- construction

    def _build_road(self, a: dict) -> Outcome:
        res = self._call("build_road_route", changes_world=True, timeout=180, **a)
        if not res.get("ok"):
            if res.get("tiles_built"):
                self.ep.world_dirty = True
            return self._fail(res)
        return Outcome(f"ok: road from ({a['x1']},{a['y1']}) to ({a['x2']},{a['y2']}) is complete: {res['length']} tiles long, "
                       f"{res['tiles_built']} built, {res['tiles_reused']} already existed, cost {_money(res['cost'])}")

    def _build_stop(self, a: dict) -> Outcome:
        res = self._call("build_road_stop", changes_world=True, hints=self.ep.scenario.episode.spatial_hints, **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: {res['kind']} stop built at ({a['x']},{a['y']}): station_id {res['station_id']}, name '{res['name']}', "
                       f"accepts [{', '.join(res['accepts']) or 'nothing'}], supplies [{', '.join(res['supplies']) or 'nothing'}], "
                       f"cost {_money(res['cost'])}")

    def _build_depot(self, a: dict) -> Outcome:
        res = self._call("build_road_depot", changes_world=True, hints=self.ep.scenario.episode.spatial_hints, **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: road depot built at ({a['x']},{a['y']}), opening onto road tile ({res['faces'][0]},{res['faces'][1]}), "
                       f"cost {_money(res['cost'])}. Buy vehicles with depot_x={a['x']}, depot_y={a['y']}")

    def _build_airport(self, a: dict) -> Outcome:
        res = self._call("build_airport", changes_world=True, **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: {a['type']} airport built at ({a['x']},{a['y']}), size {res['size'][0]}x{res['size'][1]}: station_id "
                       f"{res['station_id']}, name '{res['name']}', accepts [{', '.join(res['accepts'])}], supplies "
                       f"[{', '.join(res['supplies'])}], cost {_money(res['cost'])}. Buy aircraft with depot_x={a['x']}, depot_y={a['y']}")

    def _remove(self, a: dict) -> Outcome:
        res = self._call("remove", changes_world=True, **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: removed {res['removed']} at ({a['x']},{a['y']}), cost {_money(res['cost'])}")

    # ----------------------------------------------------------- vehicles

    def _buy(self, a: dict) -> Outcome:
        stations = a.get("station_ids") or []
        if stations and len(stations) < 2:
            return Outcome("error: station_ids needs at least two stations", ok=False)
        res = self._call("buy_vehicle", changes_world=True, depot_x=a["depot_x"], depot_y=a["depot_y"], engine_id=a["engine_id"])
        if not res.get("ok"):
            return self._fail(res)
        vid = res["id"]
        head = f"ok: bought {res['engine']} (vehicle_id {vid}, carries {res['capacity']} {res['cargo']}) for {_money(res['cost'])}"
        if not stations:
            return Outcome(head + ". It is waiting in the depot: call set_orders, then start_vehicle")
        orders = self._call("set_orders", vehicle_id=vid, orders=[{"station_id": s} for s in stations])
        if not orders.get("ok"):
            return Outcome(f"{head}, but its route could not be set: {orders.get('error')}"
                           f"{'; ' + orders['hint'] if orders.get('hint') else ''}. It is waiting in the depot (sell it or fix the route with set_orders)", ok=False)
        started = self._call("start_vehicle", vehicle_id=vid)
        if not started.get("ok"):
            return Outcome(f"{head}; route set, but it could not start: {started.get('error')}", ok=False)
        return Outcome(f"{head}; route {' -> '.join(orders['orders'])}; started")

    def _orders(self, a: dict) -> Outcome:
        if len(a["station_ids"]) < 2:
            return Outcome("error: station_ids needs at least two stations", ok=False)
        full = set(a.get("full_load_at") or [])
        orders = [{"station_id": s, "flags": ["full_load"]} if s in full else {"station_id": s} for s in a["station_ids"]]
        res = self._call("set_orders", vehicle_id=a["vehicle_id"], orders=orders)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: vehicle {a['vehicle_id']} route is now {' -> '.join(res['orders'])} (state: {res['state']})")

    def _start(self, a: dict) -> Outcome:
        res = self._call("start_vehicle", changes_world=True, **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: vehicle {a['vehicle_id']} is {res['state']}" + (f" ({res['note']})" if res.get("note") else ""))

    def _clone(self, a: dict) -> Outcome:
        if "depot_x" in a and "depot_y" in a:
            depot = (a["depot_x"], a["depot_y"])
        else:
            depot = self._nearest_depot(a["vehicle_id"])
            if depot is None:
                return Outcome("error: could not find a depot for this vehicle; give depot_x and depot_y", ok=False)
        res = self._call("clone_vehicle", changes_world=True, vehicle_id=a["vehicle_id"], count=a["count"],
                         depot_x=depot[0], depot_y=depot[1])
        if not res.get("ok"):
            return self._fail(res)
        text = f"ok: bought and started {len(res['vehicle_ids'])} copies (vehicle_ids {res['vehicle_ids']}) for {_money(res['cost'])}"
        if res.get("stopped_early"):
            text += f"; stopped early: {res['stopped_early']}"
        return Outcome(text)

    def _nearest_depot(self, vehicle_id: int) -> tuple[int, int] | None:
        vehicles = {v["id"]: v for v in self._call("vehicles")["vehicles"]}
        v = vehicles.get(vehicle_id)
        if v is None:
            return None
        want = "hangar" if v["type"] == "aircraft" else "road_depot"
        depots = [d for d in self._call("stations")["depots"] if d["kind"] == want]
        if not depots:
            return None
        best = min(depots, key=lambda d: abs(d["x"] - v["x"]) + abs(d["y"] - v["y"]))
        return best["x"], best["y"]

    def _sell(self, a: dict) -> Outcome:
        res = self._call("sell_vehicle", changes_world=True, **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: sold for {_money(res['refund'])}" if res["sold"] else f"ok: {res['note']}")

    # ------------------------------------------------------ money and time

    def _loan(self, a: dict) -> Outcome:
        res = self._call("set_loan", **a)
        if not res.get("ok"):
            return self._fail(res)
        return Outcome(f"ok: loan is now {_money(res['loan'])}, cash {_money(res['money'])}")

    def _wait(self, a: dict) -> Outcome:
        days = max(1, min(a["days"], self.ep.scenario.episode.max_wait_days, self.ep.days_left()))
        if self.ep.days_left() <= 0:
            return Outcome("error: the episode is at its end date", ok=False)
        events = self.ep.advance(days)
        text = f"ok: {days} days passed; it is now {self.ep.date}"
        if events:
            text += ". Events: " + "; ".join(events)
        return Outcome(text)


# ------------------------------------------------------------------ helpers

def _check_args(args: dict, schema: dict) -> tuple[dict, str | None]:
    """Validate against the tool's schema, coercing the near-misses small models produce ("12", 12.0)."""
    props = schema.get("properties", {})
    out: dict = {}
    for key, value in args.items():
        if key not in props:
            continue   # ignore extras rather than failing the call
        kind = props[key].get("type")
        if kind == "integer":
            value = _to_int(value)
            if value is None:
                return {}, f"argument '{key}' must be an integer"
        elif kind == "array":
            if isinstance(value, (int, float, str)):
                value = [value]
            if not isinstance(value, list):
                return {}, f"argument '{key}' must be a list"
            if props[key].get("items", {}).get("type") == "integer":
                value = [_to_int(v) for v in value]
                if any(v is None for v in value):
                    return {}, f"argument '{key}' must be a list of integers"
        elif kind == "string":
            if not isinstance(value, str):
                value = str(value)
            if "enum" in props[key] and value not in props[key]["enum"]:
                return {}, f"argument '{key}' must be one of: {', '.join(props[key]['enum'])}"
        out[key] = value
    missing = [k for k in schema.get("required", []) if k not in out]
    if missing:
        return {}, f"missing required argument(s): {', '.join(missing)}. Expected: {', '.join(props)}"
    return out, None


def _to_int(value) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value == int(value):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def format_towns(towns: list[dict]) -> str:
    rows = ["town_id | name | centre (x,y) | population | passengers produced last month | % transported"]
    for t in sorted(towns, key=lambda t: -t["population"]):
        rows.append(f"{t['id']} | {t['name']} | ({t['x']},{t['y']}) | {t['population']} | {t['passengers_last_month']} | {t['transported_pct']}%")
    return "\n".join(rows)


def format_industries(industries: list[dict]) -> str:
    rows = ["industry_id | type | name | position (x,y) | produces (per month, % transported) | accepts"]
    for i in industries:
        prod = ", ".join(f"{p['cargo']} {p['last_month']}/month ({p['transported_pct']}%)" for p in i["produces"]) or "-"
        rows.append(f"{i['id']} | {i['type']} | {i['name']} | ({i['x']},{i['y']}) | {prod} | {', '.join(i['accepts']) or '-'}")
    return "\n".join(rows)


def format_vehicles(vehicles: list[dict]) -> str:
    if not vehicles:
        return "You own no vehicles."
    rows = ["vehicle_id | model | state | position | load | profit this year | profit last year | route"]
    for v in sorted(vehicles, key=lambda v: v["id"]):
        rows.append(f"{v['id']} | {v['engine']} | {v['state']} | ({v['x']},{v['y']}) | {v['load']}/{v['capacity']} {v['cargo']} | "
                    f"{_money(v['profit_this_year'])} | {_money(v['profit_last_year'])} | {' -> '.join(v['orders']) or 'none'}")
    return "\n".join(rows)


def format_stations(stations: list[dict], depots: list[dict]) -> str:
    rows = []
    if stations:
        rows.append("station_id | name | kind | position | vehicles using it | cargo waiting (rating)")
        for s in sorted(stations, key=lambda s: s["id"]):
            cargo = ", ".join(f"{c['waiting']} {c['cargo']} ({c['rating_pct']}%)" for c in s["cargo"]) or "nothing yet"
            rows.append(f"{s['id']} | {s['name']} | {'+'.join(s['kinds'])} | ({s['x']},{s['y']}) | {s['vehicles']} | {cargo}")
    else:
        rows.append("You own no stations.")
    rows.append("Depots: " + (", ".join(f"{d['kind']} at ({d['x']},{d['y']})" for d in depots) or "none"))
    return "\n".join(rows)


def format_engines(engines: list[dict], kind: str) -> str:
    if not engines:
        return "Nothing can be bought yet."
    extra = "plane type" if kind == "aircraft" else "uses stop kind"
    rows = [f"engine_id | model | cargo | capacity | max speed | price | running cost per year | {extra}"]
    for e in sorted(engines, key=lambda e: (e["cargo"], e["price"])):
        tail = e.get("plane_type") or e.get("stop_kind")
        rows.append(f"{e['id']} | {e['name']} | {e['cargo']} | {e['capacity']} | {e['max_speed']} | {_money(e['price'])} | "
                    f"{_money(e['running_cost_per_year'])} | {tail}")
    return "\n".join(rows)
