"""The tools the harness can run, and the JSON that describes them to the model.

Every tool returns a JSON string. On failure it returns {"error": ..., "how_to_fix": ...}
so the model can recover on its own: retry with better arguments, or explain to the user.
"""

import json
import math
import re
import time

import requests

# NYC Open Data film permits. Free, no key at low volume.
FILM_PERMITS_URL = "https://data.cityofnewyork.us/resource/tg4x-b46p.json"
BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]

# OpenStreetMap services. All free with no key, but their usage policies ask for a
# User-Agent naming the app, and Nominatim allows at most one request per second.
OSM_HEADERS = {"User-Agent": "project-1-dgr2116 (class project)"}
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"  # addresses -> coordinates
OVERPASS_URLS = [  # map queries (street intersections); the main server is often briefly busy
    "https://overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
ROUTING_URL = "https://routing.openstreetmap.de/routed-{profile}/route/v1/driving/{coords}"  # OSRM
MODES = {"walk": "foot", "bike": "bike", "drive": "car"}

# Rough (south, west, north, east) boxes, so an intersection search stays in one borough.
BOROUGH_BOXES = {
    "Manhattan": (40.698, -74.020, 40.882, -73.907),
    "Brooklyn": (40.570, -74.042, 40.739, -73.833),
    "Queens": (40.541, -73.962, 40.801, -73.700),
    "Bronx": (40.785, -73.933, 40.917, -73.765),
    "Staten Island": (40.496, -74.255, 40.651, -74.052),
}
NYC_BOX = (40.496, -74.255, 40.917, -73.700)


class ToolError(Exception):
    """A failure the model can act on: what went wrong, and what to try instead."""

    def __init__(self, message: str, how_to_fix: str):
        super().__init__(message)
        self.message, self.how_to_fix = message, how_to_fix


def error(message: str, how_to_fix: str) -> str:
    return json.dumps({"error": message, "how_to_fix": how_to_fix})


# --- Shared helpers ---


def match_borough(text: str) -> str | None:
    """'the bronx' -> 'Bronx'; None if it isn't a borough."""
    text = re.sub(r"^the\s+", "", text.strip(), flags=re.I).lower()
    return next((b for b in BOROUGHS if b.lower() == text), None)


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> int:
    """Straight-line distance in meters (haversine)."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return round(2 * 6_371_000 * math.asin(math.sqrt(a)))


_last_nominatim = 0.0


def nominatim(params: dict) -> list[dict]:
    """Call Nominatim, waiting if needed to stay under one request per second."""
    global _last_nominatim
    time.sleep(max(0.0, 1.0 - (time.monotonic() - _last_nominatim)))
    _last_nominatim = time.monotonic()
    resp = requests.get(NOMINATIM_URL, params={"format": "jsonv2", **params}, headers=OSM_HEADERS, timeout=10)
    resp.raise_for_status()
    return resp.json()


def overpass(query: str) -> list[dict]:
    """Run an Overpass query: try the main server twice, then a mirror."""
    for attempt, url in enumerate(OVERPASS_URLS):
        if attempt:
            time.sleep(2)
        try:
            resp = requests.post(url, data={"data": f"[out:json][timeout:15];{query}"}, headers=OSM_HEADERS, timeout=20)
            resp.raise_for_status()  # busy servers answer 429 or 504
            return resp.json()["elements"]
        except (requests.RequestException, ValueError, KeyError):
            continue
    raise ToolError(
        "The OpenStreetMap service for finding intersections is busy right now.",
        "Wait a few seconds and call the tool again, or use a street address or landmark instead of an intersection.",
    )


def ordinal(n: str) -> str:
    """'43' -> '43rd'."""
    suffix = "th" if 11 <= int(n) % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(int(n) % 10, "th")
    return n + suffix


def osm_street_pattern(street: str) -> str:
    """'WEST 43 STREET' or 'W 43rd St' -> a regex matching OpenStreetMap's 'West 43rd Street'."""
    words = re.findall(r"[a-z0-9]+", street.lower())
    expand = {"w": "west", "e": "east", "n": "north", "s": "south", "ave": "avenue", "av": "avenue",
              "blvd": "boulevard", "pl": "place", "rd": "road", "dr": "drive", "pkwy": "parkway", "ln": "lane"}
    for i, w in enumerate(words):
        if w == "st":
            words[i] = "saint" if i == 0 else "street"  # 'St Nicholas Ave' vs '43rd St'
        elif m := re.fullmatch(r"(\d+)(st|nd|rd|th)?", w):
            words[i] = ordinal(m.group(1))
        else:
            words[i] = expand.get(w, w)
    # Letters and digits only, so nothing needs escaping inside the Overpass query.
    pattern = "[^a-z0-9]+".join(words)
    if pattern == "6th[^a-z0-9]+avenue":
        pattern = "(6th[^a-z0-9]+avenue|avenue of the americas)"
    return f"^{pattern}$"


def locate(place: str) -> tuple[float, float, str]:
    """Coordinates for an address, a landmark, or an NYC intersection ('A & B, Borough')."""
    place = place.strip()
    if "&" not in place:
        results = nominatim({"q": place, "limit": 1})
        if not results:
            raise ToolError(
                f"No place called '{place}' was found.",
                "Use a full street address with the city (e.g. '350 5th Ave, New York, NY') or a well-known "
                "landmark. For an NYC intersection, write 'Street & Cross Street, Borough'.",
            )
        return float(results[0]["lat"]), float(results[0]["lon"]), results[0]["display_name"]

    # Intersection: 'West 43 Street & 5 Avenue, Manhattan'. The borough is optional.
    streets, _, borough_text = place.partition(",")
    borough = match_borough(borough_text) if borough_text else None
    if borough_text and not borough:
        raise ToolError(
            f"'{borough_text.strip()}' is not an NYC borough. Intersections only work inside New York City.",
            f"End the intersection with one of {BOROUGHS}, or use a street address with the city instead.",
        )
    a, _, b = (s.strip() for s in streets.partition("&"))
    if not (a and b):
        raise ToolError(f"'{place}' is not a valid intersection.", "Write it as 'Street & Cross Street, Borough'.")

    box = BOROUGH_BOXES.get(borough, NYC_BOX)
    ways = [f'way["highway"]["name"~"{osm_street_pattern(s)}",i]{box}' for s in (a, b)]
    nodes = overpass(f"{ways[0]}->.a;{ways[1]}->.b;node(w.a)(w.b);out 1;")
    if not nodes:
        raise ToolError(
            f"Could not find where '{a}' meets '{b}' in {borough or 'NYC'}.",
            "Check that the two streets actually cross and spell out full names (e.g. 'West 43 Street', not "
            "'43rd'). Add the borough after a comma, or use a nearby street address instead.",
        )
    return nodes[0]["lat"], nodes[0]["lon"], f"{a} & {b}" + (f", {borough}" if borough else "")


# --- Tool 1: film shoots ---


def normalize_street(street: str) -> str:
    """'W 43rd St' -> '43 STREET', the way the permits spell streets."""
    s = street.upper()
    s = re.sub(r"^(W|WEST|E|EAST|N|NORTH|S|SOUTH)\s+", "", s)  # permits pad these with spaces
    s = re.sub(r"(\d+)(ST|ND|RD|TH)\b", r"\1", s)  # 43RD -> 43
    s = re.sub(r"\bST\b\.?", "STREET", s)
    s = re.sub(r"\bAVE?\b\.?", "AVENUE", s)
    return s.strip()


def find_film_shoots(borough: str | None = None, street: str | None = None, zipcode: str | None = None) -> str:
    """Find the most recent NYC film and TV shoots on a street, in a ZIP code, or in a borough."""
    if not (borough or street or zipcode):
        raise ToolError("No search filter was given.", "Pass at least one of borough, street, or zipcode.")

    # SoQL is SQL-like. Double any single quote so user text can't break the query.
    filters = ["enddatetime IS NOT NULL"]
    if borough:
        match = match_borough(borough)
        if not match:
            raise ToolError(f"'{borough}' is not an NYC borough.", f"Use one of {BOROUGHS}, or leave borough out.")
        filters += [f"borough = '{match}'"]
    if street:
        street = normalize_street(street).replace("'", "''")
        filters += [f"upper(parkingheld) like '%{street}%'"]
    if zipcode:
        zipcode = zipcode.strip()
        if not re.fullmatch(r"\d{5}", zipcode):
            raise ToolError(f"'{zipcode}' is not a 5-digit ZIP code.", "Pass a ZIP like '10036', or leave zipcode out.")
        filters += [f"zipcode_s like '%{zipcode}%'"]

    rows = requests.get(
        FILM_PERMITS_URL,
        params={"$where": " AND ".join(filters), "$order": "enddatetime DESC", "$limit": 100},
        timeout=10,
    ).json()
    newest = requests.get(
        FILM_PERMITS_URL, params={"$select": "max(enddatetime) AS newest"}, timeout=10
    ).json()[0]["newest"]

    # SoQL's like is a substring match ('43 STREET' hits '143 STREET'), so recheck whole words.
    if street:
        pattern = re.compile(r"\b" + re.escape(street.replace("''", "'")) + r"\b")
        rows = [r for r in rows if pattern.search(r.get("parkingheld", ""))]

    # The city's feed can lag by months; tell the model so it doesn't say "this week".
    result = {
        "data_current_through": newest[:10],
        "shoots": [
            {
                "dates": f"{(r.get('startdatetime') or '?')[:10]} to {r['enddatetime'][:10]}",
                "type": f"{r.get('category')} / {r.get('subcategoryname')}",
                "borough": r.get("borough"),
                "streets": " ".join(r.get("parkingheld", "").split()),
            }
            for r in rows[:5]
        ],
    }
    if not result["shoots"]:
        result["how_to_fix"] = "Nothing matched every filter. Try fewer filters: just the street, or just the ZIP code or borough."
    return json.dumps(result)


# --- Tool 2: coffee nearby ---


def find_coffee_near(location: str, radius_m: int = 400) -> str:
    """Find coffee shops near an address, landmark, or NYC intersection, closest first."""
    try:
        radius_m = max(100, min(int(radius_m), 1500))
    except (TypeError, ValueError):
        raise ToolError(f"radius_m must be a number of meters, not '{radius_m}'.", "Pass an integer from 100 to 1500.")
    lat, lon, label = locate(location)

    # Search a box around the point, then keep only cafes inside the circle.
    dlat = radius_m / 111_320
    dlon = radius_m / (111_320 * math.cos(math.radians(lat)))
    places = nominatim({
        "amenity": "cafe",
        "viewbox": f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}",
        "bounded": 1,
        "limit": 40,
        "addressdetails": 1,
    })

    shops = []
    for p in places:
        meters = distance_m(lat, lon, float(p["lat"]), float(p["lon"]))
        if not p.get("name") or meters > radius_m:
            continue
        addr = p.get("address", {})
        shops += [{
            "name": p["name"],
            "address": " ".join(filter(None, [addr.get("house_number"), addr.get("road")])) or None,
            "meters": meters,
            "walk_min": max(1, round(meters / 80)),  # about 80 m per minute walking
        }]
    shops.sort(key=lambda s: s["meters"])

    result = {"searched_near": label, "radius_m": radius_m, "coffee_shops": shops[:8]}
    if not shops:
        result["how_to_fix"] = f"No cafes within {radius_m} m. Call again with a larger radius_m (up to 1500)."
    return json.dumps(result)


# --- Tool 3: routes ---


def route_one(mode: str, start: tuple, end: tuple) -> dict:
    """One OSRM route: minutes, km, and turn-by-turn steps."""
    coords = f"{start[1]},{start[0]};{end[1]},{end[0]}"  # OSRM wants lon,lat
    resp = requests.get(
        ROUTING_URL.format(profile=MODES[mode], coords=coords),
        params={"overview": "false", "steps": "true"}, headers=OSM_HEADERS, timeout=15,
    )
    data = resp.json()
    if data.get("code") != "Ok":
        return {"mode": mode, "error": data.get("message", "No route found.")}

    # OSRM snaps each end to the nearest road it knows, even one an ocean away.
    snapped = max(w["distance"] for w in data["waypoints"])
    if snapped > 500:
        return {"mode": mode, "error": f"An endpoint is {round(snapped / 1000)} km from any road this router can use."}

    route = data["routes"][0]
    steps = []
    for s in route["legs"][0]["steps"]:
        m = s["maneuver"]
        action = " ".join(filter(None, [m["type"], m.get("modifier")]))
        onto = f" onto {s['name']}" if s["name"] else ""
        steps += [f"{action}{onto} ({round(s['distance'])} m)"]
    return {"mode": mode, "minutes": round(route["duration"] / 60), "km": round(route["distance"] / 1000, 1), "steps": steps[:15]}


def get_route(origin: str, destination: str, mode: str = "compare") -> str:
    """Route between two places by walking, biking, or driving, or compare all three."""
    if mode != "compare" and mode not in MODES:
        raise ToolError(f"Unknown mode '{mode}'.", f"Use 'compare' or one of {list(MODES)}. Subway and bus are not supported.")

    start, end = locate(origin), locate(destination)
    routes = [route_one(m, start, end) for m in (list(MODES) if mode == "compare" else [mode])]

    ok = [r for r in routes if "error" not in r]
    if not ok:
        raise ToolError(
            f"No route found from '{start[2]}' to '{end[2]}': {routes[0]['error']}",
            "Both places must be reachable over land. Check that each place was found where the user meant.",
        )
    fastest = min(ok, key=lambda r: r["minutes"])
    for r in routes:
        if r is not fastest:
            r.pop("steps", None)  # keep the reply short: directions only for the fastest

    return json.dumps({
        "from": start[2],
        "to": end[2],
        "fastest": fastest["mode"],
        "routes": routes,
        "note": "Drive times assume no traffic or parking. Subway and bus are not included.",
    })


# What the model sees: the "set notes" in the screenplay.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "find_film_shoots",
            "description": (
                "Find the most recent permitted film, TV, and commercial shoots in New York City, "
                "from the city's film permit records. Filter by any mix of borough, street, and ZIP "
                "code. Each shoot lists its dates and the street blocks it held. The city's data can "
                "be months old, so the result says the date it runs through; tell the user that date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "borough": {"type": "string", "enum": BOROUGHS, "description": "NYC borough to search"},
                    "street": {"type": "string", "description": "One street name, e.g. 'West 43 Street', 'Kent Ave', 'Broadway'"},
                    "zipcode": {"type": "string", "description": "5-digit NYC ZIP code, e.g. '10036'"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_coffee_near",
            "description": (
                "Find coffee shops and cafes near a location, closest first, with walking minutes. "
                "The location can be a street address, a landmark, or a New York City intersection "
                "written 'Street & Cross Street, Borough'. To search near a film shoot listed as "
                "'WEST 43 STREET between 5 AVENUE and 6 AVENUE' in Manhattan, pass "
                "'West 43 Street & 5 Avenue, Manhattan'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "e.g. '350 5th Ave, New York, NY', 'Pike Place Market, Seattle', or 'Kent Street & West Street, Brooklyn'",
                    },
                    "radius_m": {"type": "integer", "description": "Search radius in meters, 100 to 1500. Default 400, about a 5 minute walk."},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_route",
            "description": (
                "Get the route between two places by walking, biking, or driving, with travel time, "
                "distance, and turn-by-turn directions for the fastest option. Each end can be a street "
                "address, a landmark, or an NYC intersection written 'Street & Cross Street, Borough'. "
                "Subway and bus are not supported, and drive times ignore traffic; say so when it matters."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {"type": "string", "description": "Where the trip starts, e.g. 'Empire State Building, New York'"},
                    "destination": {"type": "string", "description": "Where the trip ends, e.g. 'West 43 Street & 5 Avenue, Manhattan'"},
                    "mode": {
                        "type": "string",
                        "enum": ["compare", *MODES],
                        "description": "'compare' (default) tries walk, bike, and drive and picks the fastest",
                    },
                },
                "required": ["origin", "destination"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {"find_film_shoots": find_film_shoots, "find_coffee_near": find_coffee_near, "get_route": get_route}


def run_tool(name: str, args: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop."""
    if name not in TOOL_MAP:
        return error(f"Unknown tool '{name}'.", f"Call one of {list(TOOL_MAP)}.")
    try:
        return TOOL_MAP[name](**args)
    except ToolError as e:
        return error(e.message, e.how_to_fix)
    except TypeError as e:
        return error(f"Bad arguments for {name}: {e}", "Check the parameter names and required fields, then call it again.")
    except requests.RequestException as e:
        return error(
            f"Could not reach the data service behind {name}: {e}",
            "This is usually temporary. Try once more; if it fails again, tell the user the service is down.",
        )
    except (ValueError, KeyError, IndexError) as e:
        # The service answered, but not with what we expected. Retrying the same call won't help.
        return error(
            f"{name} got an unexpected response: {type(e).__name__}: {e}",
            "Don't retry with the same arguments. Tell the user this lookup failed.",
        )
