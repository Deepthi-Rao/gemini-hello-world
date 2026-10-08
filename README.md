# Pace Coach

**For:** recreational runners who track things like resting heart rate, HRV, and
sleep, but still guess at pace on hot days and at fueling on long runs.

**What it does:** Pace Coach plans **today's** run from your own numbers. You tell
it how your body feels and give it a recent race time. It checks the weather, sets
your paces, maps a loop past public drinking fountains, and tells you when to take
each gel.

Built on the `gemini-web-tool-calling` starter (FastAPI + LiteLLM + Gemini,
`vertex_ai/gemini-3.5-flash-lite`).

## Tools

- `assess_readiness`: compares today's resting HR, HRV, sleep, and soreness to your normal, then recommends push, as planned, easy, or rest.
- `get_running_conditions`: gives the hourly forecast and air quality (Open-Meteo), the best hour to run, and how much the heat will slow you.
- `calculate_paces`: works out easy, long, marathon, tempo, and interval paces from a recent race, slowed for readiness and heat.
- `plan_run_route`: builds a loop of your distance past public drinking fountains (NYC Parks data or OpenStreetMap), with a map and a Google Maps link.
- `plan_fueling`: says how much carbs, fluid, and sodium to take per hour, and at which minute and mile to take each gel, using the gel's real label (Open Food Facts).

## How to use

Open the app and type into the chat box. It's a chatbot, so plain sentences work.
For a full plan, include:

- where you're starting
- how far you want to run
- a recent race time
- how you feel today: resting HR or HRV against your usual, sleep, and soreness
- your gel, if you use one

Or fill in the **Your numbers** panel on the left and press **Plan my run**, which
writes that message for you. The three example buttons run the queries below. Each tool the agent calls appears as
a card you can expand to see its arguments and result. Planned routes are drawn
on a map. The agent remembers the conversation, so you can ask follow-ups.
**New chat** starts over.

### Example queries

1. I'm starting from 515 W 110th St in Manhattan and want to run 7 miles past water
   fountains. My HRV last night was 56 (usually around 62) and I slept 7 hours. My
   last 10K was 52:30. Where should I run, where are the water fountains, and what
   pace should I run to build fitness? I use GU gels.
2. What's the best time to run in Central Park today, and how much will the weather
   slow me down?
3. I ran a 10K in 50:00. Slept 5.5 hours, resting HR 58 (usually 52). Plan an easy
   6 miles from Grand Army Plaza with water stops. I use GU gels.

Follow-up after (1): "What would my tempo pace be if I felt great instead?"

## How it works

The model chains the tools. Readiness and weather set the slowdown. The paces set
the run time. The route sets the water stops. All of it feeds the fueling plan.
The calculator tools exist because models are unreliable at multi-step arithmetic.

Every tool returns JSON. A failed call returns `{"error", "how_to_fix"}`, so the
model can retry with better arguments or explain the problem. `run_tool` catches
every exception, so a bad call never crashes the loop.

`/chat` returns `response`, `session_id`, and `tool_calls` (the name, args, and
result of every call).

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
