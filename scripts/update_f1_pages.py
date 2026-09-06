#!/usr/bin/env python3

from __future__ import annotations

import html
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.json"
ETAG_FILE = ROOT / "data" / "source-etags.json"
STATUS_FILE = ROOT / "data" / "update-status.json"

INKYCLOUD_BASE = "https://f1.inkycloud.click"
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"

OUTPUT_CALENDAR = ROOT / "calendar.bmp"
OUTPUT_TEAMS = ROOT / "teams.bmp"
OUTPUT_STANDINGS = ROOT / "standings.bmp"
OUTPUT_LAST_RACE = ROOT / "last-race.bmp"
OUTPUT_DATA = ROOT / "f1-data.json"
OUTPUT_HA = ROOT / "homeassistant.html"
OUTPUT_INDEX = ROOT / "index.html"

USER_AGENT = "f1-e1002-static-mirror/1.0 (+GitHub Actions)"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# Spectra-6-friendly, high-contrast palette.
WHITE = (255, 255, 255)
BLACK = (20, 20, 20)
GRAY = (120, 120, 120)
LIGHT = (238, 238, 238)
RED = (205, 35, 35)
YELLOW = (244, 194, 13)
BLUE = (33, 105, 175)
GREEN = (46, 132, 79)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_json(url: str):
    response = SESSION.get(url, timeout=45)
    response.raise_for_status()
    return response.json()


def fetch_bmp_with_etag(url: str, params: dict, destination: Path, etag_key: str, etags: dict) -> str:
    headers = {}
    if etags.get(etag_key):
        headers["If-None-Match"] = etags[etag_key]

    response = SESSION.get(url, params=params, headers=headers, timeout=60)

    if response.status_code == 304 and destination.exists():
        return "unchanged"

    response.raise_for_status()

    image = Image.open(io.BytesIO(response.content))
    if image.size != (800, 480):
        raise RuntimeError(f"Unexpected InkyCloud image size {image.size}; expected 800x480")

    image.save(destination, format="BMP")

    etag = response.headers.get("ETag")
    if etag:
        etags[etag_key] = etag

    return "updated"



def optimize_teams_bmp(path: Path) -> None:
    """Crop InkyCloud's outer black border and scale useful content to 800x480."""
    if not path.exists():
        return

    image = Image.open(path).convert("RGB")
    if image.size != (800, 480):
        return

    mask = image.convert("L").point(lambda p: 255 if p > 45 else 0)
    bbox = mask.getbbox()
    if not bbox:
        return

    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top

    # Once optimized, the content already reaches almost the full canvas.
    if width >= 790 and height >= 470:
        return

    pad = 3
    left = max(0, left - pad)
    top = max(0, top - pad)
    right = min(800, right + pad)
    bottom = min(480, bottom + pad)

    cropped = image.crop((left, top, right, bottom))
    resized = cropped.resize((800, 480), Image.Resampling.LANCZOS)
    resized.save(path, "BMP")


def font(size: int, bold: bool = False):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def text_width(draw: ImageDraw.ImageDraw, text: str, fnt) -> int:
    box = draw.textbbox((0, 0), text, font=fnt)
    return box[2] - box[0]


def short_name(given: str, family: str) -> str:
    given = (given or "").strip()
    family = (family or "").strip()
    if not given:
        return family
    return f"{given[0]}. {family}"


def parse_race_datetime(race: dict) -> datetime | None:
    date = race.get("date")
    if not date:
        return None
    time_value = race.get("time") or "00:00:00Z"
    stamp = f"{date}T{time_value.replace('Z', '+00:00')}"
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


def session_datetime(session: dict | None) -> datetime | None:
    if not session or not session.get("date"):
        return None
    time_value = session.get("time") or "00:00:00Z"
    try:
        return datetime.fromisoformat(f"{session['date']}T{time_value.replace('Z', '+00:00')}")
    except ValueError:
        return None


def local_time_text(dt: datetime | None, tz: ZoneInfo, include_date: bool = True) -> str:
    if not dt:
        return "TBC"
    local = dt.astimezone(tz)
    if include_date:
        return local.strftime("%a %d %b, %I:%M %p").lstrip("0")
    return local.strftime("%I:%M %p").lstrip("0")


def driver_standings_from(payload: dict) -> list[dict]:
    lists = payload.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
    if not lists:
        return []
    output = []
    for row in lists[0].get("DriverStandings", []):
        d = row.get("Driver", {})
        constructors = row.get("Constructors", [])
        output.append({
            "position": int(row.get("position", 0) or 0),
            "points": float(row.get("points", 0) or 0),
            "wins": int(row.get("wins", 0) or 0),
            "code": d.get("code") or d.get("familyName", "")[:3].upper(),
            "givenName": d.get("givenName", ""),
            "familyName": d.get("familyName", ""),
            "name": f"{d.get('givenName', '')} {d.get('familyName', '')}".strip(),
            "constructor": constructors[0].get("name", "") if constructors else "",
        })
    return output


def constructor_standings_from(payload: dict) -> list[dict]:
    lists = payload.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists", [])
    if not lists:
        return []
    output = []
    for row in lists[0].get("ConstructorStandings", []):
        c = row.get("Constructor", {})
        output.append({
            "position": int(row.get("position", 0) or 0),
            "points": float(row.get("points", 0) or 0),
            "wins": int(row.get("wins", 0) or 0),
            "name": c.get("name", ""),
        })
    return output


def last_race_from(payload: dict) -> dict:
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    if not races:
        return {}
    race = races[0]
    results = []
    for row in race.get("Results", []):
        d = row.get("Driver", {})
        c = row.get("Constructor", {})
        fastest = row.get("FastestLap", {})
        results.append({
            "position": int(row.get("position", 0) or 0),
            "positionText": row.get("positionText", ""),
            "points": float(row.get("points", 0) or 0),
            "code": d.get("code") or d.get("familyName", "")[:3].upper(),
            "name": f"{d.get('givenName', '')} {d.get('familyName', '')}".strip(),
            "familyName": d.get("familyName", ""),
            "constructor": c.get("name", ""),
            "status": row.get("status", ""),
            "time": row.get("Time", {}).get("time", ""),
            "fastestRank": fastest.get("rank"),
            "fastestLap": fastest.get("Time", {}).get("time", ""),
        })

    circuit = race.get("Circuit", {})
    location = circuit.get("Location", {})
    return {
        "season": race.get("season", ""),
        "round": race.get("round", ""),
        "name": race.get("raceName", ""),
        "date": race.get("date", ""),
        "time": race.get("time", ""),
        "circuit": circuit.get("circuitName", ""),
        "location": f"{location.get('locality', '')}, {location.get('country', '')}".strip(", "),
        "results": results,
    }


def calendar_from(payload: dict, tz: ZoneInfo) -> tuple[list[dict], dict]:
    races = payload.get("MRData", {}).get("RaceTable", {}).get("Races", [])
    now = datetime.now(timezone.utc)
    normalized = []
    next_race = None

    for race in races:
        circuit = race.get("Circuit", {})
        location = circuit.get("Location", {})
        race_dt = parse_race_datetime(race)

        sessions = []
        session_map = [
            ("FP1", "FirstPractice"),
            ("FP2", "SecondPractice"),
            ("FP3", "ThirdPractice"),
            ("Sprint Qualifying", "SprintQualifying"),
            ("Sprint", "Sprint"),
            ("Qualifying", "Qualifying"),
        ]
        for label, key in session_map:
            session = race.get(key)
            if session:
                dt = session_datetime(session)
                sessions.append({
                    "name": label,
                    "datetimeUtc": dt.isoformat() if dt else None,
                    "local": local_time_text(dt, tz),
                })

        sessions.append({
            "name": "Race",
            "datetimeUtc": race_dt.isoformat() if race_dt else None,
            "local": local_time_text(race_dt, tz),
        })

        item = {
            "round": int(race.get("round", 0) or 0),
            "name": race.get("raceName", ""),
            "date": race.get("date", ""),
            "datetimeUtc": race_dt.isoformat() if race_dt else None,
            "localDateTime": local_time_text(race_dt, tz),
            "circuit": circuit.get("circuitName", ""),
            "location": f"{location.get('locality', '')}, {location.get('country', '')}".strip(", "),
            "sessions": sessions,
        }
        normalized.append(item)

        if next_race is None and race_dt and race_dt >= now:
            next_race = item

    if next_race is None and normalized:
        next_race = normalized[-1]

    return normalized, next_race or {}


def fmt_points(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def render_standings(data: dict, destination: Path, tz: ZoneInfo):
    drivers = data.get("driverStandings", [])
    constructors = data.get("constructorStandings", [])
    season = data.get("season", "")

    img = Image.new("RGB", (800, 480), WHITE)
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, 800, 58), fill=RED)
    draw.text((22, 11), f"F1 {season} CHAMPIONSHIP", font=font(30, True), fill=WHITE)
    updated = datetime.now(timezone.utc).astimezone(tz).strftime("%d %b %Y %H:%M SGT")
    ufont = font(12)
    draw.text((785 - text_width(draw, updated, ufont), 22), updated, font=ufont, fill=WHITE)

    left_x, right_x = 22, 421
    top = 76
    draw.text((left_x, top), "DRIVERS", font=font(22, True), fill=BLACK)
    draw.text((right_x, top), "CONSTRUCTORS", font=font(22, True), fill=BLACK)

    row_y = top + 34
    row_h = 27
    small = font(15)
    small_bold = font(15, True)

    for i, row in enumerate(drivers[:12]):
        y = row_y + i * row_h
        if i == 0:
            draw.rectangle((left_x - 4, y - 3, 390, y + 21), fill=YELLOW)
        draw.text((left_x, y), f"{row['position']:>2}", font=small_bold, fill=BLACK)
        draw.text((58, y), row["code"], font=small_bold, fill=BLACK)
        name = short_name(row.get("givenName", ""), row.get("familyName", ""))
        draw.text((104, y), name[:20], font=small, fill=BLACK)
        pts = fmt_points(row["points"])
        draw.text((373 - text_width(draw, pts, small_bold), y), pts, font=small_bold, fill=BLACK)
        draw.line((left_x, y + 23, 386, y + 23), fill=LIGHT, width=1)

    for i, row in enumerate(constructors[:11]):
        y = row_y + i * row_h
        if i == 0:
            draw.rectangle((right_x - 4, y - 3, 780, y + 21), fill=YELLOW)
        draw.text((right_x, y), f"{row['position']:>2}", font=small_bold, fill=BLACK)
        draw.text((458, y), row["name"][:24], font=small, fill=BLACK)
        pts = fmt_points(row["points"])
        draw.text((770 - text_width(draw, pts, small_bold), y), pts, font=small_bold, fill=BLACK)
        draw.line((right_x, y + 23, 776, y + 23), fill=LIGHT, width=1)

    draw.line((400, 72, 400, 448), fill=GRAY, width=1)
    footer = "Static GitHub page • Driver top 12 • All constructors • Source: Jolpica F1 API"
    draw.text((22, 456), footer, font=font(11), fill=GRAY)
    img.save(destination, "BMP")


def render_last_race(data: dict, destination: Path, tz: ZoneInfo):
    race = data.get("lastRace", {})
    results = race.get("results", [])

    img = Image.new("RGB", (800, 480), WHITE)
    draw = ImageDraw.Draw(img)

    draw.rectangle((0, 0, 800, 54), fill=BLACK)
    draw.text((16, 8), "LAST RACE", font=font(29, True), fill=WHITE)

    title = race.get("name", "No race result available")
    title_font = font(19, True)
    title_x = 784 - text_width(draw, title, title_font)
    draw.text((max(300, title_x), 16), title, font=title_font, fill=WHITE)

    meta = f"{race.get('circuit', '')} • {race.get('date', '')}"
    draw.text((16, 61), meta, font=font(14), fill=GRAY)

    if not results:
        draw.text(
            (16, 120),
            "No completed race result is available yet.",
            font=font(24, True),
            fill=BLACK,
        )
        img.save(destination, "BMP")
        return

    podium_colors = [YELLOW, LIGHT, (217, 151, 92)]
    gap = 10
    left = 16
    card_w = (800 - (left * 2) - (gap * 2)) // 3
    card_y = 82
    card_h = 84

    for i, row in enumerate(results[:3]):
        x = left + i * (card_w + gap)
        draw.rounded_rectangle(
            (x, card_y, x + card_w, card_y + card_h),
            radius=7,
            fill=podium_colors[i],
            outline=BLACK,
            width=1,
        )
        draw.text((x + 10, card_y + 7), f"P{i+1}", font=font(18, True), fill=BLACK)
        draw.text((x + 10, card_y + 32), row["name"][:24], font=font(17, True), fill=BLACK)
        draw.text((x + 10, card_y + 59), row["constructor"][:27], font=font(12), fill=BLACK)

    draw.text((16, 176), "TOP 10 CLASSIFICATION", font=font(18, True), fill=BLACK)

    header_y = 199
    table_left = 16
    table_right = 784
    draw.rectangle((table_left, header_y, table_right, header_y + 24), fill=BLACK)

    headers = [
        (24, "POS"),
        (72, "DRIVER"),
        (326, "TEAM"),
        (552, "TIME / STATUS"),
        (741, "PTS"),
    ]
    for x, label in headers:
        draw.text((x, header_y + 4), label, font=font(12, True), fill=WHITE)

    row_font = font(14)
    row_bold = font(14, True)
    row_h = 22

    for i, row in enumerate(results[:10]):
        y = header_y + 29 + i * row_h
        if i % 2 == 0:
            draw.rectangle((table_left, y - 2, table_right, y + 19), fill=(247, 247, 247))

        draw.text(
            (26, y),
            str(row.get("positionText") or row.get("position", "")),
            font=row_bold,
            fill=BLACK,
        )
        draw.text((72, y), row["name"][:27], font=row_font, fill=BLACK)
        draw.text((326, y), row["constructor"][:24], font=row_font, fill=BLACK)

        timing = row.get("time") or row.get("status") or ""
        draw.text((552, y), timing[:20], font=row_font, fill=BLACK)

        pts = fmt_points(row.get("points", 0))
        draw.text((776 - text_width(draw, pts, row_bold), y), pts, font=row_bold, fill=BLACK)

    fastest = next((r for r in results if str(r.get("fastestRank")) == "1"), None)

    footer = "Source: Jolpica F1 API"
    if fastest and fastest.get("fastestLap"):
        footer = f"Fastest lap: {fastest['name']} {fastest['fastestLap']} • " + footer

    draw.text((16, 462), footer, font=font(10), fill=GRAY)
    img.save(destination, "BMP")


def esc(value) -> str:
    return html.escape(str(value or ""))



def countdown_text(target: datetime | None, now: datetime) -> str:
    if not target:
        return "TBC"

    seconds = int((target - now).total_seconds())
    if seconds <= 0:
        return "Started / completed"

    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60

    if days:
        return f"in {days}d {hours}h"
    if hours:
        return f"in {hours}h {minutes}m"
    return f"in {minutes}m"


def next_session_from(next_race: dict, generated_at: str | None) -> dict:
    try:
        now = datetime.fromisoformat(generated_at) if generated_at else datetime.now(timezone.utc)
    except ValueError:
        now = datetime.now(timezone.utc)

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    future = []
    for session in next_race.get("sessions", []):
        stamp = session.get("datetimeUtc")
        if not stamp:
            continue
        try:
            dt = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if dt >= now:
            future.append((dt, session))

    if not future:
        return {}

    dt, session = min(future, key=lambda item: item[0])
    return {
        "name": session.get("name", "Next session"),
        "local": session.get("local", "TBC"),
        "countdown": countdown_text(dt, now),
    }


def build_standing_rows(rows: list[dict], driver: bool, limit: int | None = None) -> str:
    subset = rows if limit is None else rows[:limit]
    parts = []
    for row in subset:
        label = row["name"] if driver else row["name"]
        sub = row.get("constructor", "") if driver else ""
        parts.append(
            f"<tr><td class='pos'>{row['position']}</td>"
            f"<td><strong>{esc(label)}</strong>{f'<span>{esc(sub)}</span>' if sub else ''}</td>"
            f"<td class='pts'>{esc(fmt_points(row['points']))}</td></tr>"
        )
    return "".join(parts)


def generate_homeassistant(data: dict, title: str) -> str:
    next_race = data.get("nextRace", {})
    drivers = data.get("driverStandings", [])
    constructors = data.get("constructorStandings", [])
    last = data.get("lastRace", {})
    results = last.get("results", [])
    next_session = next_session_from(next_race, data.get("generatedAt"))

    last_rows = "".join(
        f"<tr><td class='pos'>{esc(r.get('positionText') or r.get('position'))}</td>"
        f"<td><strong>{esc(r.get('name'))}</strong><span>{esc(r.get('constructor'))}</span></td>"
        f"<td>{esc(r.get('time') or r.get('status'))}</td>"
        f"<td class='pts'>{esc(fmt_points(r.get('points',0)))}</td></tr>"
        for r in results[:10]
    )

    driver_leader = drivers[0] if drivers else {}
    driver_p2 = drivers[1] if len(drivers) > 1 else {}
    constructor_top = constructors[:3]

    constructor_snapshot = "".join(
        f"<div class='snapshot-row'><span>{esc(row.get('position'))}. {esc(row.get('name'))}</span>"
        f"<strong>{esc(fmt_points(row.get('points', 0)))} pts</strong></div>"
        for row in constructor_top
    ) or "<div class='muted'>No constructor standings available.</div>"

    next_session_html = (
        f"<div class='next-session-name'>{esc(next_session.get('name'))}</div>"
        f"<div class='next-session-time'>{esc(next_session.get('local'))}</div>"
        f"<div class='countdown'>{esc(next_session.get('countdown'))}</div>"
        if next_session
        else "<div class='next-session-name'>Race weekend complete</div>"
    )

    updated = esc(data.get("generatedLocal", ""))

    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
:root{{--ink:#161616;--muted:#6b6b6b;--paper:#f5f5f5;--card:#fff;--line:#dedede;--soft:#efefef}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--paper);color:var(--ink);font-family:Arial,Helvetica,sans-serif}}
main{{max-width:1180px;margin:auto;padding:18px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:flex-end;margin-bottom:14px}}
h1{{margin:0;font-size:28px}}.muted{{color:var(--muted);font-size:12px}}nav{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}}
button{{border:1px solid #222;background:#fff;padding:9px 13px;font-weight:700;cursor:pointer}}button.active{{background:#222;color:#fff}}
.panel{{display:none}}.panel.active{{display:block}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.card{{background:var(--card);border:1px solid var(--line);padding:14px}}
.card h2{{margin:0 0 10px;font-size:20px}}.heroimg{{width:100%;height:auto;display:block;border:1px solid #aaa;background:#fff}}
.big{{font-size:26px;font-weight:800;margin:2px 0 5px}}.status-grid{{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:14px}}
.status-box{{border:1px solid var(--line);background:#fafafa;padding:12px;min-height:135px}}.status-label{{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.4px}}
.next-session-name{{font-size:20px;font-weight:800;margin-top:7px}}.next-session-time{{font-size:15px;font-weight:700;margin-top:4px}}.countdown{{display:inline-block;margin-top:8px;background:#222;color:#fff;padding:5px 8px;font-weight:700;font-size:13px}}
.snapshot-row{{display:flex;justify-content:space-between;gap:10px;border-bottom:1px solid var(--line);padding:5px 0;font-size:13px}}.snapshot-row:last-child{{border-bottom:0}}
.leader{{font-size:15px;font-weight:800;margin-top:7px}}.leader-sub{{color:var(--muted);font-size:12px;margin-top:3px}}.note{{margin-top:10px;padding:8px 10px;background:var(--soft);font-size:12px;color:#555}}
table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid var(--line);text-align:left;font-size:14px}}th{{background:#222;color:#fff}}
td.pos{{width:44px;font-weight:700}}td.pts{{width:65px;text-align:right;font-weight:700}}td span{{display:block;color:var(--muted);font-size:11px;margin-top:2px}}
.footer{{margin-top:14px;color:var(--muted);font-size:11px}}
@media(max-width:760px){{.grid{{grid-template-columns:1fr}}.status-grid{{grid-template-columns:1fr}}header{{display:block}}}}
</style></head><body><main>
<header><div><h1>{esc(title)}</h1><div class="muted">Static GitHub F1 dashboard for Home Assistant</div></div><div class="muted">Updated {updated}</div></header>
<nav>
<button class="tab active" data-panel="overview">Next race</button>
<button class="tab" data-panel="standings">Standings</button>
<button class="tab" data-panel="last">Last race</button>
<button class="tab" data-panel="teams">Teams & drivers</button>
</nav>

<section id="overview" class="panel active">
<div class="grid">
  <div class="card"><img class="heroimg" src="calendar.bmp" alt="F1 next-race e-paper calendar"></div>
  <div class="card">
    <h2>Next Grand Prix</h2>
    <div class="big">{esc(next_race.get('name','TBC'))}</div>
    <div class="muted">{esc(next_race.get('circuit'))} • {esc(next_race.get('location'))}</div>

    <div class="status-grid">
      <div class="status-box">
        <div class="status-label">Next session</div>
        {next_session_html}
      </div>

      <div class="status-box">
        <div class="status-label">Driver championship</div>
        <div class="leader">{esc(driver_leader.get('position',''))}. {esc(driver_leader.get('name','TBC'))}</div>
        <div class="leader-sub">{esc(fmt_points(driver_leader.get('points',0)))} pts</div>
        <div class="leader-sub" style="margin-top:8px">P2: {esc(driver_p2.get('name','TBC'))} • {esc(fmt_points(driver_p2.get('points',0)))} pts</div>
      </div>

      <div class="status-box">
        <div class="status-label">Constructor championship</div>
        <div style="margin-top:5px">{constructor_snapshot}</div>
      </div>

      <div class="status-box">
        <div class="status-label">Race time</div>
        <div class="next-session-name">{esc(next_race.get('localDateTime','TBC'))}</div>
        <div class="note">Current weather and the previous-year circuit reference are shown in the calendar image.</div>
      </div>
    </div>
  </div>
</div>
</section>

<section id="standings" class="panel"><div class="grid">
<div class="card"><h2>Driver championship</h2><table><thead><tr><th>#</th><th>Driver</th><th>Pts</th></tr></thead><tbody>{build_standing_rows(drivers, True, 15)}</tbody></table></div>
<div class="card"><h2>Constructor championship</h2><table><thead><tr><th>#</th><th>Constructor</th><th>Pts</th></tr></thead><tbody>{build_standing_rows(constructors, False)}</tbody></table></div>
</div></section>

<section id="last" class="panel"><div class="grid">
<div class="card"><img class="heroimg" src="last-race.bmp" alt="Last race summary"></div>
<div class="card"><h2>{esc(last.get('name','Last race'))}</h2><div class="muted">{esc(last.get('circuit'))} • {esc(last.get('date'))}</div>
<table><thead><tr><th>#</th><th>Driver</th><th>Time / status</th><th>Pts</th></tr></thead><tbody>{last_rows}</tbody></table></div>
</div></section>

<section id="teams" class="panel"><div class="card"><h2>Teams & drivers</h2><img class="heroimg" src="teams.bmp" alt="F1 teams and drivers"></div></section>

<div class="footer">Calendar/teams imagery: InkyCloud-F1 • Standings/results: Jolpica F1 API • All session times shown in {esc(data.get('timezone'))}</div>
</main>
<script>
document.querySelectorAll('.tab').forEach(b=>b.addEventListener('click',()=>{{
  document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x=>x.classList.remove('active'));
  b.classList.add('active');
  document.getElementById(b.dataset.panel).classList.add('active');
}}));
</script>
</body></html>'''


def generate_index(data: dict) -> str:
    updated = esc(data.get("generatedLocal", ""))
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>F1 E1002 Static Pages</title>
<style>*{{box-sizing:border-box}}body{{font-family:Arial,Helvetica,sans-serif;margin:0;background:#f3f3f3;color:#171717}}main{{max-width:1100px;margin:auto;padding:20px}}h1{{margin-bottom:4px}}.muted{{color:#666;font-size:12px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}}.card{{background:#fff;border:1px solid #ccc;padding:12px}}img{{width:100%;display:block;border:1px solid #999}}a{{color:#111;font-weight:700}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}</style></head><body><main>
<h1>F1 E1002 Static Pages</h1><div class="muted">Updated {updated}</div>
<p>This repository pre-builds F1 content so the E1002 downloads static BMP files instead of waiting for upstream rendering and data calls.</p>
<div class="grid">
<div class="card"><h2>Next race</h2><a href="calendar.bmp"><img src="calendar.bmp"></a></div>
<div class="card"><h2>Teams & drivers</h2><a href="teams.bmp"><img src="teams.bmp"></a></div>
<div class="card"><h2>Championship standings</h2><a href="standings.bmp"><img src="standings.bmp"></a></div>
<div class="card"><h2>Last race</h2><a href="last-race.bmp"><img src="last-race.bmp"></a></div>
</div><p><a href="homeassistant.html">Open the Home Assistant F1 dashboard</a></p>
</main></body></html>'''


def main():
    config = load_json(CONFIG_FILE, {})
    language = config.get("language", "en")
    timezone_name = config.get("timezone", "Asia/Singapore")
    display = config.get("display", "spectra6")
    weather = bool(config.get("weather", True))
    weather_type = config.get("weather_type", "current")
    title = config.get("homeassistant_title", "F1 Hub")
    tz = ZoneInfo(timezone_name)
    season = datetime.now(tz).year

    etags = load_json(ETAG_FILE, {})
    source_state = {}

    source_state["calendar"] = fetch_bmp_with_etag(
        f"{INKYCLOUD_BASE}/calendar.bmp",
        {
            "lang": language,
            "tz": timezone_name,
            "display": display,
            "weather": "true" if weather else "false",
            "weather_type": weather_type,
        },
        OUTPUT_CALENDAR,
        "calendar",
        etags,
    )

    source_state["teams"] = fetch_bmp_with_etag(
        f"{INKYCLOUD_BASE}/teams.bmp",
        {"lang": language, "year": season, "display": display},
        OUTPUT_TEAMS,
        "teams",
        etags,
    )
    optimize_teams_bmp(OUTPUT_TEAMS)

    calendar_payload = fetch_json(f"{JOLPICA_BASE}/{season}.json")
    driver_payload = fetch_json(f"{JOLPICA_BASE}/{season}/driverstandings.json")
    constructor_payload = fetch_json(f"{JOLPICA_BASE}/{season}/constructorstandings.json")
    last_payload = fetch_json(f"{JOLPICA_BASE}/{season}/last/results.json")

    calendar, next_race = calendar_from(calendar_payload, tz)
    driver_standings = driver_standings_from(driver_payload)
    constructor_standings = constructor_standings_from(constructor_payload)
    last_race = last_race_from(last_payload)

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(tz)
    data = {
        "generatedAt": now_utc.isoformat(),
        "generatedLocal": now_local.strftime("%a %d %b %Y, %I:%M %p").lstrip("0"),
        "season": season,
        "timezone": timezone_name,
        "config": {
            "language": language,
            "display": display,
            "weather": weather,
            "weatherType": weather_type,
        },
        "nextRace": next_race,
        "calendar": calendar,
        "driverStandings": driver_standings,
        "constructorStandings": constructor_standings,
        "lastRace": last_race,
        "sources": {
            "calendarImage": f"{INKYCLOUD_BASE}/calendar.bmp",
            "teamsImage": f"{INKYCLOUD_BASE}/teams.bmp",
            "raceData": "Jolpica F1 API",
        },
    }

    render_standings(data, OUTPUT_STANDINGS, tz)
    render_last_race(data, OUTPUT_LAST_RACE, tz)
    save_json(OUTPUT_DATA, data)
    OUTPUT_HA.write_text(generate_homeassistant(data, title), encoding="utf-8")
    OUTPUT_INDEX.write_text(generate_index(data), encoding="utf-8")
    save_json(ETAG_FILE, etags)
    save_json(STATUS_FILE, {
        "status": "ok",
        "generatedAt": now_utc.isoformat(),
        "generatedLocal": data["generatedLocal"],
        "season": season,
        "sourceState": source_state,
        "outputs": [
            "calendar.bmp",
            "teams.bmp",
            "standings.bmp",
            "last-race.bmp",
            "f1-data.json",
            "homeassistant.html",
            "index.html",
        ],
    })

    print(f"Generated F1 static pages for {season} at {data['generatedLocal']} ({timezone_name})")
    print(f"InkyCloud calendar: {source_state['calendar']}; teams: {source_state['teams']}")
    print(f"Drivers: {len(driver_standings)}; constructors: {len(constructor_standings)}; last-race results: {len(last_race.get('results', []))}")


if __name__ == "__main__":
    main()
