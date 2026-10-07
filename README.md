# Pace Coach

A running coach that plans **today's** run from your own numbers. Tell it how your
body feels and a recent race; it checks the weather, sets your paces, maps a loop
past public drinking fountains, and tells you when to take each gel.

**For:** recreational runners who track things like resting heart rate and sleep,
but still guess at pace on hot days and at fueling on long runs.

Built on the `gemini-web-tool-calling` starter (FastAPI + LiteLLM + Gemini,
`vertex_ai/gemini-3.5-flash-lite`).

## Sample queries

1. I ran a 10K in 50:00. Slept 5.5 hours, resting HR 58 (usually 52). Plan an easy
   6 miles from Grand Army Plaza with water stops. I use GU gels.
2. What's the best time to run in Central Park today, and how much will the weather
   slow me down?
3. Half marathon PR is 1:52:00. Plan a 10 mile long run from Central Park with
   Maurten Gel 100.

Follow-ups use what you said earlier, e.g. after (1): "What would my tempo pace be
if I felt great instead?"

## Tools

| Tool | What it does | Data |
|---|---|---|
| `assess_readiness` | Compares today's resting HR, HRV, sleep, and soreness to your normal; recommends push / as planned / easy / rest | Calculation |
| `get_running_conditions` | Hourly temperature, dew point, wind, rain, UV, air quality, daylight; the best hour; heat slowdown % | Open-Meteo forecast and air quality |
| `calculate_paces` | Easy, long, marathon, tempo, and interval paces from a recent race (Riegel's formula), slowed for readiness and heat; estimated run time | Calculation |
| `plan_run_route` | A loop of your distance through public drinking fountains, with the mile of each stop and a Google Maps link | NYC Parks fountains (NYC Open Data) or OpenStreetMap, OSRM foot routing, Nominatim |
| `plan_fueling` | Carbs, fluid, and sodium per hour; when (and at which mile) to take each gel, using the gel's real label | Open Food Facts |

The model chains them: readiness and weather set the slowdown, paces set the run
time, the route sets the water stops, and all of it feeds the fueling plan. The
calculators exist because models are unreliable at multi-step arithmetic.

Every tool returns JSON. Failures return `{"error", "how_to_fix"}` so the model can
retry with better arguments or explain the problem, and `run_tool` catches every
exception so a bad call never crashes the loop.

`/chat` returns `response`, `session_id`, and `tool_calls` (name, args, and result of
every call). The page shows each call as an expandable card and draws planned
routes on a map.

## Setup

1. A GCP project with billing and the Agent Platform API enabled
   (older docs and the endpoint itself still call it Vertex AI)
2. `gcloud auth application-default login`. The app uses your gcloud default
   project, so run `gemini-hello-world` first to check it.
3. `uv run app.py`, then open http://localhost:8000

On Cloud Run the app listens on `0.0.0.0:$PORT`. Sessions live in memory, so run
a single instance to keep each conversation together.

## Limits

- Paces and readiness use common coaching rules of thumb, and fueling follows
  general sports-nutrition ranges. Not medical advice.
- Fountain data can be out of date, and many park fountains are shut off in winter.
- Loops are built from fountains and turn-around points, so they sometimes use
  streets rather than staying inside a park.
- The OpenStreetMap services are shared and rate limited, so this is for demos,
  not heavy use.
