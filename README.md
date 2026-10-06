# Set Scout

An agent for NYC film and TV shoots: find where productions have been filming,
grab coffee nearby, and get there. Built on the `gemini-web-tool-calling` starter
(FastAPI + LiteLLM + Gemini, `vertex_ai/gemini-3.5-flash-lite`).

## Tools

| Tool | What it does | Data source (free, no API key) |
|---|---|---|
| `find_film_shoots` | Recent permitted shoots by borough, street, or ZIP code | NYC Open Data film permits |
| `find_coffee_near` | Cafes near an address, landmark, or NYC intersection, closest first | OpenStreetMap: Nominatim, Overpass |
| `get_route` | Walk, bike, and drive routes with directions, and which is fastest | OSRM at routing.openstreetmap.de |

NYC intersections are written `Street & Cross Street, Borough`, so the agent can
chain tools: a shoot's blocks become the location for coffee or a route.

Every tool returns JSON. Failures return `{"error", "how_to_fix"}` so the model
can retry with better arguments or explain the problem to the user.

`/chat` returns `response`, `session_id`, and `tool_calls` (name, args, and
result of every call), and the page shows each call as an expandable card.

## Setup

1. A GCP project with billing and the Agent Platform API enabled
   (older docs and the endpoint itself still call it Vertex AI)
2. `gcloud auth application-default login`. The app uses your gcloud default
   project, so run `gemini-hello-world` first to check it.
3. `uv run app.py`, then open http://localhost:8000

Try: "Find a recent shoot in Greenpoint (11222) and a coffee shop next to it."

## Limits

- The city's permit feed can lag by months; the agent says what date it runs through.
- No subway or bus routing, and drive times ignore traffic.
- The OpenStreetMap services are shared and rate limited (Nominatim: one request
  per second), so this is for demos, not heavy use.
