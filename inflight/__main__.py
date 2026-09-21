import argparse
import os
import sys

from . import prep as prep_mod


def load_dotenv(path=prep_mod.ROOT / ".env"):
    """Minimal .env loader: KEY=value lines; variables already set in the shell win."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("'\"")
        if value:
            os.environ.setdefault(key.strip(), value)


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(prog="fly", description="Offline in-terminal flight dashboard")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prep", help="download everything for a flight (needs internet)")
    p.add_argument("flight", help="flight number / callsign, e.g. SK1415 or SAS1415")
    p.add_argument("--dep", help="scheduled departure, local origin time HH:MM")
    p.add_argument("--arr", help="scheduled arrival, local destination time HH:MM")
    p.add_argument("--duration", help="airborne time, e.g. 1h05m (overrides --dep/--arr)")
    p.add_argument("--max-zoom", type=int, default=7, help="deepest map zoom to cache (default 7)")
    p.add_argument("--no-llm", action="store_true", help="skip OpenAI fun facts even if OPENAI_API_KEY is set")

    f = sub.add_parser("board", help="run the offline dashboard")
    f.add_argument("flight", nargs="?", help="prepared flight (default: most recently prepared)")
    f.add_argument("--takeoff", help="wheels-up time: HH:MM (laptop clock), 'now', or -25m / +10m")
    f.add_argument("--duration", help="actual time in the air if known (e.g. 45m, 1h10m); overrides the prepared estimate")
    f.add_argument("--speed", type=float, default=1.0, help="time multiplier for demos, e.g. 60")

    sub.add_parser("list", help="list prepared flights")

    a = ap.parse_args()
    if a.cmd == "prep":
        prep_mod.prep(a.flight, a.duration, a.dep, a.arr, a.max_zoom, a.no_llm)
    elif a.cmd == "list":
        for p in sorted(prep_mod.FLIGHTS.glob("*.json")):
            print(p.stem)
    else:
        from .app import run
        run(a.flight, a.takeoff, a.speed, a.duration)


if __name__ == "__main__":
    sys.exit(main())
