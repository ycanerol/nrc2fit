"""Convert a Nike Run Club data export (a zip with tcx/*.tcx) into FIT files.

Why not import Nike's TCX directly:
  - every value is wrapped in whitespace/newlines, which tcxreader-based importers reject;
  - GPS, heart rate, distance and cadence are separate trackpoints, and GPS-only parsers drop the rest;
  - TCX can't carry moving time, so pauses inflate duration/pace;
  - the run name lives in a Nike extension that TCX importers ignore.

Output per run: file_id, workout (name), timer events, records (GPS points with heart rate,
cadence, altitude and distance merged in by time), lap, session (moving time = Nike's lap time,
sport/sub_sport, feel, RPE) and activity messages.

Usage: nrc2fit <export.zip|tcx dir> <out dir> [--skip-no-gps]
"""

import argparse
import bisect
import csv
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.workout_message import WorkoutMessage
from fit_tool.profile.profile_type import Activity, Event, EventType, FileType, Manufacturer, Sport, SubSport
from timezonefinder import TimezoneFinder

NS = {
    "t": "http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2",
    "nax": "https://www.nike.com/xmlschemas/NikeActivityExtension/v1",
}
MATCH_TOLERANCE_S = 10  # max gap when attaching HR/cadence/altitude samples to a GPS point
FEEL = {"INJURED": 0, "TIRED": 25, "SO_SO": 50, "GREAT": 75, "UNSTOPPABLE": 100}  # FIT workout_feel 0-100

_tzf = TimezoneFinder()


def text(el, path):
    v = el.findtext(path, namespaces=NS)
    return v.strip() if v is not None else None


def ts(s):
    return datetime.fromisoformat(s.strip().replace("Z", "+00:00"))


def ms(dt):
    return round(dt.timestamp() * 1000)


def parse_run(data: bytes, source: str) -> dict:
    act = ET.fromstring(data).find(".//t:Activity", NS)
    lap = act.find("t:Lap", NS)
    gps, hr, dist, cad, alt, times = [], [], [], [], [], []
    for tp in act.iterfind(".//t:Trackpoint", NS):
        t = ts(text(tp, "t:Time"))
        times.append(t)
        if (v := text(tp, "t:HeartRateBpm/t:Value")) is not None:
            hr.append((t, int(float(v))))
        if (v := text(tp, "t:DistanceMeters")) is not None:
            dist.append((t, float(v)))
        if (v := text(tp, "t:Cadence")) is not None:
            cad.append((t, int(float(v))))
        if (v := text(tp, "t:AltitudeMeters")) is not None:
            alt.append((t, float(v)))
        if tp.find("t:Position", NS) is not None:
            gps.append((t, float(text(tp, "t:Position/t:LatitudeDegrees")), float(text(tp, "t:Position/t:LongitudeDegrees"))))

    # Nike tags are a flat <Key/><Value/> sequence inside one <Tag>.
    tags = {}
    tag = act.find("t:Extensions/nax:NAX/nax:Tags/nax:Tag", NS)
    if tag is not None:
        keys = [e.text.strip() for e in tag.findall("nax:Key", NS)]
        vals = [(e.text or "").strip() for e in tag.findall("nax:Value", NS)]
        tags = dict(zip(keys, vals))

    start = ts(lap.get("StartTime"))
    moving = float(text(lap, "t:TotalTimeSeconds"))
    for s in (gps, hr, dist, cad, alt):
        s.sort(key=lambda x: x[0])
    return {
        "source": source,
        "start": start,
        "end": max([start + timedelta(seconds=moving), *times]),
        "moving": moving,
        "distance": float(text(lap, "t:DistanceMeters")),
        "calories": int(float(text(lap, "t:Calories") or 0)),
        "gps": gps, "hr": hr, "dist": dist, "cad": cad, "alt": alt,
        "tags": tags,
    }


def nearest(series, t, tol=MATCH_TOLERANCE_S):
    if not series:
        return None
    i = bisect.bisect_left(series, t, key=lambda x: x[0])
    best = min((series[j] for j in (i - 1, i) if 0 <= j < len(series)), key=lambda x: abs((x[0] - t).total_seconds()))
    return best[1] if abs((best[0] - t).total_seconds()) <= tol else None


def interp(series, t):
    """Linear interpolation of a cumulative series (distance), clamped at both ends."""
    if not series:
        return None
    i = bisect.bisect_left(series, t, key=lambda x: x[0])
    if i == 0:
        return series[0][1]
    if i == len(series):
        return series[-1][1]
    (t0, v0), (t1, v1) = series[i - 1], series[i]
    span = (t1 - t0).total_seconds()
    return v0 if span == 0 else v0 + (v1 - v0) * (t - t0).total_seconds() / span


def local_tz(run, fallback):
    if run["gps"]:
        _, lat, lon = run["gps"][0]
        if name := _tzf.timezone_at(lat=lat, lng=lon):
            return name
    return fallback


def default_name(run, tz):
    """NRC-style name, e.g. 'Tuesday Evening Run', for runs exported without one."""
    local = run["start"].astimezone(ZoneInfo(tz))
    h = local.hour
    part = "Morning" if 4 <= h < 12 else "Afternoon" if h < 17 else "Evening" if h < 21 else "Night"
    return f"{local:%A} {part} Run"


def sub_sport(run):
    if run["tags"].get("location", "").upper() == "INDOORS" and not run["gps"]:
        return SubSport.TREADMILL
    if run["tags"].get("terrain", "").lower() == "trail":
        return SubSport.TRAIL
    return SubSport.GENERIC


def build_fit(run, name) -> bytes:
    start, end = run["start"], run["end"]
    sport_sub = sub_sport(run)

    # Record timeline: GPS points if there are any, otherwise whatever sensor samples exist.
    if run["gps"]:
        base = [(t, lat, lon) for t, lat, lon in run["gps"]]
    else:
        base = sorted({(t, None, None) for t, _ in run["dist"] + run["hr"] + run["cad"]}, key=lambda x: x[0])

    records, seen = [], set()
    for t, lat, lon in base:
        sec = int(t.timestamp())
        if sec in seen:  # FIT timestamps are whole seconds
            continue
        seen.add(sec)
        r = RecordMessage()
        r.timestamp = sec * 1000
        if lat is not None:
            r.position_lat, r.position_long = lat, lon
        if (v := nearest(run["alt"], t)) is not None:
            r.enhanced_altitude = v
        if (v := interp(run["dist"], t)) is not None:
            r.distance = v
        if (v := nearest(run["hr"], t)) is not None:
            r.heart_rate = v
        if (v := nearest(run["cad"], t)) is not None:
            r.cadence = round(v / 2)  # Nike: steps/min; FIT running cadence: strides/min
        records.append(r)

    hrs = [v for _, v in run["hr"] if v > 0]
    feel = FEEL.get(run["tags"].get("emotion", "").upper())
    rpe = run["tags"].get("rpe")

    b = FitFileBuilder(auto_define=True, min_string_size=50)

    m = FileIdMessage()
    m.type = FileType.ACTIVITY
    m.manufacturer = Manufacturer.DEVELOPMENT.value
    m.product = 0
    m.serial_number = int(start.timestamp())
    m.time_created = ms(start)
    b.add(m)

    m = WorkoutMessage()
    m.workout_name = name
    m.sport = Sport.RUNNING
    m.sub_sport = sport_sub
    b.add(m)

    m = EventMessage()
    m.event, m.event_type, m.timestamp = Event.TIMER, EventType.START, ms(start)
    b.add(m)
    b.add_all(records)
    m = EventMessage()
    m.event, m.event_type, m.timestamp = Event.TIMER, EventType.STOP_ALL, ms(end)
    b.add(m)

    def summary(m):
        m.timestamp = ms(end)
        m.start_time = ms(start)
        m.total_elapsed_time = (end - start).total_seconds()
        m.total_timer_time = run["moving"]
        m.total_distance = run["distance"]
        m.total_calories = run["calories"]
        if run["moving"] and run["distance"]:
            m.enhanced_avg_speed = run["distance"] / run["moving"]
        if hrs:
            m.avg_heart_rate = round(sum(hrs) / len(hrs))
            m.max_heart_rate = max(hrs)
        if run["gps"]:
            m.start_position_lat, m.start_position_long = run["gps"][0][1:]
            m.end_position_lat, m.end_position_long = run["gps"][-1][1:]
        m.sport = Sport.RUNNING
        m.sub_sport = sport_sub

    m = LapMessage()
    summary(m)
    m.event, m.event_type = Event.LAP, EventType.STOP
    b.add(m)

    m = SessionMessage()
    summary(m)
    m.event, m.event_type = Event.SESSION, EventType.STOP
    m.first_lap_index, m.num_laps = 0, 1
    if feel is not None:
        m.workout_feel = feel
    if rpe and rpe.isdigit():
        m.workout_rpe = int(rpe) * 10  # FIT/Endurain: 10-100
    b.add(m)

    m = ActivityMessage()
    m.timestamp = ms(end)
    m.total_timer_time = run["moving"]
    m.num_sessions = 1
    m.type = Activity.MANUAL
    m.event, m.event_type = Event.ACTIVITY, EventType.STOP
    b.add(m)

    return b.build().to_bytes()


def load_runs(src: Path):
    if src.is_dir():
        for p in sorted(src.glob("*.tcx")):
            yield parse_run(p.read_bytes(), p.name)
    else:
        with zipfile.ZipFile(src) as z:
            for n in sorted(z.namelist()):
                if n.endswith(".tcx"):
                    yield parse_run(z.read(n), Path(n).name)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--skip-no-gps", action="store_true", help="drop runs without a GPS track")
    args = ap.parse_args()

    runs = sorted(load_runs(args.src), key=lambda r: r["start"])

    # Nike exports the same run twice now and then: keep the copy with more tags.
    by_start = {}
    for r in runs:
        k = r["start"]
        if k not in by_start or len(r["tags"]) > len(by_start[k]["tags"]):
            by_start[k] = r
    dupes = len(runs) - len(by_start)
    runs = sorted(by_start.values(), key=lambda r: r["start"])

    args.out.mkdir(parents=True, exist_ok=True)
    rows, last_tz, skipped = [], "UTC", 0
    gps_tz = {i: local_tz(r, None) for i, r in enumerate(runs)}
    for i, run in enumerate(runs):
        if args.skip_no_gps and not run["gps"]:
            skipped += 1
            continue
        # No-GPS runs borrow the timezone of the closest run in time that has one.
        tz = gps_tz[i] or min(
            ((abs((runs[j]["start"] - run["start"]).total_seconds()), z) for j, z in gps_tz.items() if z),
            default=(0, "UTC"),
        )[1]
        name = run["tags"].get("com.nike.name") or default_name(run, tz)
        fname = f"nrc_{run['start']:%Y%m%dT%H%M%SZ}.fit"
        (args.out / fname).write_bytes(build_fit(run, name))
        local = run["start"].astimezone(ZoneInfo(tz))
        rows.append({
            "local_start": f"{local:%Y-%m-%d %H:%M}", "tz": tz, "name": name, "sub_sport": sub_sport(run).name.lower(),
            "km": round(run["distance"] / 1000, 2), "moving_min": round(run["moving"] / 60, 1),
            "elapsed_min": round((run["end"] - run["start"]).total_seconds() / 60, 1),
            "gps_points": len(run["gps"]), "hr_samples": len(run["hr"]),
            "feel": run["tags"].get("emotion", ""), "rpe": run["tags"].get("rpe", ""),
            "shoes": run["tags"].get("shoes", ""), "note": run["tags"].get("note", ""),
            "file": fname, "source": run["source"],
        })

    with open(args.out / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} FIT files to {args.out} ({dupes} duplicate(s) dropped, {skipped} no-GPS run(s) skipped)")


if __name__ == "__main__":
    main()
