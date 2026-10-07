"""The tools the harness can run, and the JSON that describes them to the model.

Pace Coach's tools. Three call external services (weather, maps, food data) and two are
calculators, because models are unreliable at multi-step arithmetic.

Every tool returns a JSON string. On failure it returns {"error": ..., "how_to_fix": ...}
so the model can recover on its own: retry with better arguments, or explain to the user.
"""

import datetime as dt
import itertools
import json
import math
import re
import time
from urllib.parse import urlencode

import requests

# Weather and air quality: Open-Meteo. Free, no key.
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

# Maps: OpenStreetMap services. Free with no key, but their usage policies ask for a
# User-Agent naming the app, and Nominatim allows at most one request per second.
OSM_HEADERS = {"User-Agent": "project-1-dgr2116 (class project)"}
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"  # place -> coordinates
OVERPASS_URLS = [  # fountains anywhere in the world; the main server is often briefly busy
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
# Inside NYC, the Parks Department's own fountain list is faster, more reliable, and has names.
NYC_FOUNTAINS_URL = "https://data.cityofnewyork.us/resource/qnv7-p7a2.json"
NYC_BOX = (40.49, -74.26, 40.92, -73.70)  # south, west, north, east
FOOT_ROUTE_URL = "https://routing.openstreetmap.de/routed-foot/route/v1/driving/{coords}"  # OSRM

# Gel and food nutrition: Open Food Facts. Free, no key.
FOOD_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"

METERS_PER_MILE = 1609.34


class ToolError(Exception):
    """A failure the model can act on: what went wrong, and what to try instead."""

    def __init__(self, message: str, how_to_fix: str):
        super().__init__(message)
        self.message, self.how_to_fix = message, how_to_fix


def error(message: str, how_to_fix: str) -> str:
    return json.dumps({"error": message, "how_to_fix": how_to_fix})


# --- Shared helpers ---


def number(name: str, value, low: float, high: float) -> float:
    """Parse a numeric argument and range-check it, so bad input becomes an actionable error."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ToolError(f"{name} must be a number, not '{value}'.", f"Pass {name} as a number from {low} to {high}.")
    if not low <= value <= high:
        raise ToolError(f"{name}={value:g} is outside the expected range.", f"Expected {low} to {high}. Ask the user to double-check it.")
    return value


def clock(minutes: float) -> str:
    """75.5 -> '1:15' (hours:minutes)."""
    minutes = round(minutes)
    return f"{minutes // 60}:{minutes % 60:02d}"


def race_clock(seconds: float) -> str:
    """3000 -> '50:00'; 6330 -> '1:45:30'."""
    s = round(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def pace(seconds_per_mile: float) -> str:
    """545 -> '9:05/mi'."""
    s = round(seconds_per_mile)
    return f"{s // 60}:{s % 60:02d}/mi"


def distance_m(a: tuple, b: tuple) -> float:
    """Straight-line distance in meters between (lat, lon) points (haversine)."""
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(b[1] - a[1]) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def bearing(a: tuple, b: tuple) -> float:
    """Compass bearing in degrees from a to b."""
    dlon = math.radians(b[1] - a[1]) * math.cos(math.radians(a[0]))
    return math.degrees(math.atan2(dlon, math.radians(b[0] - a[0]))) % 360


_last_nominatim = 0.0


def locate(place: str) -> tuple[float, float, str]:
    """Coordinates for an address, park, or city via Nominatim (at most one request per second)."""
    global _last_nominatim
    time.sleep(max(0.0, 1.0 - (time.monotonic() - _last_nominatim)))
    _last_nominatim = time.monotonic()
    resp = requests.get(NOMINATIM_URL, params={"q": place, "format": "jsonv2", "limit": 1}, headers=OSM_HEADERS, timeout=10)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ToolError(
            f"No place called '{place}' was found.",
            "Use a park, landmark, or street address with the city, e.g. 'Prospect Park, Brooklyn'.",
        )
    return float(results[0]["lat"]), float(results[0]["lon"]), results[0]["display_name"]


def overpass(query: str) -> list[dict]:
    """Run an Overpass query, falling back to a mirror when the main server is busy."""
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(url, data={"data": f"[out:json][timeout:8];{query}"}, headers=OSM_HEADERS, timeout=9)
            resp.raise_for_status()  # busy servers answer 429 or 504
            return resp.json()["elements"]
        except (requests.RequestException, ValueError, KeyError):
            continue
    raise ToolError("The OpenStreetMap fountain service is busy.", "Try again in a few seconds.")


# --- Tool 1: readiness ---


def assess_readiness(
    resting_hr=None, usual_resting_hr=None, hrv=None, usual_hrv=None, sleep_hours=None, soreness=None
) -> str:
    """Compare today's body signals to the runner's own normal, and recommend an effort level."""
    flags, findings, skipped = 0, [], []

    # Each signal adds 0 (normal), 1 (caution), or 2 (warning) flags.
    if resting_hr is not None and usual_resting_hr is not None:
        diff = number("resting_hr", resting_hr, 25, 130) - number("usual_resting_hr", usual_resting_hr, 25, 130)
        level = 2 if diff >= 7 else 1 if diff >= 4 else 0
        flags += level
        findings += [f"Resting HR {diff:+.0f} bpm vs usual: " + ["normal.", "slightly elevated.", "well above normal, a sign of fatigue, stress, or illness."][level]]
    elif resting_hr is not None or usual_resting_hr is not None:
        skipped += ["resting HR (needs both today's and the usual value)"]

    if hrv is not None and usual_hrv is not None:
        change = (number("hrv", hrv, 5, 250) / number("usual_hrv", usual_hrv, 5, 250) - 1) * 100
        level = 2 if change <= -15 else 1 if change <= -8 else 0
        flags += level
        findings += [f"HRV {change:+.0f}% vs usual: " + ["normal.", "a bit suppressed.", "well below normal, the body is still recovering."][level]]
    elif hrv is not None or usual_hrv is not None:
        skipped += ["HRV (needs both today's and the usual value)"]

    if sleep_hours is not None:
        hours = number("sleep_hours", sleep_hours, 0, 16)
        level = 2 if hours < 5 else 1 if hours < 6.5 else 0
        flags += level
        findings += [f"Slept {hours:g} h: " + ["enough.", "a little short.", "very short."][level]]

    if soreness is not None:
        sore = number("soreness", soreness, 1, 10)
        level = 2 if sore >= 8 else 1 if sore >= 6 else 0
        flags += level
        findings += [f"Soreness {sore:g}/10: " + ["fine.", "noticeable.", "high."][level]]

    if not findings:
        raise ToolError(
            "No usable readiness metrics were given.",
            "Ask the user for sleep hours, soreness (1-10), or today's and usual resting heart rate. "
            "Or skip this tool and plan a normal run.",
        )

    effort, workout, slowdown = (
        ("push", "Hard workout is fine today.", 0) if flags == 0
        else ("as planned", "Run what was planned, but don't force extra intensity.", 0) if flags == 1
        else ("easy", "Swap any hard session for an easy run.", 2) if flags <= 3
        else ("rest", "Rest, or at most 20-30 minutes very easy.", 5)
    )
    result = {"effort": effort, "recommendation": workout, "pace_slowdown_pct": slowdown, "flags": flags, "findings": findings}
    if skipped:
        result["skipped"] = skipped
    return json.dumps(result)


# --- Tool 2: weather ---


def heat_slowdown_pct(temp_f: float, dew_point_f: float) -> float:
    """Runner's rule of thumb: temperature + dew point (F) above 100 starts to slow you down."""
    total = temp_f + dew_point_f
    for limit, pct in [(100, 0), (110, 0.5), (120, 1), (130, 2), (140, 3), (150, 4.5), (160, 6), (170, 8), (180, 10)]:
        if total <= limit:
            return pct
    return 12  # hard running not recommended


def get_running_conditions(location: str, hours_ahead: int = 12) -> str:
    """Hourly running weather and air quality for a place, and the best hour to go."""
    hours = int(number("hours_ahead", hours_ahead, 1, 48))
    lat, lon, label = locate(location)

    wx = requests.get(FORECAST_URL, params={
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_hours": hours,
        "hourly": "temperature_2m,dew_point_2m,apparent_temperature,precipitation_probability,wind_speed_10m,uv_index,is_day",
        "daily": "sunrise,sunset", "forecast_days": 3,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
    }, timeout=10).json()
    try:  # Air quality is a bonus; don't fail the whole forecast without it.
        aq = requests.get(AIR_QUALITY_URL, params={
            "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_hours": hours, "hourly": "us_aqi",
        }, timeout=10).json()["hourly"]
        aqi_at = dict(zip(aq["time"], aq["us_aqi"]))
    except (requests.RequestException, ValueError, KeyError):
        aqi_at = {}

    h = wx["hourly"]
    rows = []
    for i, t in enumerate(h["time"]):
        row = {
            "time": dt.datetime.fromisoformat(t).strftime("%a %H:%M"),
            "temp_f": round(h["temperature_2m"][i]),
            "feels_like_f": round(h["apparent_temperature"][i]),
            "dew_point_f": round(h["dew_point_2m"][i]),
            "rain_chance_pct": h["precipitation_probability"][i],
            "wind_mph": round(h["wind_speed_10m"][i]),
            "uv": h["uv_index"][i],
            "aqi": aqi_at.get(t),
            "daylight": bool(h["is_day"][i]),
            "heat_slowdown_pct": heat_slowdown_pct(h["temperature_2m"][i], h["dew_point_2m"][i]),
        }
        # Lower is better: heat costs pace; rain, smoke, dark, wind, and frost cost comfort.
        aqi = row["aqi"] or 0
        row["_score"] = (
            row["heat_slowdown_pct"]
            + (3 if (row["rain_chance_pct"] or 0) > 50 else 0)
            + (8 if aqi > 150 else 4 if aqi > 100 else 0)
            + (0 if row["daylight"] else 2)
            + (1 if row["wind_mph"] > 15 else 0)
            + (1 if (row["uv"] or 0) >= 8 else 0)
            + (2 if row["feels_like_f"] < 20 else 0)
        )
        rows += [row]

    best = min(rows, key=lambda r: r["_score"])
    for r in rows:
        r.pop("_score")

    notes = []
    if any((r["aqi"] or 0) > 100 for r in rows):
        notes += ["Air quality goes above 100 (unhealthy for sensitive groups) in this window; keep efforts easy or run indoors."]
    if any(r["heat_slowdown_pct"] >= 12 for r in rows):
        notes += ["Some hours are too hot and humid for hard running."]

    return json.dumps({
        "location": label,
        "sunrise": wx["daily"]["sunrise"][0][-5:],
        "sunset": wx["daily"]["sunset"][0][-5:],
        "now": rows[0],
        "best_hour": best,
        "hourly": rows[:24],
        "notes": notes,
    })


# --- Tool 3: paces ---

RACE_MILES = {"mile": 1.0, "5k": 3.10686, "10k": 6.21371, "half": 13.1094, "marathon": 26.2188}
WORLD_RECORDS = {"mile": 223, "5k": 755, "10k": 1571, "half": 3451, "marathon": 7235}  # seconds, men's
WORKOUTS = ["easy", "long", "marathon", "tempo", "interval"]


def parse_time(text: str) -> int:
    """'50:00' -> 3000 seconds; '1:45:30' -> 6330."""
    if not re.fullmatch(r"\d{1,2}(:\d{2}){1,2}", str(text).strip()):
        raise ToolError(f"Can't read race time '{text}'.", "Pass it as MM:SS or H:MM:SS, e.g. '50:00' or '1:52:30'.")
    seconds = 0
    for part in str(text).strip().split(":"):
        seconds = seconds * 60 + int(part)
    return seconds


def calculate_paces(
    race_distance: str, race_time: str, workout: str = "easy", run_miles=None,
    readiness_slowdown_pct=0, heat_slowdown_pct=0,
) -> str:
    """Training paces from a recent race, adjusted for today's readiness and weather."""
    key = re.sub(r"[^a-z0-9.]", "", race_distance.lower())  # 'Half Marathon' -> 'halfmarathon'
    key = {"halfmarathon": "half", "13.1": "half", "21k": "half", "26.2": "marathon", "fullmarathon": "marathon",
           "1mile": "mile", "5km": "5k", "10km": "10k"}.get(key, key)
    if key not in RACE_MILES:
        raise ToolError(f"Unknown race distance '{race_distance}'.", f"Use one of {list(RACE_MILES)}.")
    if workout not in WORKOUTS:
        raise ToolError(f"Unknown workout '{workout}'.", f"Use one of {WORKOUTS}.")

    seconds = parse_time(race_time)
    if seconds < WORLD_RECORDS[key]:
        raise ToolError(f"A {key} in {race_time} would beat the world record.", "Ask the user to double-check their race time.")
    if seconds / RACE_MILES[key] > 25 * 60:
        raise ToolError(f"A {key} in {race_time} is slower than 25 min/mile.", "Ask the user to double-check, or plan by effort instead.")

    # Riegel's formula predicts other distances: T2 = T1 * (D2 / D1) ** 1.06
    predicted = {d: seconds * (miles / RACE_MILES[key]) ** 1.06 for d, miles in RACE_MILES.items()}
    race_pace = {d: predicted[d] / RACE_MILES[d] for d in RACE_MILES}

    # Common coaching ranges, as multiples of race-equivalent paces (bigger = slower).
    ranges = {
        "easy": (race_pace["marathon"] * 1.12, race_pace["marathon"] * 1.25),
        "long": (race_pace["marathon"] * 1.06, race_pace["marathon"] * 1.15),
        "marathon": (race_pace["marathon"] * 0.99, race_pace["marathon"] * 1.01),
        "tempo": (race_pace["10k"] * 1.02, race_pace["half"] * 1.01),
        "interval": (race_pace["5k"] * 0.98, race_pace["5k"] * 1.01),
    }
    slowdown = number("readiness_slowdown_pct", readiness_slowdown_pct, 0, 15) + number("heat_slowdown_pct", heat_slowdown_pct, 0, 15)
    factor = 1 + slowdown / 100

    result = {
        "based_on": f"{key} in {race_time}",
        "adjusted_for_today_pct": slowdown,
        "predicted_race_times": {d: race_clock(t) for d, t in predicted.items()},
        "paces_today": {w: f"{pace(lo * factor)[:-3]}-{pace(hi * factor)}" for w, (lo, hi) in ranges.items()},
    }
    if run_miles is not None:
        miles = number("run_miles", run_miles, 0.5, 100)
        lo, hi = (p * factor for p in ranges[workout])
        result["today"] = {
            "workout": workout,
            "miles": miles,
            "pace": f"{pace(lo)[:-3]}-{pace(hi)}",
            "target_pace_min_per_mile": round((lo + hi) / 2 / 60, 2),
            "estimated_minutes": round(miles * (lo + hi) / 2 / 60),
            "estimated_time": f"{clock(miles * lo / 60)}-{clock(miles * hi / 60)}",
        }
    return json.dumps(result)


# --- Tool 4: route with water ---


def foot_route(points: list[tuple]) -> dict | None:
    """A walking/running route through points, or None if a point is far from any path."""
    coords = ";".join(f"{lon},{lat}" for lat, lon in points[:4])  # OSRM wants lon,lat
    data = requests.get(
        FOOT_ROUTE_URL.format(coords=coords),
        params={"overview": "simplified", "geometries": "geojson"}, headers=OSM_HEADERS, timeout=15,
    ).json()
    # OSRM snaps each point to the nearest path, even a far-off one. Reject big snaps.
    if data.get("code") != "Ok" or max(w["distance"] for w in data["waypoints"]) > 300:
        return None
    route = data["routes"][0]
    return {
        "meters": route["distance"],
        "legs": [leg["distance"] for leg in route["legs"]],
        "path": [[round(lat, 5), round(lon, 5)] for lon, lat in route["geometry"]["coordinates"]],
    }


def find_fountains(lat: float, lon: float, radius_m: float) -> list[dict]:
    """Public drinking fountains near a point: NYC Parks data inside NYC, OpenStreetMap elsewhere."""
    south, west, north, east = NYC_BOX
    if south < lat < north and west < lon < east:
        rows = requests.get(NYC_FOUNTAINS_URL, params={
            "$where": f"within_circle(the_geom, {lat}, {lon}, {round(radius_m)}) AND featuresta = 'Active'",
            "$limit": 2000,
        }, timeout=10).json()
        return [
            {
                "point": (r["the_geom"]["coordinates"][1], r["the_geom"]["coordinates"][0]),
                "water": True,
                "what": re.sub(r"^[A-Z]-", "", r.get("decription") or r.get("propertyna") or "drinking fountain"),
            }
            for r in rows if r.get("the_geom")
        ]

    elements = overpass(f'node["amenity"="drinking_water"](around:{round(radius_m)},{lat},{lon});out;')
    return [
        {
            "point": (e["lat"], e["lon"]),
            "water": True,
            "what": e["tags"].get("name") or ("bottle filler" if e["tags"].get("bottle") == "yes" else "drinking fountain"),
        }
        for e in elements if e.get("tags", {}).get("access") not in ("private", "no")
    ]


def plan_run_route(start: str, miles, water_stops: bool = True) -> str:
    """A loop of about `miles` from a start point, through public drinking fountains."""
    miles = number("miles", miles, 1, 20)
    lat, lon, label = locate(start)
    home = (lat, lon)
    target = miles * METERS_PER_MILE
    radius = target / (2 * math.pi)  # a circle with this radius has the target length

    notes = []
    fountains = []
    if water_stops:
        try:
            fountains = find_fountains(lat, lon, radius * 2.2)
        except (ToolError, requests.RequestException, ValueError, KeyError):
            notes += ["Fountain data was unavailable, so this loop has no planned water stops. Carry water."]
        if not fountains and not notes:
            notes += ["No public fountains are mapped near here. Carry water."]

    # Plain turn-around points on rings, used when there aren't enough fountains.
    ring = []
    for scale in (0.9, 1.3, 1.7):
        for deg in range(0, 360, 30):
            b = math.radians(deg)
            r = radius * scale
            ring += [{
                "point": (lat + r * math.cos(b) / 111_320, lon + r * math.sin(b) / (111_320 * math.cos(math.radians(lat)))),
                "water": False,
            }]
    near = sorted(fountains, key=lambda f: abs(distance_m(home, f["point"]) - radius))[:30]
    candidates = [c for c in near + ring if 0.3 * radius < distance_m(home, c["point"]) < 2.2 * radius]

    def straight(a, b):  # length of the triangle home -> a -> b -> home
        return distance_m(home, a["point"]) + distance_m(a["point"], b["point"]) + distance_m(b["point"], home)

    pairs = [
        (a, b) for a, b in itertools.combinations(candidates, 2)
        if 60 < (bearing(home, a["point"]) - bearing(home, b["point"])) % 360 < 300  # not an out-and-back
    ]

    # Paths wind, so a route runs longer than the straight lines. Start at 1.2x, then
    # learn the real ratio from each route and retry, up to 6 routes.
    stretch, best, tried = 1.2, None, set()
    for _ in range(6):
        options = [
            (abs(stretch * straight(a, b) - target) + 0.15 * target * ((not a["water"]) + (not b["water"])), i)
            for i, (a, b) in enumerate(pairs) if i not in tried
        ]
        if not options:
            break
        i = min(options)[1]
        tried.add(i)
        a, b = pairs[i]
        route = foot_route([home, a["point"], b["point"], home])
        if route is None:
            continue
        ratio = route["meters"] / straight(a, b)
        if ratio > 2:  # a detour around water (e.g. over a bridge): skip it, and don't learn from it
            continue
        stretch = min(max(ratio, 1.05), 1.8)
        if best is None or abs(route["meters"] - target) < abs(best[0]["meters"] - target):
            best = (route, a, b)
        if abs(route["meters"] - target) < 0.05 * target:
            break

    if best is None:
        raise ToolError(
            f"Couldn't build a {miles:g} mile loop from '{label}'.",
            "Try a start point in or next to a park, or a different distance.",
        )

    route, a, b = best
    stops, at = [], 0.0
    for leg, wp in zip(route["legs"], (a, b)):
        at += leg
        if wp["water"]:
            stops += [{"mile": round(at / METERS_PER_MILE, 1), "what": wp["what"]}]

    actual = route["meters"] / METERS_PER_MILE
    if miles - actual > 0.15:
        extra = (miles - actual) / 2
        notes += [f"The loop is {actual:.1f} mi, short of {miles:g}. To make it up, run {extra:.2f} mi out from the start and back ({2 * extra:.1f} mi extra)."]
    elif actual - miles > 0.15:
        notes += [f"The loop is {actual:.1f} mi, a little over {miles:g}."]

    month = dt.date.today().month
    if stops and lat > 30 and month in (11, 12, 1, 2, 3, 4):
        notes += ["Winter season: many park fountains are shut off (NYC Parks: roughly November to April). Carry water."]
    elif stops:
        notes += ["Fountains can be broken or off without notice. Carry a little water in case."]

    waypoints = [f"{p['point'][0]:.5f},{p['point'][1]:.5f}" for p in (a, b)]
    maps_link = "https://www.google.com/maps/dir/?" + urlencode({
        "api": 1, "origin": f"{lat:.5f},{lon:.5f}", "destination": f"{lat:.5f},{lon:.5f}",
        "waypoints": "|".join(waypoints), "travelmode": "walking",
    })
    gaps = [g / METERS_PER_MILE for g in route["legs"]]

    return json.dumps({
        "start": label,
        "loop_miles": round(actual, 2),
        "water_stops": stops,
        "longest_stretch_without_water_mi": round(max(gaps), 1) if stops else round(actual, 1),
        "google_maps_link": maps_link,
        "notes": notes,
        # For the page's map. The model can ignore it.
        "map": {"start": [round(lat, 5), round(lon, 5)], "path": route["path"], "water": [list(p["point"]) for p in (a, b) if p["water"]]},
    })


# --- Tool 5: fueling ---


def lookup_gel(name: str) -> dict:
    """Carbs, sodium, and caffeine per serving from Open Food Facts; falls back to a typical gel."""
    typical = {"name": name, "carbs_g": 25, "sodium_mg": 50, "caffeine_mg": 0, "source": "assumed typical gel"}
    try:
        products = requests.get(FOOD_SEARCH_URL, params={
            "search_terms": name, "search_simple": 1, "json": 1, "page_size": 8,
            "fields": "product_name,brands,serving_size,nutriments",
        }, headers=OSM_HEADERS, timeout=15).json().get("products", [])
    except (requests.RequestException, ValueError):
        return {**typical, "note": "Open Food Facts didn't respond, so a typical 25 g gel was assumed. Ask the user for the carbs per gel."}

    for p in products:
        n = p.get("nutriments", {})
        carbs = n.get("carbohydrates_serving")
        if carbs is None:  # fall back to per-100g times the serving size, if both are known
            grams = re.search(r"(\d+(?:\.\d+)?)\s*g", p.get("serving_size") or "")
            if grams and n.get("carbohydrates_100g") is not None:
                carbs = n["carbohydrates_100g"] * float(grams.group(1)) / 100
        if not carbs:
            continue
        caffeine = n.get("caffeine_serving") or 0
        brand, product = (p.get("brands") or "").split(",")[0].strip(), p.get("product_name") or name
        return {
            "name": product if product.lower().startswith(brand.lower()) else f"{brand} {product}".strip(),
            "carbs_g": round(float(carbs)),
            "sodium_mg": round(float(n.get("sodium_serving") or 0) * 1000),
            "caffeine_mg": round(float(caffeine) * (1000 if float(caffeine) < 1 else 1)),  # stored in g or mg
            "source": "Open Food Facts",
        }
    return {**typical, "note": f"Couldn't find carbs per serving for '{name}' in Open Food Facts, so a typical 25 g gel was assumed. Ask the user what the packet says."}


def plan_fueling(duration_min, temp_f=60, gel: str | None = None, pace_min_per_mile=None, water_stop_miles=None) -> str:
    """Carbs, fluids, and sodium for a run, and when to take each gel."""
    minutes = number("duration_min", duration_min, 10, 600)
    temp = number("temp_f", temp_f, -20, 120)
    per_mile = number("pace_min_per_mile", pace_min_per_mile, 3.5, 25) if pace_min_per_mile is not None else None
    hours = minutes / 60

    # General sports-nutrition guidance: more carbs per hour the longer you go.
    carbs_per_h = (0, 0) if minutes < 60 else (30, 45) if minutes < 90 else (45, 60) if minutes <= 150 else (60, 90)
    fluid_per_h = (300, 500) if temp < 50 else (400, 600) if temp <= 75 else (600, 800)
    sodium_per_h = (300, 500) if temp < 50 else (300, 600) if temp <= 75 else (500, 800)

    result = {
        "duration": clock(minutes),
        "carbs_g_per_hour": f"{carbs_per_h[0]}-{carbs_per_h[1]}" if carbs_per_h[1] else "none needed",
        "fluid_ml_per_hour": f"{fluid_per_h[0]}-{fluid_per_h[1]}",
        "sodium_mg_per_hour": f"{sodium_per_h[0]}-{sodium_per_h[1]}",
        "notes": [],
    }

    if minutes < 60:
        result["gels"] = []
        result["notes"] += ["Under an hour, no fuel is needed. Water if it's hot."]
    else:
        info = lookup_gel(gel) if gel else {"name": "gel", "carbs_g": 25, "sodium_mg": 50, "caffeine_mg": 0, "source": "assumed typical gel"}
        if "note" in info:
            result["notes"] += [info.pop("note")]
        target = sum(carbs_per_h) / 2
        every = info["carbs_g"] / target * 60  # minutes between gels
        schedule, t = [], min(45, max(30, every))  # first gel around 30-45 minutes in
        while t <= minutes - 10:
            schedule += [{"at": clock(t), **({"mile": round(t / per_mile, 1)} if per_mile else {})}]
            t += every
        result["gel"] = info
        result["gels"] = schedule
        result["gel_count"] = len(schedule)

        got_sodium = len(schedule) * info["sodium_mg"] / hours
        if got_sodium < sodium_per_h[0]:
            result["notes"] += [f"Gels give only ~{round(got_sodium)} mg sodium/hour. Add an electrolyte drink or salt tab."]
        if info["caffeine_mg"]:
            result["notes"] += [f"Each gel has ~{info['caffeine_mg']} mg caffeine ({info['caffeine_mg'] * len(schedule)} mg total). Most runners stay under ~200-300 mg."]

    if water_stop_miles and per_mile:
        result["water_stops"] = [{"mile": m, "at": clock(float(m) * per_mile)} for m in water_stop_miles]
        result["notes"] += [f"Drink about {round(sum(fluid_per_h) / 2 * hours / (len(water_stop_miles) + 1))} ml at each stop, or to thirst."]
    elif minutes >= 60:
        result["notes"] += [f"Carry about {round(fluid_per_h[0] * hours)} ml of fluid, or plan water stops."]

    result["notes"] += ["General guidance, not medical advice. Practice fueling in training before race day."]
    return json.dumps(result)


# What the model sees: the "set notes" in the screenplay.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "assess_readiness",
            "description": (
                "Compare the runner's body signals today to their own normal and recommend an effort: "
                "push, as planned, easy, or rest. Pass whatever metrics the user gave; each comparison "
                "needs both today's value and the usual value. Returns a pace slowdown to pass to calculate_paces."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "resting_hr": {"type": "number", "description": "Resting heart rate this morning, bpm"},
                    "usual_resting_hr": {"type": "number", "description": "Their normal resting heart rate, bpm"},
                    "hrv": {"type": "number", "description": "Heart rate variability this morning, ms"},
                    "usual_hrv": {"type": "number", "description": "Their normal HRV, ms"},
                    "sleep_hours": {"type": "number", "description": "Hours slept last night"},
                    "soreness": {"type": "number", "description": "Muscle soreness from 1 (none) to 10 (very sore)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_running_conditions",
            "description": (
                "Hour-by-hour running weather for a place: temperature, feels-like, dew point, rain chance, "
                "wind, UV, air quality, and daylight. Picks the best hour to run and estimates how much "
                "heat and humidity will slow the runner down (heat_slowdown_pct, for calculate_paces)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "Park, neighborhood, or city, e.g. 'Prospect Park, Brooklyn'"},
                    "hours_ahead": {"type": "integer", "description": "How many hours to look ahead, 1 to 48. Default 12."},
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_paces",
            "description": (
                "Training paces (easy, long, marathon, tempo, interval) from a recent race result, slowed "
                "down for today's readiness and heat. With run_miles, also estimates how long today's run "
                "takes; pass that duration and target pace to plan_fueling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "race_distance": {"type": "string", "enum": list(RACE_MILES), "description": "Distance of a recent race or time trial"},
                    "race_time": {"type": "string", "description": "Finish time, MM:SS or H:MM:SS, e.g. '50:00'"},
                    "workout": {"type": "string", "enum": WORKOUTS, "description": "Today's workout type. Default 'easy'."},
                    "run_miles": {"type": "number", "description": "Today's planned distance in miles"},
                    "readiness_slowdown_pct": {"type": "number", "description": "pace_slowdown_pct from assess_readiness, default 0"},
                    "heat_slowdown_pct": {"type": "number", "description": "heat_slowdown_pct for the run hour from get_running_conditions, default 0"},
                },
                "required": ["race_distance", "race_time"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_run_route",
            "description": (
                "Plan a running loop of about the given miles that starts and ends at one place and passes "
                "public drinking fountains. Returns the actual distance, the mile of each water stop, the "
                "longest stretch without water, and a Google Maps link. Always share the link."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start": {"type": "string", "description": "Where the run starts and ends, e.g. 'Grand Army Plaza, Brooklyn'"},
                    "miles": {"type": "number", "description": "Target distance, 1 to 20 miles"},
                    "water_stops": {"type": "boolean", "description": "Route past drinking fountains. Default true."},
                },
                "required": ["start", "miles"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_fueling",
            "description": (
                "Fueling plan for a run: carbs, fluid, and sodium per hour, and the time (and mile, if pace "
                "is given) to take each gel. Looks up the runner's gel in Open Food Facts for its real carbs, "
                "sodium, and caffeine. Pass water_stop_miles from plan_run_route to time drinks to fountains."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_min": {"type": "number", "description": "Expected run time in minutes"},
                    "temp_f": {"type": "number", "description": "Temperature during the run, F. Default 60."},
                    "gel": {"type": "string", "description": "The runner's gel or chew, e.g. 'GU Energy Gel', 'Maurten Gel 100'"},
                    "pace_min_per_mile": {"type": "number", "description": "Target pace in minutes per mile, e.g. 9.5"},
                    "water_stop_miles": {"type": "array", "items": {"type": "number"}, "description": "Miles where the route passes water"},
                },
                "required": ["duration_min"],
            },
        },
    },
]

# What the harness runs: tool name -> Python function.
TOOL_MAP = {
    "assess_readiness": assess_readiness,
    "get_running_conditions": get_running_conditions,
    "calculate_paces": calculate_paces,
    "plan_run_route": plan_run_route,
    "plan_fueling": plan_fueling,
}


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
