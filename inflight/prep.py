"""Online preparation: download everything a flight needs so `fly` can run offline."""
import io
import json
import math
import os
import random
import re
import sys
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import geo
from .profile import FlightModel

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TILES = DATA / "tiles"
FLIGHTS = ROOT / "flights"

UA = {"User-Agent": "inflight-terminal-tracker/0.1 (personal hobby project)"}
GEONAMES = "https://download.geonames.org/export/dump/"
TILE_SERVER = "https://mapscii.me/"
WIKI_API = "https://en.wikipedia.org/w/api.php"

session = requests.Session()
session.headers.update(UA)


def log(msg):
    print(f"  {msg}", flush=True)


# --- flight lookup ----------------------------------------------------------

def lookup_route(flight):
    r = session.get(f"https://api.adsbdb.com/v0/callsign/{flight}", timeout=20)
    if r.status_code == 404:
        raise SystemExit(f"adsbdb has no route for {flight}. Try the ICAO callsign (e.g. SAS1415).")
    r.raise_for_status()
    resp = r.json().get("response")
    if not isinstance(resp, dict) or "flightroute" not in resp:
        raise SystemExit(f"No route found for {flight}: {resp}")
    return resp["flightroute"]


# --- geonames ---------------------------------------------------------------

def _download(name):
    path = DATA / name
    if not path.exists():
        log(f"downloading {name} …")
        r = session.get(GEONAMES + name, timeout=120)
        r.raise_for_status()
        path.write_bytes(r.content)
    return path


def load_geonames():
    DATA.mkdir(exist_ok=True)
    countries = {}
    for line in _download("countryInfo.txt").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            f = line.split("\t")
            countries[f[0]] = f[4]
    admin1 = {}
    for line in _download("admin1CodesASCII.txt").read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) > 1:
            admin1[f[0]] = f[1]
    cities = []
    with zipfile.ZipFile(_download("cities15000.zip")) as z:
        with z.open("cities15000.txt") as fh:
            for line in io.TextIOWrapper(fh, encoding="utf-8"):
                f = line.rstrip("\n").split("\t")
                cities.append({
                    "name": f[1], "lat": float(f[4]), "lon": float(f[5]),
                    "country": countries.get(f[8], f[8]), "cc": f[8],
                    "region": admin1.get(f"{f[8]}.{f[10]}", ""),
                    "pop": int(f[14] or 0), "tz": f[17],
                })
    return cities


def nearest_city(cities, lat, lon):
    return min(cities, key=lambda c: geo.haversine(lat, lon, c["lat"], c["lon"]))


def airport_city(cities, airport):
    """The city an airport serves: its municipality name if nearby, else the nearest city."""
    lat, lon = airport["latitude"], airport["longitude"]
    name = (airport.get("municipality") or "").lower()
    named = [c for c in cities if c["name"].lower() == name
             and geo.haversine(lat, lon, c["lat"], c["lon"]) < 80]
    return max(named, key=lambda c: c["pop"]) if named else nearest_city(cities, lat, lon)


def corridor_cities(cities, o, d, total_km, width_km):
    """All cities within width_km of the great circle, annotated with along/cross-track km."""
    route = geo.great_circle(o[0], o[1], d[0], d[1], 60)
    lats = [p[0] for p in route]
    pad = width_km / 111 + 1
    lat_lo, lat_hi = min(lats) - pad, max(lats) + pad
    out = []
    for c in cities:
        if not (lat_lo <= c["lat"] <= lat_hi):
            continue
        xt, at = geo.cross_along_track(o[0], o[1], d[0], d[1], c["lat"], c["lon"])
        if abs(xt) <= width_km and -width_km <= at <= total_km + width_km:
            out.append({**c, "xt": round(xt, 1), "at": round(at, 1)})
    out.sort(key=lambda c: c["at"])
    return out


def pick_featured(corridor, total_km, forced):
    """Most populous cities near the track, spaced out; then fill big gaps with towns further off it."""
    spacing = max(25.0, total_km / 70)
    picked = list(forced)
    for max_xt, gap in ((45, spacing), (150, spacing * 3)):
        for c in sorted(corridor, key=lambda c: -c["pop"]):
            if (abs(c["xt"]) <= max_xt and -15 <= c["at"] <= total_km + 15
                    and all(abs(c["at"] - p["at"]) >= gap for p in picked)):
                picked.append(c)
    picked.sort(key=lambda c: c["at"])
    return picked


# --- wikipedia + facts --------------------------------------------------------

def wiki_get(params, timeout=30):
    """Wikipedia API call with backoff; Wikimedia rate-limits bursts with non-JSON 429s."""
    for attempt in range(6):
        r = session.get(WIKI_API, params=params, timeout=timeout)
        if r.status_code == 200:
            try:
                data = r.json()
                if "error" not in data:
                    return data
            except ValueError:
                pass
        time.sleep(float(r.headers.get("Retry-After", 0) or 0) or min(10.0, 1.5 * 2 ** attempt))
    r.raise_for_status()
    return r.json()


def wiki_find_title(city):
    if city.get("sea"):
        return city["name"]
    params = {"action": "query", "list": "geosearch", "gscoord": f"{city['lat']}|{city['lon']}",
              "gsradius": 10000, "gslimit": 30, "format": "json"}
    hits = wiki_get(params).get("query", {}).get("geosearch", [])
    name = city["name"].lower()
    for h in hits:
        t = h["title"].lower()
        if t == name or t.startswith(name + ","):
            return h["title"]
    params = {"action": "query", "list": "search", "srlimit": 5, "format": "json",
              "srsearch": f"{city['name']} {city['region']} {city['country']}"}
    for h in wiki_get(params).get("query", {}).get("search", []):
        if h["title"].lower().startswith(name):
            return h["title"]
    return None


def wiki_article(title):
    params = {"action": "query", "prop": "extracts|description", "explaintext": 1,
              "titles": title, "redirects": 1, "format": "json"}
    pages = wiki_get(params)["query"]["pages"]
    page = next(iter(pages.values()))
    return page.get("extract", ""), page.get("description", "")


HOOKS = re.compile(r"\b(first|oldest|only|largest|smallest|longest|tallest|highest|world|famous|"
                   r"known as|nicknamed|record|legend|named after|invented|unusual|unique|"
                   r"birthplace|festival|museum|castle|shipwreck|meteor|ghost|curious|remarkable|"
                   r"inspired|mystery|annual|dinosaur|fossil|twinned|statue)\b", re.I)
BORING = re.compile(r"\b(population|census|municipality|inhabitants|km2|km²|square kilometres|"
                    r"administrative|seat of|coordinates|elevation|located)\b", re.I)


def heuristic_facts(text, name, n=5):
    """Pick 'interesting-looking' sentences from outside the lead section."""
    body = text.split("\n==", 1)[1] if "\n==" in text else text
    body = re.sub(r"=+[^=\n]+=+", "\n", body)
    stop = re.search(r"\n\s*(See also|References|External links|Notes|Further reading)\s*\n", body)
    if stop:
        body = body[:stop.start()]
    sents = re.split(r"(?<=[.!?])\s+(?=[A-Z])", body.replace("\n", " "))
    scored = []
    for s in sents:
        s = s.strip()
        if not 70 <= len(s) <= 280 or s.count(",") > 6 or ":" in s:
            continue
        score = 2 * len(HOOKS.findall(s)) - 2 * len(BORING.findall(s))
        score += 0.5 * bool(re.search(r"\b1[0-9]{3}\b", s))
        if re.match(r"(It|This|These|He|She|They|His|Her|Its|The \w+ (has|was|is|also))\b", s):
            score -= 2
        if name.split(",")[0].lower() in s.lower():
            score += 1.5
        scored.append((score + random.random() * 0.5, s))
    scored.sort(reverse=True)
    return [s for _, s in scored[:n] if _ > 0.5] or [s for _, s in scored[:2]]


def openai_facts(city, article):
    from openai import OpenAI
    client = OpenAI(timeout=90, max_retries=1)
    model = os.environ.get("OPENAI_MODEL", "gpt-5.4-mini")
    prompt = (
        f"Here is the Wikipedia article for {city['name']}{', ' + city['country'] if city.get('country') else ''}.\n\n{article[:20000]}\n\n"
        "Give 3 genuinely surprising, little-known fun facts about this place that a curious airline "
        "passenger flying overhead would enjoy. Avoid population, location and generic 'is a city' facts. "
        "Each fact must be one or two sentences, self-contained (name the place), and grounded in the "
        "article. Output one fact per line, no numbering or bullets."
    )
    resp = client.responses.create(model=model, input=prompt)
    lines = [re.sub(r"^[\s\-*•\d.)]+", "", l).strip() for l in resp.output_text.splitlines()]
    return [l for l in lines if len(l) > 20][:3]


_llm_disabled = False


def enrich(city, use_llm):
    global _llm_disabled
    city = dict(city)
    try:
        title = wiki_find_title(city)
        if title:
            text, desc = wiki_article(title)
            lead = text.split("\n", 1)[0]
            # drop pronunciation/transliteration asides: "Bering Sea ( BAIR-ing; Russian: ...)"
            lead = re.sub(r"\s*\((?:[^()]|\([^()]*\))*(?:pronunciation|IPA|listen|romanized|Russian|;)(?:[^()]|\([^()]*\))*\)", "", lead)
            city["wiki"] = title
            city["description"] = desc
            city["summary"] = " ".join(re.split(r"(?<=[.!?])\s+", lead)[:2])[:400]
            city["facts"] = heuristic_facts(text, city["name"])
            if use_llm and not _llm_disabled and len(text) > 500:
                try:
                    llm = openai_facts(city, text)
                    if llm:
                        city["facts"] = llm + city["facts"][:2]
                        city["facts_source"] = "openai"
                except Exception as e:  # keep the heuristic facts
                    city["llm_error"] = str(e)[:200]
                    if getattr(e, "status_code", None) in (401, 403, 404):
                        _llm_disabled = True  # bad key/model: don't retry for every city
    except Exception as e:
        city["error"] = str(e)[:200]
    return city


# --- map tiles ------------------------------------------------------------------

def tile_xy(lat, lon, z):
    lat = max(-85.05, min(85.05, lat))
    n = 2 ** z
    x = (lon + 180) / 360 * n
    y = (1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat))) / math.pi) / 2 * n
    return x, y


def needed_tiles(route, max_zoom):
    tiles = set()
    lats = [p[0] for p in route]
    lons = [p[1] for p in route]
    center = ((min(lats) + max(lats)) / 2, (min(lons) + max(lons)) / 2)
    for z in range(0, max_zoom + 1):
        n = 2 ** z
        pts = [(lat, lon) for lat, lon in route] + [center]
        # densify so no tile along the route is skipped
        dense = []
        for (a, b), (c, d) in zip(pts, pts[1:]):
            steps = 8
            dense += [(a + (c - a) * i / steps, b + (d - b) * i / steps) for i in range(steps)]
        dense += pts[-2:]
        for lat, lon in dense:
            x, y = tile_xy(lat, lon, z)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    ty = int(y) + dy
                    if 0 <= ty < n:
                        tiles.add((z, (int(math.floor(x)) + dx) % n, ty))
    return sorted(tiles)


def fetch_tile(t):
    z, x, y = t
    path = TILES / str(z) / f"{x}-{y}.pbf"
    if path.exists():
        return 0
    r = session.get(f"{TILE_SERVER}{z}/{x}/{y}.pbf", timeout=60)
    r.raise_for_status()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(r.content)
    return len(r.content)


def marine_labels(tiles, max_zoom):
    """Sea/ocean names (from the tiles' marine_label layer) near the route."""
    import subprocess
    seas = {}
    for z in range(2, min(5, max_zoom) + 1):
        names = [f"{x}-{y}" for tz, x, y in tiles if tz == z]
        try:
            out = subprocess.run(["node", str(ROOT / "inflight" / "marine_labels.js"), str(TILES), str(z), *names],
                                 capture_output=True, text=True, timeout=120, check=True).stdout
        except Exception as e:
            log(f"could not read sea names from tiles: {e}")
            return []
        for lab in json.loads(out):
            key = (lab["name"], round(lab["lat"], 1), round(lab["lon"], 1))
            seas.setdefault(key, {"name": lab["name"], "lat": lab["lat"], "lon": lab["lon"], "rank": lab["rank"]})
    return list(seas.values())


# --- timing ---------------------------------------------------------------------

def parse_duration(s):
    m = re.fullmatch(r"\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?\s*", s or "")
    if not s or not m or not any(m.groups()):
        return None
    return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60


def parse_hhmm(s):
    m = re.fullmatch(r"\s*(\d{1,2})[:.]?(\d{2})\s*", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def airborne_from_block(block_s):
    """Scheduled gate-to-gate time minus taxiing: ~15 min on short hops, up to 25 min on long-haul."""
    taxi_min = max(12, min(25, 0.25 * block_s / 60))
    return int(block_s - taxi_min * 60)


def block_from_times(dep, arr, tz_o, tz_d):
    today = datetime.now(ZoneInfo(tz_o)).date()
    d = datetime(today.year, today.month, today.day, *dep, tzinfo=ZoneInfo(tz_o))
    a = datetime(today.year, today.month, today.day, *arr, tzinfo=ZoneInfo(tz_d))
    while a <= d:
        a += timedelta(days=1)
    return int((a - d).total_seconds())


def ask(q):
    if not sys.stdin.isatty():
        return ""
    try:
        return input(q)
    except EOFError:
        return ""


# --- main -------------------------------------------------------------------------

def prep(flight, duration=None, dep=None, arr=None, max_zoom=7, no_llm=False):
    flight = re.sub(r"\s+", "", flight).upper()
    print(f"✈  Preparing {flight}")
    fr = lookup_route(flight)
    o, d = fr["origin"], fr["destination"]
    olat, olon, dlat, dlon = o["latitude"], o["longitude"], d["latitude"], d["longitude"]
    total = geo.haversine(olat, olon, dlat, dlon)
    log(f"{o['iata_code']} {o['municipality']} → {d['iata_code']} {d['municipality']}  "
        f"({total:,.0f} km great circle) · {fr.get('airline', {}).get('name', '?')}")

    log("loading GeoNames cities …")
    cities = load_geonames()
    oc, dc = airport_city(cities, o), airport_city(cities, d)
    tz_o, tz_d = oc["tz"], dc["tz"]

    # timing
    airborne = None
    if duration:
        airborne = parse_duration(duration)
    elif dep and arr:
        airborne = airborne_from_block(block_from_times(parse_hhmm(dep), parse_hhmm(arr), tz_o, tz_d))
    else:
        print(f"\n  Scheduled times make the dashboard accurate (Enter to estimate instead).")
        dep = parse_hhmm(ask(f"  Scheduled departure, local {o['iata_code']} time (HH:MM): "))
        arr = parse_hhmm(ask(f"  Scheduled arrival, local {d['iata_code']} time (HH:MM): ")) if dep else None
        if dep and arr:
            airborne = airborne_from_block(block_from_times(dep, arr, tz_o, tz_d))
    estimated = airborne is None
    if estimated:
        airborne = int((18 + total / 840 * 60) * 60)
    airborne = max(airborne, 20 * 60)
    log(f"airborne time {'(estimated) ' if estimated else ''}{airborne // 3600}h{airborne % 3600 // 60:02d}m")

    route = geo.great_circle(olat, olon, dlat, dlon, 400)

    log("finding cities along the route …")
    corridor = corridor_cities(cities, (olat, olon), (dlat, dlon), total, 150)
    forced = [{**oc, "xt": 0.0, "at": 0.0}, {**dc, "xt": 0.0, "at": round(total, 1)}]
    featured = pick_featured(corridor, total, forced)
    log(f"{len(corridor)} cities in a 150 km corridor, {len(featured)} featured")

    use_llm = not no_llm and bool(os.environ.get("OPENAI_API_KEY"))
    log(f"fetching Wikipedia articles{' + OpenAI fun facts' if use_llm else ''} …")
    enriched = []
    with ThreadPoolExecutor(2) as ex:
        futs = [ex.submit(enrich, c, use_llm) for c in featured]
        for i, f in enumerate(as_completed(futs), 1):
            enriched.append(f.result())
            print(f"\r    {i}/{len(futs)} cities", end="", flush=True)
    print()
    enriched.sort(key=lambda c: c["at"])
    errs = [c for c in enriched if "llm_error" in c]
    if errs:
        log(f"OpenAI failed for {len(errs)} cities (kept Wikipedia facts): {errs[0]['llm_error']}")

    tiles = needed_tiles(route, max_zoom)
    log(f"downloading {len(tiles)} MapSCII vector tiles (zoom 0–{max_zoom}) …")
    got, failed = 0, 0
    with ThreadPoolExecutor(8) as ex:
        futs = [ex.submit(fetch_tile, t) for t in tiles]
        for i, f in enumerate(as_completed(futs), 1):
            try:
                got += f.result()
            except Exception:
                failed += 1
            print(f"\r    {i}/{len(tiles)} tiles ({got / 1e6:.1f} MB new)", end="", flush=True)
    print()
    if failed:
        log(f"warning: {failed} tiles failed to download (map will have holes)")

    seas = marine_labels(tiles, max_zoom)
    # keep only the seas the dashboard would name: nearest label at route points with no town nearby
    seas = marine_labels(tiles, max_zoom)
    near_seas = {}
    for lat, lon in route[::2]:
        lon = (lon + 540) % 360 - 180
        if any(geo.haversine(lat, lon, c["lat"], c["lon"]) < 150 for c in corridor):
            continue
        sea = geo.nearest_sea(seas, lat, lon)
        if sea:
            near_seas[(sea["name"], sea["lat"], sea["lon"])] = sea
    near_seas = list(near_seas.values())
    sea_names = sorted({s["name"] for s in near_seas})
    sea_info = {}
    if sea_names:
        log(f"over water: {', '.join(sea_names)} — fetching facts …")
        with ThreadPoolExecutor(2) as ex:
            futs = {ex.submit(enrich, {"name": n, "sea": True}, use_llm): n for n in sea_names}
            for i, f in enumerate(as_completed(futs), 1):
                info = f.result()
                sea_info[futs[f]] = {k: info.get(k) for k in ("wiki", "description", "summary", "facts", "facts_source")}
                print(f"\r    {i}/{len(futs)} seas", end="", flush=True)
        print()

    model = FlightModel(route, airborne)
    data = {
        "flight": flight,
        "callsign_icao": fr.get("callsign_icao"),
        "airline": fr.get("airline") or {},
        "origin": {**o, "tz": tz_o, "city": oc["name"]},
        "destination": {**d, "tz": tz_d, "city": dc["name"]},
        "distance_km": total,
        "airborne_s": airborne,
        "airborne_estimated": estimated,
        "cruise_ft": model.cruise_ft,
        "route": route,
        "max_zoom": max_zoom,
        "featured": enriched,
        "seas": [{**s, **sea_info.get(s["name"], {})} for s in near_seas],
        "corridor": [{k: c[k] for k in ("name", "lat", "lon", "country", "region", "pop", "tz", "xt", "at")}
                     for c in corridor],
        "prepared_at": datetime.now().isoformat(timespec="seconds"),
    }
    FLIGHTS.mkdir(exist_ok=True)
    out = FLIGHTS / f"{flight}.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(f"\n✔  Saved {out.relative_to(ROOT)} — you can go offline now.")
    print(f"   On board run:  ./fly board {flight}")
