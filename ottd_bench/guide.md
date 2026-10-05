# OpenTTD field guide

## How money is made
- A vehicle is paid when it unloads cargo at a station that accepts it. Payment grows with
  distance travelled and amount carried, and shrinks if the trip is slow.
- Very short routes (under about 15 tiles) barely cover running costs. 25 to 60 tiles between
  stops is a good range for road vehicles.
- Every vehicle has a yearly running cost, and the loan charges interest. A vehicle with
  negative profit after a full year should get a better route or be sold.

## Cargo
- PASS (passengers) and MAIL come from town buildings and are accepted by town buildings.
  A stop in a larger town, close to its centre, has more passengers.
- Industries produce raw cargo (COAL, IORE, WOOD, GRAI, LVST, OIL_) that must go to an industry
  that accepts it (coal to a power station, iron ore to a steel mill, wood to a sawmill,
  grain and livestock to a factory, oil to a refinery). get_info("industries") lists both sides.
- Processing industries turn deliveries into new cargo (STEL, GOOD). Towns accept GOOD.
- Buses carry PASS and use bus stops. Every other road vehicle is a truck built for one cargo
  and uses truck stops.

## Stations
- A stop serves tiles within 3 tiles of it. build_road_stop and inspect_tile report what a stop
  at a tile accepts and supplies; if the cargo you want is not listed, move closer.
- A stop goes on a straight road tile (not a junction, bend or dead end) or on flat empty land.
  For an industry, build the stop on empty land beside it, then connect it with build_road.
- Cargo only appears at a station after a vehicle has loaded there once.
- Station rating falls when vehicles come rarely; low rating means less cargo. More vehicles
  on a busy route raise it.

## Roads
- build_road connects two tiles and reuses existing roads. It cannot cross water or very steep
  land, and it will not demolish buildings. If it fails, look at the terrain between the two
  ends and try a different end tile, or connect through an intermediate tile.
- Roads inside towns belong to the town; you may use them and place stops on them.

## Depots and vehicles
- A depot needs empty flat land touching a road. Vehicles leave through the side facing that road.
- A depot only helps if it is connected by road to the route, so build it next to a road that is
  part of the route (for example beside one of the stops).
- buy_vehicle with station_ids buys, routes and starts in one call. clone_vehicle adds more.
- A vehicle shown as "broken_down" recovers by itself. A "crashed" vehicle is lost.

## Aircraft (only in scenarios that allow them)
- Airports need a large flat empty rectangle near a town and are expensive, but aircraft earn
  far more per trip over long distances. Small aircraft only for small and commuter airports.
- Buy aircraft with the airport's own coordinates as depot_x, depot_y.

## Reading the pictures
- look: the real game view. White labels "x,y" sit on the tile they name. Moving down-left
  increases x; moving down-right increases y. Grey strips are roads, blue is water, dark green
  or brown patches are fields and trees (trees can be built over, buildings cannot).
- view_map: top-down with ordinary axes. x is labelled along the top edge and grows to the right;
  y is labelled along the left edge and grows downward. It is a mirror image of the look view,
  so compare the two by their coordinate labels, not by left and right.
- Things you own are tagged in cyan; your roads and vehicles are yellow.

## Common errors
- "junction, bend or dead end": pick a road tile with road on exactly two opposite sides.
- "no road leads onto this tile": depots must be directly beside a road (not diagonal to it), or
  at the open end of a stop, in line with the road through it.
- "ERR_NOT_ENOUGH_CASH": raise the loan with set_loan, or wait for income.
- "ERR_LOCAL_AUTHORITY_REFUSES": the town dislikes you (too much demolition); build elsewhere.
- "not a valid stop for this vehicle": buses need bus stops, trucks need truck stops.
- "no buildable route found": water or buildings block every path between the two ends.
