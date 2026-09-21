# terminal_L — offline in-flight dashboard

A terminal flight tracker for the flight you're **on**. Prepare it on the ground
while you still have internet, then run it offline in the air. It works out where
you are from the laptop clock and your takeoff time.

```
┌ 🗺 MapSCII · follow · zoom 4.9 ──────────────┐┌ ✈ Flight data ─────────────┐
│   (vector map, route: flown ━ / remaining ┄)  ││ altitude, V/S, GS, TAS,     │
│              ✈↙                              ││ Mach, wind, OAT, heading…   │
│                                              │├ ⏱ Progress ────────────────┤
│                                              ││ ━━━━━━━━✈──────  ETA, TZs   │
│                                              │├ ⌖ Below you ───────────────┤
│                                              ││ city + fun fact (rotates)   │
│                                              │├ ➜ Coming up · ☀ Sky ───────┤
└──────────────────────────────────────────────┘└ next cities, sun side ──────┘
```

## Setup (once)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # then add your OpenAI key (optional)
npm install            # installs mapscii (used as the map renderer)
```

## Before the flight (online)

```bash
./fly prep SK1415 --dep 10:05 --arr 11:15    # scheduled local times
./fly prep UA837 --duration 10h50m           # or airborne time directly
./fly prep SK1415                            # asks for times; Enter = estimate
```

This downloads:

- **Route**: origin/destination airports from [adsbdb](https://www.adsbdb.com/)
  (no API key needed). IATA (`SK1415`) or ICAO (`SAS1415`) callsigns both work.
- **Cities** along a 150 km corridor of the great-circle path (GeoNames `cities15000`).
- **Fun facts**: the Wikipedia article for each featured city. With
  `OPENAI_API_KEY` set in `.env`, OpenAI picks 3 surprising facts per city
  (model from `OPENAI_MODEL` in `.env`, currently `gpt-5.6-terra`). Without a
  key, a heuristic picks interesting-looking sentences from the article.
  `--no-llm` skips OpenAI. Variables already exported in your shell override `.env`.
- **Map tiles**: MapSCII vector tiles covering the route at zoom 0–7
  (`--max-zoom` to change; long-haul flights at 7 take roughly 20–60 MB).

- **Seas/oceans** named along the route (from the tiles), with their own Wikipedia facts for long stretches over water.

Everything goes into `flights/<FLIGHT>.json` and `data/`.

## On board (offline)

```bash
./fly board SK1415                 # asks for the wheels-up time
./fly board SK1415 --takeoff 10:21 # HH:MM on the laptop clock
./fly board --takeoff -35m         # took off 35 min ago (last prepared flight)
./fly board SK1415 --takeoff now --speed 60   # demo: 60× speed
./fly board AY810 --duration 48m   # use the flight time the pilot announces
```

| Key | Action |
| --- | --- |
| `m` | toggle map follow / whole-route overview |
| `+` / `-` | zoom |
| `←` / `→` | shift time ±5 min (correct a wrong takeoff time) |
| `0` | back to live time |
| `f` | next fun fact |
| `q` | quit |

## How the numbers are made

Nothing is live in the air, so the flight is **modelled**:

- Path: great circle between the airports (real routes curve around airways and weather).
- Profile: climb to a distance-based cruise level (~FL310–370, step climbs on
  long-haul), cruise at Mach 0.78–0.83, descent. Ground speed is scaled so you
  arrive exactly at the scheduled time; the difference from airspeed is shown as
  estimated wind.
- Outside temperature/pressure: ISA standard atmosphere at the modelled altitude.
- Sun: solar elevation/azimuth at your position, which side of the plane it's on,
  and the next sunrise/sunset along the route.
- Airborne time from `--dep/--arr` = scheduled gate-to-gate time minus taxiing (~15 min on short hops, up to 25 min on long-haul). If the pilot announces the flight time, pass it: `./fly board AY810 --duration 48m`.

If the plane is ahead or behind the model, nudge it with `←`/`→`.

## Layout

- `inflight/prep.py` — online data collection
- `inflight/profile.py` — flight model (altitude, speeds, position vs time)
- `inflight/geo.py` — great-circle math, cross-track distance, sun position
- `inflight/app.py` — Textual dashboard
- `inflight/map_bridge.js` — renders MapSCII frames from local tiles with the route overlaid
- `inflight/marine_labels.js` — pulls sea/ocean names out of the tiles (shown when over water)
