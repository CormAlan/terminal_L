"""Spherical geometry, great-circle routing and solar position."""
import math
from datetime import datetime, timezone

R_KM = 6371.0088


def haversine(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_KM * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def interpolate(lat1, lon1, lat2, lon2, f):
    """Point at fraction f along the great circle between two points."""
    p1, l1, p2, l2 = map(math.radians, (lat1, lon1, lat2, lon2))
    d = haversine(lat1, lon1, lat2, lon2) / R_KM
    if d < 1e-9:
        return lat1, lon1
    a = math.sin((1 - f) * d) / math.sin(d)
    b = math.sin(f * d) / math.sin(d)
    x = a * math.cos(p1) * math.cos(l1) + b * math.cos(p2) * math.cos(l2)
    y = a * math.cos(p1) * math.sin(l1) + b * math.cos(p2) * math.sin(l2)
    z = a * math.sin(p1) + b * math.sin(p2)
    return math.degrees(math.atan2(z, math.hypot(x, y))), math.degrees(math.atan2(y, x))


def great_circle(lat1, lon1, lat2, lon2, n=200):
    """n+1 points along the route, longitudes unwrapped so the line never jumps at ±180."""
    pts = [interpolate(lat1, lon1, lat2, lon2, i / n) for i in range(n + 1)]
    out = [pts[0]]
    for lat, lon in pts[1:]:
        prev = out[-1][1]
        while lon - prev > 180:
            lon -= 360
        while lon - prev < -180:
            lon += 360
        out.append((lat, lon))
    return out


def cross_along_track(lat1, lon1, lat2, lon2, plat, plon):
    """(cross-track km, along-track km) of point P relative to the route 1->2."""
    d13 = haversine(lat1, lon1, plat, plon) / R_KM
    t13 = math.radians(bearing(lat1, lon1, plat, plon))
    t12 = math.radians(bearing(lat1, lon1, lat2, lon2))
    xt = math.asin(max(-1.0, min(1.0, math.sin(d13) * math.sin(t13 - t12))))
    c = math.cos(d13) / max(1e-12, math.cos(xt))
    at = math.acos(max(-1.0, min(1.0, c)))
    if math.cos(t13 - t12) < 0:
        at = -at
    return xt * R_KM, at * R_KM


def compass(deg):
    names = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return names[int((deg % 360) / 22.5 + 0.5) % 16]


def dms(value, pos, neg):
    h = pos if value >= 0 else neg
    v = abs(value)
    d = int(v)
    m = (v - d) * 60
    return f"{d:3d}°{m:05.2f}' {h}"


def sun_position(when: datetime, lat, lon):
    """(elevation°, azimuth°) of the sun, NOAA approximation (~0.5° accuracy)."""
    when = when.astimezone(timezone.utc)
    doy = when.timetuple().tm_yday
    hour = when.hour + when.minute / 60 + when.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    tst = hour * 60 + eqtime + 4 * lon
    ha = math.radians(tst / 4 - 180)
    phi = math.radians(lat)
    cos_zen = math.sin(phi) * math.sin(decl) + math.cos(phi) * math.cos(decl) * math.cos(ha)
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    elev = 90 - math.degrees(zen)
    az = math.degrees(math.atan2(math.sin(ha),
                                 math.cos(ha) * math.sin(phi) - math.tan(decl) * math.cos(phi))) + 180
    return elev, az % 360


def nearest_sea(seas, lat, lon, max_km=2500.0):
    """Closest sea/ocean label; oceans have distant label points, so their distance is discounted."""
    best, score = None, max_km
    for sea in seas:
        d = haversine(lat, lon, sea["lat"], sea["lon"]) / (1 + 0.6 * (3 - min(sea["rank"], 3)))
        if d < score:
            best, score = sea, d
    return best
