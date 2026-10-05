/*
 * LLMBridge: a JSON-RPC bridge between an external harness and the game.
 *
 * The harness connects to the admin port and sends {"id": n, "cmd": "...", "args": {...}}.
 * Each request is answered with one or more {"id": n, "seq": k, "last": bool, "d": "<json fragment>"}
 * packets; concatenating the fragments gives the JSON reply. Replies are chunked because a
 * single admin packet carries at most ~1400 bytes.
 *
 * All company actions run in GSCompanyMode for the company named in "init". The game is kept
 * paused by this script; only "advance" lets time pass, and it re-pauses on an exact tick, so
 * a slow model and a fast model see identical game time.
 */

class LLMBridge extends GSController {
	static CHUNK = 600;        // raw bytes per packet; worst case doubles under JSON escaping
	static VERSION = 1;
	/* Pathfinder heuristic weight per tile. New road costs 4, so 5 makes the search greedy enough to
	 * cross a 128-tile map in a few thousand expansions, at the price of slightly longer routes. */
	static PF_WEIGHT = 5;

	company = null;            // GSCompany id we act for, or null before "init"
	cargos = null;             // [{id, label, name}]
	events = null;             // events recorded since the last "advance" / "events" call
	adv_id = null;             // request id of a pending "advance", or null
	adv_target = 0;            // date at which the pending "advance" completes
	adv_from = 0;

	constructor()
	{
		this.cargos = [];
		this.events = [];
	}
}

function LLMBridge::Start()
{
	GSGame.Pause();
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	foreach (c, _ in GSCargoList()) {
		this.cargos.append({ id = c, label = GSCargo.GetCargoLabel(c), name = GSCargo.GetName(c) });
	}
	GSLog.Info("LLMBridge ready");

	while (true) {
		this.HandleEvents();
		if (this.adv_id != null && GSDate.GetCurrentDate() >= this.adv_target) this.FinishAdvance();
		this.Sleep(1);
	}
}

function LLMBridge::HandleEvents()
{
	while (GSEventController.IsEventWaiting()) {
		local e = GSEventController.GetNextEvent();
		if (e.GetEventType() == GSEvent.ET_ADMIN_PORT) {
			local req = GSEventAdminPort.Convert(e).GetObject();
			if (typeof req == "table") this.Dispatch(req);
		} else {
			this.RecordEvent(e);
		}
	}
}

function LLMBridge::Dispatch(req)
{
	local id = ("id" in req) ? req.id : -1;
	local cmd = ("cmd" in req) ? req.cmd : "";
	local args = ("args" in req && typeof req.args == "table") ? req.args : {};
	local fn = "Cmd_" + cmd;
	local res = null;
	if (!(fn in this)) {
		res = { ok = false, error = "unknown command '" + cmd + "'" };
	} else if (this.company == null && cmd != "init" && cmd != "ping") {
		res = { ok = false, error = "bridge not initialised" };
	} else if (this.adv_id != null && cmd != "ping" && cmd != "company") {
		/* Only read-only status may be asked for while the clock runs. */
		res = { ok = false, error = "busy: time is advancing" };
	} else {
		try {
			res = this[fn](args, id);
		} catch (ex) {
			res = { ok = false, error = "bad request: " + ex };
		}
	}
	/* A null result means the handler replies later (see "advance"). */
	if (res != null) this.Reply(id, res);
}

/* ------------------------------------------------------------------ JSON */

function LLMBridge::Esc(s)
{
	local n = s.len();
	local dirty = false;
	for (local i = 0; i < n; i++) {
		local c = s[i];
		if (c == 34 || c == 92 || (c >= 0 && c < 32)) { dirty = true; break; }
	}
	if (!dirty) return s;
	local out = "";
	for (local i = 0; i < n; i++) {
		local c = s[i];
		if (c == 34) out += "\\\"";
		else if (c == 92) out += "\\\\";
		else if (c >= 0 && c < 32) out += " ";
		else out += s.slice(i, i + 1);
	}
	return out;
}

function LLMBridge::ToJSON(v)
{
	local t = typeof v;
	if (t == "null") return "null";
	if (t == "bool") return v ? "true" : "false";
	if (t == "integer" || t == "float") return v.tostring();
	if (t == "string") return "\"" + this.Esc(v) + "\"";
	if (t == "array") {
		local s = "[";
		foreach (i, x in v) {
			if (i > 0) s += ",";
			s += this.ToJSON(x);
		}
		return s + "]";
	}
	if (t == "table") {
		local s = "{";
		local first = true;
		foreach (k, x in v) {
			if (!first) s += ",";
			first = false;
			s += "\"" + k + "\":" + this.ToJSON(x);
		}
		return s + "}";
	}
	return "\"<" + t + ">\"";
}

function LLMBridge::Reply(id, res)
{
	local s = this.ToJSON(res);
	local total = s.len();
	local pos = 0;
	local seq = 0;
	do {
		local end = pos + LLMBridge.CHUNK;
		if (end >= total) {
			end = total;
		} else {
			/* Never split a UTF-8 sequence: back up while 'end' points at a continuation byte. */
			while (end > pos + 1 && (s[end] & 0xC0) == 0x80) end--;
		}
		GSAdmin.Send({ id = id, seq = seq, last = (end >= total), d = s.slice(pos, end) });
		pos = end;
		seq++;
	} while (pos < total);
}

/* --------------------------------------------------------------- helpers */

function LLMBridge::T(x, y) { return GSMap.GetTileIndex(x, y); }
function LLMBridge::Err() { return GSError.GetLastErrorString(); }

function LLMBridge::DateStr()
{
	local d = GSDate.GetCurrentDate();
	local m = GSDate.GetMonth(d);
	local dd = GSDate.GetDayOfMonth(d);
	return GSDate.GetYear(d) + "-" + (m < 10 ? "0" : "") + m + "-" + (dd < 10 ? "0" : "") + dd;
}

function LLMBridge::CargoLabel(c)
{
	foreach (e in this.cargos) if (e.id == c) return e.label;
	return "?";
}

function LLMBridge::CargoByLabel(label)
{
	foreach (e in this.cargos) if (e.label == label) return e.id;
	return -1;
}

function LLMBridge::TileKind(t)
{
	if (!GSMap.IsValidTile(t)) return "void";
	if (GSRoad.IsRoadStationTile(t)) return "road_stop";
	if (GSRoad.IsRoadDepotTile(t)) return "road_depot";
	if (GSAirport.IsAirportTile(t)) return "airport";
	if (GSTile.IsStationTile(t)) return "station";
	if (GSRoad.IsRoadTile(t)) return "road";
	if (GSTile.IsHouseTile(t)) return "house";
	if (GSIndustry.IsValidIndustry(GSIndustry.GetIndustryID(t))) return "industry";
	if (GSTile.IsWaterTile(t)) return "water";
	if (GSTile.HasTransportType(t, GSTile.TRANSPORT_RAIL)) return "rail";
	if (GSTile.IsBuildable(t)) return GSTile.HasTreeOnTile(t) ? "trees" : "clear";
	return "other";
}

function LLMBridge::Owner(t)
{
	local o = GSTile.GetOwner(t);
	if (o == this.company) return "you";
	if (o != GSCompany.COMPANY_INVALID) return "other_company";
	if (GSTile.IsHouseTile(t) || GSRoad.IsRoadTile(t)) return "town";
	return "none";
}

/* What a station at tile t with the given catchment radius would accept and supply. */
function LLMBridge::Catchment(t, radius)
{
	local acc = [];
	local prod = [];
	foreach (c in this.cargos) {
		if (GSTile.GetCargoAcceptance(t, c.id, 1, 1, radius) >= 8) acc.append(c.label);
		if (GSTile.GetCargoProduction(t, c.id, 1, 1, radius) > 0) prod.append(c.label);
	}
	return { accepts = acc, supplies = prod };
}

function LLMBridge::TryStop(t, front, kind)
{
	local tm = GSTestMode();
	return GSRoad.BuildDriveThroughRoadStation(t, front, kind, GSStation.STATION_NEW);
}

/* Straight road tiles near t on which a drive-through stop could be built. */
function LLMBridge::NearbyStopSites(t, kind, radius, limit)
{
	local out = [];
	local x0 = GSMap.GetTileX(t);
	local y0 = GSMap.GetTileY(t);
	local sx = GSMap.GetMapSizeX();
	for (local d = 1; d <= radius && out.len() < limit; d++) {
		for (local dx = -d; dx <= d && out.len() < limit; dx++) {
			for (local dy = -d; dy <= d && out.len() < limit; dy++) {
				if (abs(dx) != d && abs(dy) != d) continue;
				local x = x0 + dx;
				local y = y0 + dy;
				if (x < 1 || y < 1 || x >= sx - 1 || y >= GSMap.GetMapSizeY() - 1) continue;
				local n = this.T(x, y);
				if (!GSRoad.IsRoadTile(n) || GSRoad.IsRoadStationTile(n)) continue;
				if (this.TryStop(n, n + 1, kind) || this.TryStop(n, n + sx, kind)) out.append([x, y]);
			}
		}
	}
	return out;
}

/* The tile a depot at t would open onto, or -1. Plain road is preferred; failing that, the open
 * end of a drive-through stop, which can only be entered along its own axis. */
function LLMBridge::DepotFront(t)
{
	local sx = GSMap.GetMapSizeX();
	local stop_end = -1;
	foreach (o in [1, -1, sx, -sx]) {
		local n = t + o;
		if (!GSMap.IsValidTile(n) || GSMap.DistanceManhattan(t, n) != 1) continue;
		if (GSRoad.IsRoadStationTile(n)) {
			if (GSRoad.IsDriveThroughRoadStationTile(n) &&
					(GSRoad.GetRoadStationFrontTile(n) == t || GSRoad.GetDriveThroughBackTile(n) == t)) stop_end = n;
		} else if (GSRoad.IsRoadTile(n)) {
			return n;
		}
	}
	return stop_end;
}

/* Flat empty tiles near t that touch a road. */
function LLMBridge::NearbyDepotSites(t, radius, limit)
{
	local out = [];
	local x0 = GSMap.GetTileX(t);
	local y0 = GSMap.GetTileY(t);
	for (local d = 1; d <= radius && out.len() < limit; d++) {
		for (local dx = -d; dx <= d && out.len() < limit; dx++) {
			for (local dy = -d; dy <= d && out.len() < limit; dy++) {
				if (abs(dx) != d && abs(dy) != d) continue;
				local x = x0 + dx;
				local y = y0 + dy;
				if (x < 1 || y < 1 || x >= GSMap.GetMapSizeX() - 1 || y >= GSMap.GetMapSizeY() - 1) continue;
				local n = this.T(x, y);
				if (GSTile.IsBuildable(n) && GSTile.GetSlope(n) == GSTile.SLOPE_FLAT && this.DepotFront(n) != -1) out.append([x, y]);
			}
		}
	}
	return out;
}

function LLMBridge::RecordEvent(e)
{
	local rec = null;
	switch (e.GetEventType()) {
		case GSEvent.ET_VEHICLE_CRASHED: {
			local ev = GSEventVehicleCrashed.Convert(e);
			local site = ev.GetCrashSite();
			rec = { type = "vehicle_crashed", vehicle_id = ev.GetVehicleID(), x = GSMap.GetTileX(site), y = GSMap.GetTileY(site) };
			break;
		}
		case GSEvent.ET_COMPANY_IN_TROUBLE:
			rec = { type = "company_in_trouble", note = "money is negative; bankruptcy follows if this persists" };
			break;
		case GSEvent.ET_COMPANY_BANKRUPT:
			rec = { type = "company_bankrupt" };
			break;
		case GSEvent.ET_STATION_FIRST_VEHICLE: {
			local ev = GSEventStationFirstVehicle.Convert(e);
			rec = { type = "first_vehicle_arrived", station_id = ev.GetStationID(), vehicle_id = ev.GetVehicleID() };
			break;
		}
		case GSEvent.ET_INDUSTRY_OPEN: {
			local ev = GSEventIndustryOpen.Convert(e);
			rec = { type = "industry_opened", industry_id = ev.GetIndustryID() };
			break;
		}
		case GSEvent.ET_INDUSTRY_CLOSE: {
			local ev = GSEventIndustryClose.Convert(e);
			rec = { type = "industry_closed", industry_id = ev.GetIndustryID() };
			break;
		}
	}
	if (rec != null) {
		rec.date <- this.DateStr();
		if (this.events.len() < 50) this.events.append(rec);
	}
}

function LLMBridge::TakeEvents()
{
	local ev = this.events;
	this.events = [];
	return ev;
}

/* ------------------------------------------------------------- lifecycle */

function LLMBridge::Cmd_ping(a, id)
{
	return { ok = true, version = LLMBridge.VERSION, date = this.DateStr(), paused = GSGame.IsPaused() };
}

function LLMBridge::Cmd_init(a, id)
{
	local c = GSCompany.ResolveCompanyID(a.company);
	if (c == GSCompany.COMPANY_INVALID) return { ok = false, error = "company does not exist yet" };
	this.company = c;
	local cm = GSCompanyMode(c);
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	if ("name" in a) GSCompany.SetName(a.name);
	/* A fixed colour that no terrain uses, so the company's property stands out in screenshots. */
	GSCompany.SetPrimaryLiveryColour(GSCompany.LS_DEFAULT, GSCompany.COLOUR_YELLOW);
	return {
		ok = true,
		version = LLMBridge.VERSION,
		date = this.DateStr(),
		days = GSDate.GetCurrentDate(),
		map_x = GSMap.GetMapSizeX(),
		map_y = GSMap.GetMapSizeY(),
		freeform_edges = GSGameSettings.GetValue("construction.freeform_edges"),
		paused = GSGame.IsPaused(),
		cargos = this.cargos
	};
}

function LLMBridge::Cmd_advance(a, id)
{
	local days = a.days;
	if (days < 1) return { ok = false, error = "days must be >= 1" };
	this.adv_from = GSDate.GetCurrentDate();
	this.adv_target = this.adv_from + days;
	this.adv_id = id;
	GSGame.Unpause();
	return null;
}

function LLMBridge::FinishAdvance()
{
	GSGame.Pause();
	local id = this.adv_id;
	this.adv_id = null;
	this.Reply(id, {
		ok = true,
		date = this.DateStr(),
		days = GSDate.GetCurrentDate(),
		advanced = GSDate.GetCurrentDate() - this.adv_from,
		events = this.TakeEvents()
	});
}

function LLMBridge::Cmd_events(a, id)
{
	return { ok = true, events = this.TakeEvents() };
}

/* ----------------------------------------------------------- observation */

function LLMBridge::Cmd_company(a, id)
{
	local c = this.company;
	if (GSCompany.ResolveCompanyID(c) == GSCompany.COMPANY_INVALID) return { ok = true, exists = false, date = this.DateStr() };
	local cm = GSCompanyMode(c);
	local q = GSCompany.CURRENT_QUARTER;
	local fleet = 0;
	foreach (v, _ in GSVehicleList()) fleet += GSVehicle.GetCurrentValue(v);
	return {
		ok = true,
		exists = true,
		/* The game's own company value is clamped at 1, which hides losses; report the parts instead. */
		vehicle_value = fleet,
		net_worth = GSCompany.GetBankBalance(c) - GSCompany.GetLoanAmount() + fleet,
		date = this.DateStr(),
		days = GSDate.GetCurrentDate(),
		name = GSCompany.GetName(c),
		money = GSCompany.GetBankBalance(c),
		loan = GSCompany.GetLoanAmount(),
		max_loan = GSCompany.GetMaxLoanAmount(),
		loan_step = GSCompany.GetLoanInterval(),
		value = GSCompany.GetQuarterlyCompanyValue(c, q),
		income_this_quarter = GSCompany.GetQuarterlyIncome(c, q),
		expenses_this_quarter = GSCompany.GetQuarterlyExpenses(c, q),
		delivered_this_quarter = GSCompany.GetQuarterlyCargoDelivered(c, q),
		income_last_quarter = GSCompany.GetQuarterlyIncome(c, q + 1),
		expenses_last_quarter = GSCompany.GetQuarterlyExpenses(c, q + 1),
		delivered_last_quarter = GSCompany.GetQuarterlyCargoDelivered(c, q + 1),
		performance = GSCompany.GetQuarterlyPerformanceRating(c, q + 1),
		vehicles = GSVehicleList().Count(),
		stations = GSStationList(GSStation.STATION_ANY).Count()
	};
}

function LLMBridge::Cmd_towns(a, id)
{
	local out = [];
	local pass = this.CargoByLabel("PASS");
	foreach (t, _ in GSTownList()) {
		local loc = GSTown.GetLocation(t);
		out.append({
			id = t,
			name = GSTown.GetName(t),
			x = GSMap.GetTileX(loc),
			y = GSMap.GetTileY(loc),
			population = GSTown.GetPopulation(t),
			houses = GSTown.GetHouseCount(t),
			passengers_last_month = (pass < 0) ? 0 : GSTown.GetLastMonthProduction(t, pass),
			transported_pct = (pass < 0) ? 0 : GSTown.GetLastMonthTransportedPercentage(t, pass)
		});
	}
	return { ok = true, towns = out };
}

function LLMBridge::Cmd_industries(a, id)
{
	local out = [];
	foreach (i, _ in GSIndustryList()) {
		local loc = GSIndustry.GetLocation(i);
		local prod = [];
		foreach (c, _ in GSCargoList_IndustryProducing(i)) {
			prod.append({
				cargo = this.CargoLabel(c),
				last_month = GSIndustry.GetLastMonthProduction(i, c),
				transported_pct = GSIndustry.GetLastMonthTransportedPercentage(i, c)
			});
		}
		local acc = [];
		foreach (c, _ in GSCargoList_IndustryAccepting(i)) acc.append(this.CargoLabel(c));
		out.append({
			id = i,
			name = GSIndustry.GetName(i),
			type = GSIndustryType.GetName(GSIndustry.GetIndustryType(i)),
			x = GSMap.GetTileX(loc),
			y = GSMap.GetTileY(loc),
			produces = prod,
			accepts = acc
		});
	}
	return { ok = true, industries = out };
}

function LLMBridge::Cmd_tile(a, id)
{
	local cm = GSCompanyMode(this.company);
	local t = this.T(a.x, a.y);
	if (!GSMap.IsValidTile(t)) return { ok = false, error = "tile is outside the map" };
	local kind = this.TileKind(t);
	local res = {
		ok = true,
		x = a.x,
		y = a.y,
		kind = kind,
		owner = this.Owner(t),
		height = GSTile.GetMaxHeight(t),
		flat = (GSTile.GetSlope(t) == GSTile.SLOPE_FLAT),
		buildable = GSTile.IsBuildable(t),
		nearest_town = GSTown.GetName(GSTile.GetClosestTown(t)),
		catchment = this.Catchment(t, 3)
	};
	if (kind == "industry") res.industry <- GSIndustry.GetName(GSIndustry.GetIndustryID(t));
	if (kind == "road_stop" || kind == "airport" || kind == "station") res.station_id <- GSStation.GetStationID(t);
	if (kind == "road") {
		local sx = GSMap.GetMapSizeX();
		res.can_place_bus_stop <- (this.TryStop(t, t + 1, GSRoad.ROADVEHTYPE_BUS) || this.TryStop(t, t + sx, GSRoad.ROADVEHTYPE_BUS));
	}
	return res;
}

/* Tile kinds for a rectangle, one string per row (y), one character per tile (x ascending). */
function LLMBridge::Cmd_scan(a, id)
{
	local cm = GSCompanyMode(this.company);
	local sx = GSMap.GetMapSizeX();
	local sy = GSMap.GetMapSizeY();
	local x0 = max(0, a.x);
	local y0 = max(0, a.y);
	local x1 = min(sx - 1, a.x + a.w - 1);
	local y1 = min(sy - 1, a.y + a.h - 1);
	local glyph = {
		void = " ", clear = ".", trees = "t", water = "~", house = "#", road = "=", industry = "I",
		road_stop = "S", road_depot = "D", airport = "A", station = "s", rail = "r", other = "?"
	};
	local rows = [];
	for (local y = y0; y <= y1; y++) {
		local row = "";
		for (local x = x0; x <= x1; x++) {
			local t = this.T(x, y);
			local k = this.TileKind(t);
			if ((k == "road" || k == "road_stop" || k == "road_depot") && GSTile.GetOwner(t) == this.company) {
				row += (k == "road") ? "+" : glyph[k];
			} else {
				row += glyph[k];
			}
		}
		rows.append(row);
	}
	return { ok = true, x = x0, y = y0, rows = rows };
}

/* North-corner height of every tile, one string per row, chr(48 + height) per tile. */
function LLMBridge::Cmd_heights(a, id)
{
	local sx = GSMap.GetMapSizeX();
	local sy = GSMap.GetMapSizeY();
	local rows = [];
	for (local y = 0; y < sy; y++) {
		local row = "";
		for (local x = 0; x < sx; x++) {
			local t = this.T(x, y);
			local h = GSMap.IsValidTile(t) ? GSTile.GetCornerHeight(t, GSTile.CORNER_N) : 0;
			if (h < 0) h = 0;
			row += (48 + h).tochar();
		}
		rows.append(row);
	}
	return { ok = true, rows = rows };
}

function LLMBridge::Cmd_engines(a, id)
{
	local cm = GSCompanyMode(this.company);
	local air = ("kind" in a && a.kind == "aircraft");
	local out = [];
	foreach (e, _ in GSEngineList(air ? GSVehicle.VT_AIR : GSVehicle.VT_ROAD)) {
		if (!GSEngine.IsBuildable(e)) continue;
		if (!air && GSRoad.GetRoadTramType(GSEngine.GetRoadType(e)) != GSRoad.ROADTRAMTYPES_ROAD) continue;
		local rec = {
			id = e,
			name = GSEngine.GetName(e),
			cargo = this.CargoLabel(GSEngine.GetCargoType(e)),
			capacity = GSEngine.GetCapacity(e),
			max_speed = GSEngine.GetMaxSpeed(e),
			price = GSEngine.GetPrice(e),
			running_cost_per_year = GSEngine.GetRunningCost(e),
			reliability_pct = GSEngine.GetReliability(e)
		};
		if (air) {
			local pt = GSEngine.GetPlaneType(e);
			rec.plane_type <- (pt == GSAirport.PT_BIG_PLANE) ? "big" : ((pt == GSAirport.PT_HELICOPTER) ? "helicopter" : "small");
		} else {
			rec.stop_kind <- (GSRoad.GetRoadVehicleTypeForCargo(GSEngine.GetCargoType(e)) == GSRoad.ROADVEHTYPE_BUS) ? "bus" : "truck";
		}
		out.append(rec);
	}
	return { ok = true, engines = out };
}

function LLMBridge::VehicleCargo(v)
{
	foreach (c in this.cargos) {
		if (GSVehicle.GetCapacity(v, c.id) > 0) return c;
	}
	return null;
}

function LLMBridge::StateName(s)
{
	switch (s) {
		case GSVehicle.VS_RUNNING: return "running";
		case GSVehicle.VS_STOPPED: return "stopped";
		case GSVehicle.VS_IN_DEPOT: return "in_depot";
		case GSVehicle.VS_AT_STATION: return "at_station";
		case GSVehicle.VS_BROKEN: return "broken_down";
		case GSVehicle.VS_CRASHED: return "crashed";
	}
	return "invalid";
}

function LLMBridge::VehicleRec(v)
{
	local loc = GSVehicle.GetLocation(v);
	local c = this.VehicleCargo(v);
	local orders = [];
	local n = GSOrder.GetOrderCount(v);
	for (local i = 0; i < n && i < 8; i++) {
		local d = GSOrder.GetOrderDestination(v, i);
		local st = GSStation.GetStationID(d);
		orders.append(GSStation.IsValidStation(st) ? GSBaseStation.GetName(st) : ("tile " + GSMap.GetTileX(d) + "," + GSMap.GetTileY(d)));
	}
	return {
		id = v,
		name = GSVehicle.GetName(v),
		type = (GSVehicle.GetVehicleType(v) == GSVehicle.VT_AIR) ? "aircraft" : "road",
		engine = GSEngine.GetName(GSVehicle.GetEngineType(v)),
		state = this.StateName(GSVehicle.GetState(v)),
		x = GSMap.GetTileX(loc),
		y = GSMap.GetTileY(loc),
		age_days = GSVehicle.GetAge(v),
		cargo = (c == null) ? "?" : c.label,
		load = (c == null) ? 0 : GSVehicle.GetCargoLoad(v, c.id),
		capacity = (c == null) ? 0 : GSVehicle.GetCapacity(v, c.id),
		profit_this_year = GSVehicle.GetProfitThisYear(v),
		profit_last_year = GSVehicle.GetProfitLastYear(v),
		orders = orders
	};
}

function LLMBridge::Cmd_vehicles(a, id)
{
	local cm = GSCompanyMode(this.company);
	local out = [];
	foreach (v, _ in GSVehicleList()) out.append(this.VehicleRec(v));
	return { ok = true, vehicles = out };
}

function LLMBridge::StationRec(s)
{
	local loc = GSBaseStation.GetLocation(s);
	local cargo = [];
	foreach (c in this.cargos) {
		if (!GSStation.HasCargoRating(s, c.id)) continue;
		cargo.append({ cargo = c.label, waiting = GSStation.GetCargoWaiting(s, c.id), rating_pct = GSStation.GetCargoRating(s, c.id) });
	}
	local kinds = [];
	if (GSStation.HasStationType(s, GSStation.STATION_BUS_STOP)) kinds.append("bus");
	if (GSStation.HasStationType(s, GSStation.STATION_TRUCK_STOP)) kinds.append("truck");
	if (GSStation.HasStationType(s, GSStation.STATION_AIRPORT)) kinds.append("airport");
	return {
		id = s,
		name = GSBaseStation.GetName(s),
		x = GSMap.GetTileX(loc),
		y = GSMap.GetTileY(loc),
		kinds = kinds,
		cargo = cargo,
		vehicles = GSVehicleList_Station(s).Count()
	};
}

function LLMBridge::Cmd_stations(a, id)
{
	local cm = GSCompanyMode(this.company);
	local out = [];
	foreach (s, _ in GSStationList(GSStation.STATION_ANY)) out.append(this.StationRec(s));
	local depots = [];
	foreach (d, _ in GSDepotList(GSTile.TRANSPORT_ROAD)) depots.append({ kind = "road_depot", x = GSMap.GetTileX(d), y = GSMap.GetTileY(d) });
	foreach (d, _ in GSDepotList(GSTile.TRANSPORT_AIR)) depots.append({ kind = "hangar", x = GSMap.GetTileX(d), y = GSMap.GetTileY(d) });
	return { ok = true, stations = out, depots = depots };
}

/* ---------------------------------------------------------- construction */

function LLMBridge::Cmd_build_road(a, id)
{
	local cm = GSCompanyMode(this.company);
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	if (a.x1 != a.x2 && a.y1 != a.y2) return { ok = false, error = "road segments must be straight: x1 == x2 or y1 == y2. Use build_road_route for anything else." };
	if (a.x1 == a.x2 && a.y1 == a.y2) return { ok = false, error = "start and end are the same tile" };
	local acc = GSAccounting();
	if (GSRoad.BuildRoad(this.T(a.x1, a.y1), this.T(a.x2, a.y2))) return { ok = true, cost = acc.GetCosts() };
	local e = this.Err();
	if (e == "ERR_ALREADY_BUILT") return { ok = true, cost = 0, note = "road already existed" };
	return { ok = false, error = e };
}

/*
 * Weighted A* over tiles. Existing connected road is cheap, new road is expensive, so routes
 * reuse what is there. Runs in test mode: nothing is built and no money is spent.
 * Returns an array of tiles from 'from' to 'to', or null.
 */
function LLMBridge::FindRoadPath(from, to, max_nodes)
{
	local tm = GSTestMode();
	local open = GSPriorityQueue();
	local g = {};
	local prev = {};
	local closed = {};
	local sx = GSMap.GetMapSizeX();
	local offs = [1, -1, sx, -sx];
	g[from] <- 0;
	prev[from] <- -1;
	open.Insert(from, GSMap.DistanceManhattan(from, to) * LLMBridge.PF_WEIGHT);
	local expanded = 0;

	while (!open.IsEmpty()) {
		local cur = open.Pop();
		if (cur in closed) continue;
		closed[cur] <- true;
		if (cur == to) {
			local path = [];
			for (local t = to; t != -1; t = prev[t]) path.append(t);
			path.reverse();
			return path;
		}
		if (++expanded > max_nodes) return null;
		local par = prev[cur];
		foreach (o in offs) {
			local nx = cur + o;
			if (nx == par || (nx in closed)) continue;
			if (!GSMap.IsValidTile(nx) || GSMap.DistanceManhattan(cur, nx) != 1) continue;
			local step;
			if (GSRoad.AreRoadTilesConnected(cur, nx)) {
				step = 1;
			} else if ((GSTile.IsBuildable(nx) || GSRoad.IsRoadTile(nx)) &&
					/* 0 = impossible; 1 = fine; 2 = fine but needs a foundation; -1 = no opinion. */
					(par == -1 || GSRoad.CanBuildConnectedRoadPartsHere(cur, par, nx) != 0) &&
					GSRoad.BuildRoad(cur, nx)) {
				step = 4;
				if (GSTile.GetSlope(nx) != GSTile.SLOPE_FLAT) step += 2;
				if (par != -1 && (cur - par) != o) step += 1;
			} else {
				continue;
			}
			local ng = g[cur] + step;
			if (!(nx in g) || ng < g[nx]) {
				g[nx] <- ng;
				prev[nx] <- cur;
				open.Insert(nx, ng + GSMap.DistanceManhattan(nx, to) * LLMBridge.PF_WEIGHT);
			}
		}
	}
	return null;
}

function LLMBridge::Cmd_build_road_route(a, id)
{
	local cm = GSCompanyMode(this.company);
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	local from = this.T(a.x1, a.y1);
	local to = this.T(a.x2, a.y2);
	if (!GSMap.IsValidTile(from) || !GSMap.IsValidTile(to)) return { ok = false, error = "tile is outside the map" };
	if (from == to) return { ok = false, error = "start and end are the same tile" };
	foreach (t in [from, to]) {
		local k = this.TileKind(t);
		if (k != "road" && k != "road_stop" && k != "clear" && k != "trees") {
			return { ok = false, error = "endpoint " + GSMap.GetTileX(t) + "," + GSMap.GetTileY(t) + " is '" + k + "'; endpoints must be a road, one of your road stops, or empty land" };
		}
	}
	local path = this.FindRoadPath(from, to, 30000);
	if (path == null) return { ok = false, error = "no buildable route found (water, steep terrain or buildings in the way; bridges and tunnels are not supported)" };

	local acc = GSAccounting();
	local built = 0;
	local reused = 0;
	local n = path.len();
	local i = 0;
	while (i + 1 < n) {
		/* Extend a straight run as far as it goes and build it with one command. */
		local d = path[i + 1] - path[i];
		local j = i + 1;
		while (j + 1 < n && path[j + 1] - path[j] == d) j++;
		local all_connected = true;
		for (local k = i; k < j; k++) {
			if (!GSRoad.AreRoadTilesConnected(path[k], path[k + 1])) { all_connected = false; break; }
		}
		if (all_connected) {
			reused += j - i;
		} else if (GSRoad.BuildRoad(path[i], path[j]) || this.Err() == "ERR_ALREADY_BUILT") {
			built += j - i;
		} else {
			/* The run failed as a whole; go tile by tile to get as far as possible. */
			for (local k = i; k < j; k++) {
				if (GSRoad.AreRoadTilesConnected(path[k], path[k + 1])) { reused++; continue; }
				if (GSRoad.BuildRoad(path[k], path[k + 1]) || this.Err() == "ERR_ALREADY_BUILT") { built++; continue; }
				return {
					ok = false,
					error = this.Err(),
					stopped_at = [GSMap.GetTileX(path[k]), GSMap.GetTileY(path[k])],
					tiles_built = built,
					cost = acc.GetCosts(),
					note = "route is incomplete"
				};
			}
		}
		i = j;
	}
	return { ok = true, length = n, tiles_built = built, tiles_reused = reused, cost = acc.GetCosts() };
}

function LLMBridge::Cmd_build_road_stop(a, id)
{
	local cm = GSCompanyMode(this.company);
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	local t = this.T(a.x, a.y);
	if (!GSMap.IsValidTile(t)) return { ok = false, error = "tile is outside the map" };
	local truck = ("kind" in a && a.kind == "truck");
	local kind = truck ? GSRoad.ROADVEHTYPE_TRUCK : GSRoad.ROADVEHTYPE_BUS;
	local sx = GSMap.GetMapSizeX();
	local fronts = [t + 1, t + sx];
	if ("axis" in a && a.axis == "y") fronts = [t + sx, t + 1];

	local front = -1;
	local err = "";
	foreach (f in fronts) {
		if (this.TryStop(t, f, kind)) { front = f; break; }
		err = this.Err();
	}
	if (front == -1) {
		local k = this.TileKind(t);
		if (k == "road") err = "this road tile is a junction, bend or dead end";
		else if (k == "road_stop") err = "there is already a stop on this tile";
		else if (k != "clear" && k != "trees") err = "tile is '" + k + "'";
		local res = { ok = false, error = err, tile_kind = k };
		res.hint <- "a stop needs a straight piece of road, or flat empty land";
		if ("hints" in a && a.hints) res.nearby_valid_tiles <- this.NearbyStopSites(t, kind, 3, 6);
		return res;
	}
	local acc = GSAccounting();
	if (!GSRoad.BuildDriveThroughRoadStation(t, front, kind, GSStation.STATION_NEW)) return { ok = false, error = this.Err() };
	local st = GSStation.GetStationID(t);
	local res = this.Catchment(t, GSStation.GetCoverageRadius(truck ? GSStation.STATION_TRUCK_STOP : GSStation.STATION_BUS_STOP));
	res.ok <- true;
	res.station_id <- st;
	res.name <- GSBaseStation.GetName(st);
	res.kind <- truck ? "truck" : "bus";
	res.cost <- acc.GetCosts();
	return res;
}

function LLMBridge::Cmd_build_road_depot(a, id)
{
	local cm = GSCompanyMode(this.company);
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	local t = this.T(a.x, a.y);
	if (!GSMap.IsValidTile(t)) return { ok = false, error = "tile is outside the map" };
	local front = GSTile.IsBuildable(t) ? this.DepotFront(t) : -1;
	if (front == -1) {
		local res = { ok = false, tile_kind = this.TileKind(t) };
		res.error <- GSTile.IsBuildable(t) ? "no road leads onto this tile" : ("tile is '" + res.tile_kind + "'");
		res.hint <- "a depot needs empty land whose N/E/S/W neighbour is a road tile, or the open end of one of your stops";
		if ("hints" in a && a.hints) res.nearby_valid_tiles <- this.NearbyDepotSites(t, 8, 6);
		return res;
	}
	local acc = GSAccounting();
	if (!GSRoad.BuildRoadDepot(t, front)) return { ok = false, error = this.Err() };
	if (!GSRoad.AreRoadTilesConnected(t, front)) {
		if (!GSRoad.BuildRoad(t, front) && this.Err() != "ERR_ALREADY_BUILT") {
			local e = this.Err();
			GSRoad.RemoveRoadDepot(t);
			return { ok = false, error = "depot could not be connected to the road: " + e };
		}
	}
	return { ok = true, x = a.x, y = a.y, faces = [GSMap.GetTileX(front), GSMap.GetTileY(front)], cost = acc.GetCosts() };
}

function LLMBridge::AirportType(name)
{
	switch (name) {
		case "small": return GSAirport.AT_SMALL;
		case "commuter": return GSAirport.AT_COMMUTER;
		case "large": return GSAirport.AT_LARGE;
		case "metropolitan": return GSAirport.AT_METROPOLITAN;
		case "international": return GSAirport.AT_INTERNATIONAL;
	}
	return GSAirport.AT_INVALID;
}

function LLMBridge::Cmd_build_airport(a, id)
{
	local cm = GSCompanyMode(this.company);
	local type = this.AirportType(a.type);
	if (type == GSAirport.AT_INVALID || !GSAirport.IsValidAirportType(type)) return { ok = false, error = "airport type not available; use small, commuter, large, metropolitan or international" };
	local t = this.T(a.x, a.y);
	local w = GSAirport.GetAirportWidth(type);
	local h = GSAirport.GetAirportHeight(type);
	local acc = GSAccounting();
	if (!GSAirport.BuildAirport(t, type, GSStation.STATION_NEW)) {
		return { ok = false, error = this.Err(), hint = "needs a flat, empty " + w + "x" + h + " rectangle with (x,y) as its lowest-coordinate corner; price " + GSAirport.GetPrice(type) };
	}
	local st = GSStation.GetStationID(t);
	local hangar = GSAirport.GetHangarOfAirport(t);
	local res = this.Catchment(t, GSAirport.GetAirportCoverageRadius(type));
	res.ok <- true;
	res.station_id <- st;
	res.name <- GSBaseStation.GetName(st);
	res.size <- [w, h];
	res.hangar <- [GSMap.GetTileX(hangar), GSMap.GetTileY(hangar)];
	res.cost <- acc.GetCosts();
	return res;
}

function LLMBridge::Cmd_remove(a, id)
{
	local cm = GSCompanyMode(this.company);
	GSRoad.SetCurrentRoadType(GSRoad.ROADTYPE_ROAD);
	local t = this.T(a.x, a.y);
	local k = this.TileKind(t);
	local acc = GSAccounting();
	local ok;
	if (k == "road_stop") ok = GSRoad.RemoveRoadStation(t);
	else if (k == "road_depot") ok = GSRoad.RemoveRoadDepot(t);
	else if (k == "airport") ok = GSAirport.RemoveAirport(t);
	else ok = GSTile.DemolishTile(t);
	if (!ok) return { ok = false, error = this.Err(), tile_kind = k };
	return { ok = true, removed = k, cost = acc.GetCosts() };
}

/* -------------------------------------------------------------- vehicles */

function LLMBridge::DepotTile(x, y, air)
{
	local t = this.T(x, y);
	if (air) {
		if (GSAirport.IsHangarTile(t)) return t;
		if (GSAirport.IsAirportTile(t)) return GSAirport.GetHangarOfAirport(t);
		return -1;
	}
	return GSRoad.IsRoadDepotTile(t) ? t : -1;
}

function LLMBridge::Cmd_buy_vehicle(a, id)
{
	local cm = GSCompanyMode(this.company);
	local e = a.engine_id;
	if (!GSEngine.IsValidEngine(e) || !GSEngine.IsBuildable(e)) return { ok = false, error = "engine_id is not a buildable engine; call list_engines" };
	local air = (GSEngine.GetVehicleType(e) == GSVehicle.VT_AIR);
	local depot = this.DepotTile(a.depot_x, a.depot_y, air);
	if (depot == -1) return { ok = false, error = air ? "no airport of yours at that tile" : "no road depot of yours at that tile; build one with build_road_depot" };
	local acc = GSAccounting();
	local v;
	if ("cargo" in a && a.cargo != null && a.cargo != this.CargoLabel(GSEngine.GetCargoType(e))) {
		local c = this.CargoByLabel(a.cargo);
		if (c == -1) return { ok = false, error = "unknown cargo label '" + a.cargo + "'" };
		if (!GSEngine.CanRefitCargo(e, c)) return { ok = false, error = "this engine cannot carry " + a.cargo };
		v = GSVehicle.BuildVehicleWithRefit(depot, e, c);
	} else {
		v = GSVehicle.BuildVehicle(depot, e);
	}
	if (!GSVehicle.IsValidVehicle(v)) return { ok = false, error = this.Err() };
	local res = this.VehicleRec(v);
	res.ok <- true;
	res.cost <- acc.GetCosts();
	res.note <- "vehicle is stopped in the depot: give it orders with set_orders, then start_vehicle";
	return res;
}

function LLMBridge::OrderTile(v, o)
{
	if ("station_id" in o) {
		local st = o.station_id;
		if (!GSStation.IsValidStation(st)) return -1;
		local want;
		if (GSVehicle.GetVehicleType(v) == GSVehicle.VT_AIR) {
			want = GSStation.STATION_AIRPORT;
		} else {
			local c = this.VehicleCargo(v);
			local bus = (c != null && GSRoad.GetRoadVehicleTypeForCargo(c.id) == GSRoad.ROADVEHTYPE_BUS);
			want = bus ? GSStation.STATION_BUS_STOP : GSStation.STATION_TRUCK_STOP;
		}
		local tl = GSTileList_StationType(st, want);
		return tl.IsEmpty() ? -1 : tl.Begin();
	}
	return this.T(o.x, o.y);
}

function LLMBridge::Cmd_set_orders(a, id)
{
	local cm = GSCompanyMode(this.company);
	local v = a.vehicle_id;
	if (!GSVehicle.IsValidVehicle(v)) return { ok = false, error = "no such vehicle" };
	if (a.orders.len() < 2) return { ok = false, error = "give at least two orders" };
	local road = (GSVehicle.GetVehicleType(v) == GSVehicle.VT_ROAD);

	/* Resolve and validate everything before touching the existing orders. */
	local plan = [];
	foreach (i, o in a.orders) {
		local dest = this.OrderTile(v, o);
		local flags = road ? GSOrder.OF_NON_STOP_INTERMEDIATE : GSOrder.OF_NONE;
		if ("flags" in o) {
			foreach (f in o.flags) {
				if (f == "full_load") flags = flags | GSOrder.OF_FULL_LOAD_ANY;
				else if (f == "unload") flags = flags | GSOrder.OF_UNLOAD;
				else if (f == "transfer") flags = flags | GSOrder.OF_TRANSFER;
				else if (f == "no_load") flags = flags | GSOrder.OF_NO_LOAD;
				else if (f == "no_unload") flags = flags | GSOrder.OF_NO_UNLOAD;
				else return { ok = false, error = "unknown order flag '" + f + "'" };
			}
		}
		if (dest == -1 || !GSOrder.AreOrderFlagsValid(dest, flags)) {
			local c = this.VehicleCargo(v);
			return {
				ok = false,
				error = "order " + i + " is not a valid stop for this vehicle",
				hint = road ? ("this vehicle carries " + (c == null ? "?" : c.label) + " and needs a " +
					((c != null && GSRoad.GetRoadVehicleTypeForCargo(c.id) == GSRoad.ROADVEHTYPE_BUS) ? "bus" : "truck") +
					" stop of yours at that location") : "aircraft need one of your airports"
			};
		}
		plan.append([dest, flags]);
	}
	while (GSOrder.GetOrderCount(v) > 0) {
		if (!GSOrder.RemoveOrder(v, 0)) return { ok = false, error = "could not clear old orders: " + this.Err() };
	}
	foreach (i, p in plan) {
		if (!GSOrder.AppendOrder(v, p[0], p[1])) return { ok = false, error = "order " + i + ": " + this.Err() };
	}
	local res = this.VehicleRec(v);
	res.ok <- true;
	return res;
}

function LLMBridge::Cmd_start_vehicle(a, id)
{
	local cm = GSCompanyMode(this.company);
	local v = a.vehicle_id;
	if (!GSVehicle.IsValidVehicle(v)) return { ok = false, error = "no such vehicle" };
	local s = GSVehicle.GetState(v);
	if (s != GSVehicle.VS_STOPPED && s != GSVehicle.VS_IN_DEPOT) return { ok = true, state = this.StateName(s), note = "already running" };
	if (GSOrder.GetOrderCount(v) < 2) return { ok = false, error = "vehicle has no orders; call set_orders first" };
	if (!GSVehicle.StartStopVehicle(v)) return { ok = false, error = this.Err() };
	return { ok = true, state = "running" };
}

function LLMBridge::Cmd_clone_vehicle(a, id)
{
	local cm = GSCompanyMode(this.company);
	local v = a.vehicle_id;
	if (!GSVehicle.IsValidVehicle(v)) return { ok = false, error = "no such vehicle" };
	local air = (GSVehicle.GetVehicleType(v) == GSVehicle.VT_AIR);
	local depot = this.DepotTile(a.depot_x, a.depot_y, air);
	if (depot == -1) return { ok = false, error = "no depot of yours at that tile" };
	local count = ("count" in a) ? a.count : 1;
	if (count < 1 || count > 10) return { ok = false, error = "count must be 1..10" };
	local acc = GSAccounting();
	local made = [];
	for (local i = 0; i < count; i++) {
		local n = GSVehicle.CloneVehicle(depot, v, true);
		if (!GSVehicle.IsValidVehicle(n)) {
			if (made.len() == 0) return { ok = false, error = this.Err() };
			break;
		}
		GSVehicle.StartStopVehicle(n);
		made.append(n);
	}
	return { ok = true, vehicle_ids = made, started = true, cost = acc.GetCosts(), stopped_early = (made.len() < count) ? this.Err() : null };
}

function LLMBridge::Cmd_sell_vehicle(a, id)
{
	local cm = GSCompanyMode(this.company);
	local v = a.vehicle_id;
	if (!GSVehicle.IsValidVehicle(v)) return { ok = false, error = "no such vehicle" };
	if (GSVehicle.IsStoppedInDepot(v)) {
		local acc = GSAccounting();
		if (!GSVehicle.SellVehicle(v)) return { ok = false, error = this.Err() };
		return { ok = true, sold = true, refund = -acc.GetCosts() };
	}
	if (!GSVehicle.SendVehicleToDepot(v)) return { ok = false, error = this.Err() };
	return { ok = true, sold = false, note = "vehicle was sent to a depot; let time pass, then call sell_vehicle again" };
}

function LLMBridge::Cmd_set_loan(a, id)
{
	local cm = GSCompanyMode(this.company);
	local step = GSCompany.GetLoanInterval();
	local amount = a.amount - (a.amount % step);
	if (amount < 0 || amount > GSCompany.GetMaxLoanAmount()) return { ok = false, error = "loan must be between 0 and " + GSCompany.GetMaxLoanAmount() };
	if (!GSCompany.SetLoanAmount(amount)) return { ok = false, error = this.Err() };
	return { ok = true, loan = GSCompany.GetLoanAmount(), money = GSCompany.GetBankBalance(this.company) };
}
