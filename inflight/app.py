"""The offline in-flight dashboard."""
import asyncio
import json
import math
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Static

from . import geo
from .prep import FLIGHTS, ROOT, TILES
from .profile import FT_PER_M, FlightModel

BRIDGE = Path(__file__).with_name("map_bridge.js")


def fmt_dur(seconds):
    s = int(abs(seconds))
    h, m = s // 3600, s % 3600 // 60
    return f"{h}h{m:02d}m" if h else f"{m}m{s % 60:02d}s"


def fmt_pop(p):
    return f"{p / 1e6:.1f}M" if p >= 1e6 else f"{p / 1e3:.0f}k"


def parse_takeoff(s, now):
    """'now', 'HH:MM' (laptop clock; most recent past/near occurrence), or relative '-25m' / '+1h10m'."""
    s = (s or "now").strip().lower()
    if s in ("", "now"):
        return now
    m = re.fullmatch(r"([+-])\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m?)?", s)
    if m and (m.group(2) or m.group(3)):
        delta = timedelta(hours=int(m.group(2) or 0), minutes=int(m.group(3) or 0))
        return now + delta if m.group(1) == "+" else now - delta
    m = re.fullmatch(r"(\d{1,2})[:.]?(\d{2})", s)
    if m:
        t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if t - now > timedelta(hours=6):
            t -= timedelta(days=1)
        elif now - t > timedelta(hours=18):
            t += timedelta(days=1)
        return t
    raise ValueError(f"can't parse takeoff time {s!r}")


class MapBridge:
    """Long-running node process rendering MapSCII frames."""

    def __init__(self):
        self.proc = None
        self.lock = asyncio.Lock()
        self.seq = 0

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            "node", str(BRIDGE), str(TILES), cwd=str(ROOT), limit=2 ** 24,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)

    async def render(self, req):
        async with self.lock:
            if self.proc is None or self.proc.returncode is not None:
                await self.start()
            self.seq += 1
            req["id"] = self.seq
            self.proc.stdin.write((json.dumps(req) + "\n").encode())
            await self.proc.stdin.drain()
            while True:
                line = await asyncio.wait_for(self.proc.stdout.readline(), 20)
                if not line:
                    raise RuntimeError("map renderer exited")
                resp = json.loads(line)
                if resp.get("id") == self.seq:
                    if resp.get("error"):
                        raise RuntimeError(resp["error"].splitlines()[0])
                    return resp["frame"]

    def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


class FlightDashboard(App):
    CSS = """
    Screen { background: #0b0f17; }
    #title { height: 1; background: #1b2a41; color: #e8eef7; padding: 0 1; }
    #main { height: 1fr; }
    #map { width: 1fr; height: 1fr; border: round #3a6ea5; background: #000000; }
    #side { width: 60; }
    .panel { border: round #2f4f6f; padding: 0 1; height: auto; }
    #below { height: 1fr; }
    .panel, #map { border-title-color: #9fd3ff; border-title-style: bold; }
    """
    BINDINGS = [
        Binding("m", "map_mode", "Follow/overview"),
        Binding("plus,equals_sign", "zoom(0.5)", "Zoom in"),
        Binding("minus", "zoom(-0.5)", "Zoom out"),
        Binding("left", "warp(-300)", "-5 min"),
        Binding("right", "warp(300)", "+5 min"),
        Binding("0", "live", "Live"),
        Binding("f", "next_fact", "Next fact"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, flight, takeoff, speed):
        super().__init__()
        self.f = flight
        self.model = FlightModel([tuple(p) for p in flight["route"]], flight["airborne_s"])
        self.takeoff = takeoff
        self.speed = speed
        self.started = datetime.now().astimezone()
        self.warp = 0.0
        self.follow = True
        self.zoom = None
        self.fact_offset = 0
        self.bridge = MapBridge()
        self.map_busy = False
        self.map_error = None
        self.last_map = None
        self.tz_o = ZoneInfo(flight["origin"]["tz"])
        self.tz_d = ZoneInfo(flight["destination"]["tz"])
        self.featured = flight["featured"]
        self.corridor = flight["corridor"]
        self.sun_event = None
        self.sun_checked = None

    # --- time ---------------------------------------------------------------
    def now(self):
        real = datetime.now().astimezone()
        return real + (real - self.started) * (self.speed - 1) + timedelta(seconds=self.warp)

    def t(self):
        return (self.now() - self.takeoff).total_seconds()

    # --- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="title")
        with Horizontal(id="main"):
            yield Static(id="map")
            with Vertical(id="side"):
                yield Static(id="flight", classes="panel")
                yield Static(id="progress", classes="panel")
                yield Static(id="below", classes="panel")
                yield Static(id="next", classes="panel")
        yield Footer()

    def on_mount(self):
        self.query_one("#flight").border_title = "✈ Flight data"
        self.query_one("#progress").border_title = "⏱ Progress"
        self.query_one("#below").border_title = "⌖ Below you"
        self.query_one("#next").border_title = "➜ Coming up · ☀ Sky"
        self.tick()
        self.set_interval(1, self.tick)
        self.set_interval(4, self.refresh_map)
        self.call_after_refresh(self.refresh_map)

    def on_resize(self):
        self.call_after_refresh(self.refresh_map)

    async def on_unmount(self):
        self.bridge.stop()

    # --- actions ------------------------------------------------------------
    def action_map_mode(self):
        self.follow = not self.follow
        self.zoom = None
        self.refresh_map()

    def action_zoom(self, step):
        self.zoom = max(0.0, min(self.f["max_zoom"] + 0.95, (self.zoom or self.auto_zoom()) + step))
        self.refresh_map()

    def action_warp(self, seconds):
        self.warp += seconds
        self.tick()
        self.refresh_map()

    def action_live(self):
        self.warp = 0.0
        self.speed = 1.0
        self.started = datetime.now().astimezone()
        self.tick()
        self.refresh_map()

    def action_next_fact(self):
        self.fact_offset += 1
        self.tick()

    # --- map ------------------------------------------------------------------
    def map_size(self):
        r = self.query_one("#map").content_region
        return max(10, r.width), max(5, r.height)

    def overview_view(self):
        from .prep import tile_xy
        cols, rows = self.map_size()
        pts = [tile_xy(lat, lon, 0) for lat, lon in self.model.route]
        xs, ys = [p[0] * 256 for p in pts], [p[1] * 256 for p in pts]
        dx, dy = max(1e-6, max(xs) - min(xs)), max(1e-6, max(ys) - min(ys))
        zoom = math.log2(max(1e-9, min(cols * 2 * 0.8 / dx, rows * 4 * 0.75 / dy)))
        cx, cy = (max(xs) + min(xs)) / 2 / 256, (max(ys) + min(ys)) / 2 / 256
        lon = cx * 360 - 180
        lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * cy))))
        return (lat, lon), max(0.0, min(self.f["max_zoom"] + 0.95, zoom))

    def auto_zoom(self):
        if not self.follow:
            return self.overview_view()[1]
        # follow mode: show roughly 600 km across, capped to cached detail
        cols, _ = self.map_size()
        lat = self.model.route[len(self.model.route) // 2][0]
        m_per_px = 600_000 / (cols * 2)
        z = math.log2(math.cos(math.radians(lat)) * 2 * math.pi * 6378137 / (256 * m_per_px))
        return max(2.0, min(self.f["max_zoom"] + 0.95, z))

    def refresh_map(self):
        if not self.map_busy:
            self.run_worker(self._render_map, group="map")

    async def _render_map(self):
        self.map_busy = True
        try:
            st = self.model.state(self.t())
            cols, rows = self.map_size()
            if self.follow:
                center, zoom = (st["lat"], st["lon"]), self.zoom or self.auto_zoom()
            else:
                center, zoom = self.overview_view()
                zoom = self.zoom or zoom
            idx = st["idx"]
            route = self.model.route
            plane = [st["lat"], st["lon"]]
            o, d = self.f["origin"], self.f["destination"]
            req = {
                "center": {"lat": center[0], "lon": center[1]}, "zoom": zoom,
                "cols": cols, "rows": rows,
                "flown": [list(p) for p in route[:idx]] + [plane] if st["dist"] > 0 else [],
                "remaining": [plane] + [list(p) for p in route[idx:]],
                "plane": {"lat": st["lat"], "lon": st["lon"], "heading": st["heading"]},
                "marks": [{"lat": route[0][0], "lon": route[0][1], "label": o["iata_code"], "color": 231},
                          {"lat": route[-1][0], "lon": route[-1][1], "label": d["iata_code"], "color": 231}],
            }
            frame = await self.bridge.render(req)
            self.map_error = None
            widget = self.query_one("#map")
            widget.update(Text.from_ansi(frame.rstrip("\n"), no_wrap=True, end=""))
            mode = "follow" if self.follow else "overview"
            widget.border_title = f"🗺  MapSCII · {mode} · zoom {zoom:.1f}"
        except Exception as e:  # keep the dashboard alive without a map
            self.map_error = str(e)
            self.query_one("#map").update(Text(f"\n  Map unavailable: {e}\n  (is node installed and were tiles prepared?)", style="red"))
        finally:
            self.map_busy = False

    # --- helpers ----------------------------------------------------------------
    def nearest_ground(self, lat, lon):
        best = min(self.corridor, key=lambda c: geo.haversine(lat, lon, c["lat"], c["lon"]), default=None)
        if best is None:
            return None, None
        return best, geo.haversine(lat, lon, best["lat"], best["lon"])

    def nearest_sea(self, lat, lon):
        return geo.nearest_sea(self.f.get("seas", []), lat, lon)

    def nearest_featured(self, dist):
        return min(self.featured, key=lambda c: abs(c["at"] - dist), default=None)

    def local_tz(self, lat, lon):
        city, km = self.nearest_ground(lat, lon)
        if city and km < 400:
            try:
                return ZoneInfo(city["tz"])
            except Exception:
                pass
        offset = round(lon / 15)  # nautical time zone; Etc/GMT signs are inverted
        return ZoneInfo(f"Etc/GMT{-offset:+d}") if offset else ZoneInfo("UTC")

    def sun_forecast(self, now, t):
        """Next sunrise/sunset along the remaining flight (computed every 30 s)."""
        if self.sun_checked and abs((now - self.sun_checked).total_seconds()) < 30:
            return self.sun_event
        self.sun_checked = now
        self.sun_event = None
        T = self.model.T
        step = 120
        prev = None
        tt = max(0.0, t)
        while tt <= T:
            st = self.model.state(tt)
            el, _ = geo.sun_position(now + timedelta(seconds=tt - t), st["lat"], st["lon_wrapped"])
            if prev is not None and (prev < -0.833) != (el < -0.833):
                self.sun_event = ("Sunrise" if el > prev else "Sunset", tt - t)
                break
            prev = el
            tt += step
        return self.sun_event

    # --- rendering --------------------------------------------------------------
    def tick(self):
        now = self.now()
        t = self.t()
        m = self.model
        st = m.state(t)
        f = self.f
        o, d = f["origin"], f["destination"]

        # title bar
        title = Text()
        title.append(f" ✈ {f['flight']} ", style="bold black on #ffb347")
        title.append(f"  {f['airline'].get('name', '')}  ", style="#9fd3ff")
        title.append(f"{o['iata_code']} {o['city']}", style="bold white")
        title.append(" ━━▶ ", style="#ffb347")
        title.append(f"{d['iata_code']} {d['city']}", style="bold white")
        title.append(f"   {st['phase']}", style="bold #7CFC00" if 0 <= t < m.T else "bold yellow")
        extra = ""
        if self.speed != 1 or self.warp:
            extra = f"  [sim ×{self.speed:g} {'+' if self.warp >= 0 else '-'}{fmt_dur(self.warp)}]"
        title.append(f"   {now.strftime('%H:%M:%S')} laptop{extra}", style="#8899aa")
        self.query_one("#title").update(title)

        # flight data
        g = Table.grid(padding=(0, 1))
        g.add_column(style="#8899aa", width=11)
        g.add_column(style="bold white")
        vs = st["vs_fpm"]
        arrow = "▲" if vs > 150 else "▼" if vs < -150 else "▶"
        g.add_row("Altitude", f"{st['alt_ft']:,.0f} ft  ({st['alt_ft'] / FT_PER_M:,.0f} m)")
        g.add_row("Vert speed", f"{arrow} {vs:+,.0f} ft/min")
        g.add_row("Ground spd", f"{st['gs_kmh']:,.0f} km/h  ({st['gs_kmh'] / 1.852:,.0f} kt)")
        wind = st["wind_kmh"]
        wtxt = f"{abs(wind):.0f} km/h {'tailwind' if wind > 0 else 'headwind'}" if abs(wind) > 3 else "calm"
        g.add_row("Airspeed", f"{st['tas_kmh']:,.0f} km/h TAS · Mach {st['mach']:.2f}")
        g.add_row("Wind (est)", wtxt)
        g.add_row("Heading", f"{st['heading']:03.0f}° {geo.compass(st['heading'])}")
        g.add_row("Outside", f"{st['oat_c']:.0f} °C · {st['pressure_hpa']:.0f} hPa")
        g.add_row("Position", f"{geo.dms(st['lat'], 'N', 'S')}  {geo.dms(st['lon_wrapped'], 'E', 'W')}")
        if st["alt_ft"] > 500:
            g.add_row("Horizon", f"you can see ~{3.57 * math.sqrt(st['alt_ft'] / FT_PER_M):.0f} km")
        self.query_one("#flight").update(g)

        # progress
        w = max(10, self.query_one("#progress").content_region.width - 2)
        frac = max(0.0, min(1.0, st["dist"] / m.D)) if m.D else 0
        pos = int(frac * (w - 1))
        bar = Text()
        bar.append("━" * pos, style="#ffb347")
        bar.append("✈", style="bold yellow")
        bar.append("─" * (w - 1 - pos), style="#3a6ea5")
        p = Table.grid(padding=(0, 1))
        p.add_column(style="#8899aa", width=11)
        p.add_column(style="bold white")
        eta = self.takeoff + timedelta(seconds=m.T)
        if t < 0:
            p.add_row("Takeoff in", fmt_dur(-t))
        elif t < m.T:
            p.add_row("Elapsed", f"{fmt_dur(t)}   remaining {fmt_dur(m.T - t)}")
        else:
            p.add_row("Landed", f"{fmt_dur(t - m.T)} ago — welcome to {d['city']}!")
        p.add_row("Distance", f"{st['dist']:,.0f} / {m.D:,.0f} km  ({frac * 100:.1f}%)")
        p.add_row("Wheels up", f"{self.takeoff.astimezone(self.tz_o):%H:%M} {o['iata_code']} time")
        est = " (est.)" if f.get("airborne_estimated") else ""
        p.add_row("Landing", f"{eta.astimezone(self.tz_d):%H:%M} {d['iata_code']} time{est}")
        here = now.astimezone(self.local_tz(st["lat"], st["lon_wrapped"]))
        p.add_row("Local time", f"{here:%H:%M} below · {now.astimezone(self.tz_o):%H:%M} {o['iata_code']} · "
                                f"{now.astimezone(self.tz_d):%H:%M} {d['iata_code']}")
        prog = Table.grid()
        prog.add_row(bar)
        prog.add_row(p)
        self.query_one("#progress").update(prog)

        # below you
        below = Text()
        city, km = self.nearest_ground(st["lat"], st["lon_wrapped"])
        if city and km < 150:
            where = ", ".join(x for x in (city["region"], city["country"]) if x and x != city["name"])
            if km < 12:
                below.append("Right above ", style="#8899aa")
            else:
                dirn = geo.compass(geo.bearing(city["lat"], city["lon"], st["lat"], st["lon_wrapped"]))
                below.append(f"{km:.0f} km {dirn} of ", style="#8899aa")
            below.append(city["name"], style="bold #7CFC00")
            below.append(f"  ({where} · pop {fmt_pop(city['pop'])})\n", style="#8899aa")
        else:
            sea = self.nearest_sea(st["lat"], st["lon_wrapped"])
            below.append(f"Over open water · {sea['name']}" if sea else "Over open water or remote terrain",
                         style="bold #5fafff")
            if city and km < 600:
                below.append(f" — nearest town {city['name']} ({km:.0f} km)", style="#8899aa")
            below.append("\n")
        fc = self.nearest_featured(st["dist"])
        subject, rel = None, ""
        if fc:
            gap = fc["at"] - st["dist"]
            rel = "right below" if abs(gap) < 15 else (f"{gap:.0f} km ahead" if gap > 0 else f"{-gap:.0f} km behind")
            subject = fc
            if (not city or km >= 150) and abs(gap) > 250:
                sea = self.nearest_sea(st["lat"], st["lon_wrapped"])
                if sea and sea.get("facts"):
                    subject, rel = sea, "all around you"
        if subject:
            below.append(f"\n{subject['name']}", style="bold #ffb347")
            below.append(f"  {subject.get('description') or subject.get('country', '')} · {rel}\n", style="#8899aa")
            if subject.get("summary"):
                below.append(subject["summary"] + "\n", style="#c8d3e0")
            facts = subject.get("facts") or []
            if facts:
                i = (int(max(t, 0) // 30) + self.fact_offset) % len(facts)
                src = " (AI)" if subject.get("facts_source") == "openai" and i < 3 else ""
                below.append(f"\n💡 Fun fact {i + 1}/{len(facts)}{src}\n", style="bold #ffd75f")
                below.append(facts[i], style="italic #ffffff")
        self.query_one("#below").update(below)

        # coming up + sky
        nxt = Table.grid(padding=(0, 1))
        nxt.add_column(style="bold white", no_wrap=True)
        nxt.add_column(style="#8899aa", no_wrap=True)
        nxt.add_column(style="#9fd3ff", justify="right", no_wrap=True)
        upcoming = [c for c in self.featured if c["at"] > st["dist"] + 5][:4]
        for c in upcoming:
            tt = m.time_at_distance(c["at"]) - max(t, 0)
            nxt.add_row(c["name"][:20], c["country"][:16], f"in {fmt_dur(tt)}")
        if not upcoming:
            nxt.add_row("—", "nothing more ahead", "")
        el, az = geo.sun_position(now, st["lat"], st["lon_wrapped"])
        rel = (az - st["heading"] + 540) % 360 - 180
        side = "ahead" if abs(rel) < 30 else "behind" if abs(rel) > 150 else ("RIGHT" if rel > 0 else "LEFT")
        sky = Text()
        if el > -0.833:
            sky.append(f"☀ Sun {el:.0f}° up, {side} side of the aircraft", style="#ffd75f")
        elif el > -12:
            sky.append(f"🌆 Twilight, sun {-el:.0f}° below horizon ({side})", style="#ff9f5f")
        else:
            sky.append(f"🌙 Night — sun {-el:.0f}° below horizon", style="#9fa8ff")
        if 0 <= t < m.T:
            ev = self.sun_forecast(now, t)
            if ev:
                sky.append(f"\n   {ev[0]} on board in ~{fmt_dur(ev[1])}", style="#8899aa")
        wrap = Table.grid()
        wrap.add_row(nxt)
        wrap.add_row(sky)
        self.query_one("#next").update(wrap)


def run(flight_name, takeoff_arg, speed, duration=None):
    if flight_name:
        path = FLIGHTS / f"{re.sub(r'\s+', '', flight_name).upper()}.json"
    else:
        found = sorted(FLIGHTS.glob("*.json"), key=lambda p: p.stat().st_mtime)
        if not found:
            sys.exit("No prepared flights. Run `./fly prep <FLIGHT>` while online first.")
        path = found[-1]
    if not path.exists():
        sys.exit(f"{path.name} not prepared. Run `./fly prep {flight_name}` while online first.")
    flight = json.loads(path.read_text())
    if duration:
        from .prep import parse_duration
        seconds = parse_duration(duration)
        if not seconds:
            sys.exit(f"can't parse duration {duration!r} (try 45m or 1h10m)")
        flight["airborne_s"], flight["airborne_estimated"] = seconds, False
    now = datetime.now().astimezone()
    o, d = flight["origin"], flight["destination"]
    print(f"✈  {flight['flight']}  {o['iata_code']} → {d['iata_code']}  "
          f"(airborne ~{fmt_dur(flight['airborne_s'])})")
    while True:
        s = takeoff_arg
        if s is None:
            s = input(f"   Wheels-up time (HH:MM laptop clock, 'now', or e.g. -20m) [now]: ")
        try:
            takeoff = parse_takeoff(s, now)
            break
        except ValueError as e:
            print(f"   {e}")
            takeoff_arg = None
    FlightDashboard(flight, takeoff, speed).run()
