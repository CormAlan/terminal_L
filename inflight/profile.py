"""A simple physical model of a jet flight: climb, (step-)cruise, descent.

Everything is a function of seconds since wheels-up. The speed profile is
scaled so the aircraft arrives exactly at the destination at the scheduled
airborne time; the scale factor is reported as an implied wind component.
"""
import bisect
import math

from . import geo

FT_PER_M = 3.28084


def isa(alt_ft):
    """(temperature °C, speed of sound m/s, pressure hPa) in the ISA atmosphere."""
    h = alt_ft / FT_PER_M
    if h < 11000:
        t = 288.15 - 0.0065 * h
        p = 1013.25 * (t / 288.15) ** 5.2559
    else:
        t = 216.65
        p = 226.32 * math.exp(-(h - 11000) / 6341.6)
    return t - 273.15, 20.0468 * math.sqrt(t), p


class FlightModel:
    def __init__(self, route, airborne_s):
        self.route = route                      # list of (lat, lon), lon unwrapped
        self.T = float(airborne_s)
        self.cum = [0.0]
        for (a, b), (c, d) in zip(route, route[1:]):
            self.cum.append(self.cum[-1] + geo.haversine(a, b, c, d))
        self.D = self.cum[-1]

        D = self.D
        self.long_haul = D > 3000
        cruise = 35000 if self.long_haul else min(37000, max(24000, 21000 + 30 * D))
        tc, td = cruise / 1800 * 60, cruise / 1500 * 60
        budget = 0.85 * self.T
        if tc + td > budget:
            s = budget / (tc + td)
            cruise, tc, td = cruise * s, tc * s, td * s
        self.cruise_ft, self.tc, self.td = cruise, tc, td
        self.mach = 0.78 + 0.05 * min(1.0, D / 6000)

        # integrate nominal TAS to calibrate ground speed
        n = 3000
        self.tgrid = [self.T * i / n for i in range(n + 1)]
        tas = [self.tas_kmh(t) for t in self.tgrid]
        dist = [0.0]
        for i in range(n):
            dist.append(dist[-1] + (tas[i] + tas[i + 1]) / 2 * (self.T / n) / 3600)
        self.k = D / dist[-1] if dist[-1] > 0 else 1.0
        self.dgrid = [d * self.k for d in dist]

    # --- vertical profile -------------------------------------------------
    def cruise_level(self, t):
        if not self.long_haul:
            return self.cruise_ft
        span = self.T - self.tc - self.td
        f = (t - self.tc) / span if span > 0 else 0
        return self.cruise_ft + 2000 * min(2, int(max(0.0, f) * 3))

    def altitude_ft(self, t):
        if t <= 0 or t >= self.T:
            return 0.0
        if t < self.tc:
            u = t / self.tc
            return self.cruise_ft * (1 - (1 - u) ** 1.8)
        if t > self.T - self.td:
            top = self.cruise_level(self.T - self.td - 1)
            v = (t - (self.T - self.td)) / self.td
            return top * (1 - v) * (1 - 0.35 * v)
        return self.cruise_level(t)

    def vertical_speed_fpm(self, t):
        dt = 5.0
        return (self.altitude_ft(t + dt) - self.altitude_ft(t - dt)) / (2 * dt) * 60

    def phase(self, t):
        if t < 0:
            return "PRE-DEPARTURE"
        if t >= self.T:
            return "LANDED"
        if t < self.tc:
            return "TAKEOFF" if t < 90 else "CLIMB"
        if t > self.T - self.td:
            return "FINAL APPROACH" if self.T - t < 240 else "DESCENT"
        vs = self.vertical_speed_fpm(t)
        return "STEP CLIMB" if vs > 100 else "CRUISE"

    # --- speeds -----------------------------------------------------------
    def cruise_tas_kmh(self, alt):
        _, a, _ = isa(alt)
        return self.mach * a * 3.6

    def tas_kmh(self, t):
        if t < 0 or t > self.T:
            return 0.0
        alt = self.altitude_ft(t)
        vc = self.cruise_tas_kmh(max(alt, 1))
        if t < self.tc:
            u = t / self.tc
            return 300 + (vc - 300) * math.sqrt(u)
        if t > self.T - self.td:
            v = (t - (self.T - self.td)) / self.td
            top = self.cruise_tas_kmh(self.cruise_level(self.T - self.td - 1))
            return top + (260 - top) * v ** 1.5
        return vc

    def ground_speed_kmh(self, t):
        return self.tas_kmh(t) * self.k

    # --- position ---------------------------------------------------------
    def distance_km(self, t):
        if t <= 0:
            return 0.0
        if t >= self.T:
            return self.D
        i = min(len(self.tgrid) - 2, int(t / self.T * (len(self.tgrid) - 1)))
        f = (t - self.tgrid[i]) / (self.tgrid[i + 1] - self.tgrid[i])
        return self.dgrid[i] + f * (self.dgrid[i + 1] - self.dgrid[i])

    def time_at_distance(self, d):
        """Inverse of distance_km: seconds after takeoff when distance d is reached."""
        d = max(0.0, min(self.D, d))
        i = max(1, bisect.bisect_left(self.dgrid, d))
        d0, d1 = self.dgrid[i - 1], self.dgrid[i]
        f = (d - d0) / (d1 - d0) if d1 > d0 else 0
        return self.tgrid[i - 1] + f * (self.tgrid[i] - self.tgrid[i - 1])

    def point_at_distance(self, d):
        """(lat, lon, route index) at distance d along the route."""
        d = max(0.0, min(self.D, d))
        i = max(1, bisect.bisect_left(self.cum, d))
        i = min(i, len(self.cum) - 1)
        d0, d1 = self.cum[i - 1], self.cum[i]
        f = (d - d0) / (d1 - d0) if d1 > d0 else 0
        (a, b), (c, e) = self.route[i - 1], self.route[i]
        return a + f * (c - a), b + f * (e - b), i

    def state(self, t):
        d = self.distance_km(t)
        lat, lon, idx = self.point_at_distance(d)
        la2, lo2, _ = self.point_at_distance(min(self.D, d + 5))
        la1, lo1, _ = self.point_at_distance(max(0.0, d - 5))
        hdg = geo.bearing(la1, lo1, la2, lo2)
        alt = self.altitude_ft(t)
        temp, a, pres = isa(alt)
        tas = self.tas_kmh(t)
        return {
            "t": t, "dist": d, "lat": lat, "lon": lon, "lon_wrapped": (lon + 540) % 360 - 180,
            "idx": idx, "heading": hdg, "alt_ft": alt, "vs_fpm": self.vertical_speed_fpm(t),
            "tas_kmh": tas, "gs_kmh": tas * self.k, "wind_kmh": tas * (self.k - 1),
            "mach": tas / 3.6 / a if tas else 0.0, "oat_c": temp, "pressure_hpa": pres,
            "phase": self.phase(t),
        }
