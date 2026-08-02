"""Milkbook sync + calendar feed.

Design note: the phone is the source of truth. Every client keeps the whole
book in local storage and works with no network at all. This service is a
mirror that lets a household's devices agree, plus an .ics endpoint so a
phone calendar can subscribe. If this service loses its database the next
client to open re-uploads everything, so nothing is lost.

Merges are per-day last-write-wins, not whole-blob, so two people editing
different days on different phones both keep their edits.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus

DB_PATH = os.environ.get("MILKBOOK_DB", os.path.join(os.path.dirname(__file__), "milkbook.db"))

CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STATES = {"yes", "skip", "away"}

MAX_BODY = 512 * 1024
MAX_DAYS = 20_000
RATE_LIMIT = 90          # requests
RATE_WINDOW = 60         # seconds

ALLOWED_ORIGINS = {
    "https://rahulatrkm.github.io",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

CURRENCY_SYMBOLS = {
    "INR": "\u20b9", "USD": "$", "EUR": "\u20ac", "GBP": "\u00a3", "PKR": "\u20a8",
    "BDT": "\u09f3", "LKR": "Rs", "NPR": "Rs", "AED": "\u062f.\u0625", "AUD": "$", "CAD": "$",
}


# --------------------------------------------------------------------------- db
def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS books("
        " code TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER NOT NULL)"
    )
    return conn


def read_book(code: str) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT data FROM books WHERE code = ?", (code,)).fetchone()
    if not row:
        return {"settings": {}, "days": {}}
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {"settings": {}, "days": {}}


def write_book(code: str, book: dict) -> None:
    payload = json.dumps(book, separators=(",", ":"))
    with _db() as conn:
        conn.execute(
            "INSERT INTO books(code, data, updated) VALUES(?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET data=excluded.data, updated=excluded.updated",
            (code, payload, int(time.time())),
        )


# ---------------------------------------------------------------------- domain
def sanitise(book: dict) -> dict:
    """Accept only what we understand. Anything else is dropped rather than stored."""
    if not isinstance(book, dict):
        raise ValueError("body must be an object")

    raw_settings = book.get("settings")
    settings: dict = {}
    if isinstance(raw_settings, dict):
        settings = {
            "t": int(raw_settings.get("t") or 0),
            "vendor": str(raw_settings.get("vendor") or "")[:60],
            "qtyMl": max(0, min(int(raw_settings.get("qtyMl") or 1000), 100_000)),
            "rateMinor": max(0, min(int(raw_settings.get("rateMinor") or 0), 10_000_000)),
            "currency": str(raw_settings.get("currency") or "INR")[:4],
            "startDate": str(raw_settings.get("startDate") or "")[:10],
            "skipWeekly": [d for d in (raw_settings.get("skipWeekly") or []) if isinstance(d, int) and 0 <= d <= 6],
        }
        if not DAY_RE.match(settings["startDate"]):
            settings["startDate"] = ""

    raw_days = book.get("days")
    days: dict = {}
    if isinstance(raw_days, dict):
        if len(raw_days) > MAX_DAYS:
            raise ValueError("too many days")
        for key, entry in raw_days.items():
            if not DAY_RE.match(key) or not isinstance(entry, dict):
                continue
            state = entry.get("s")
            if state not in STATES:
                continue
            clean = {"s": state, "t": int(entry.get("t") or 0)}
            qty = entry.get("q")
            if isinstance(qty, (int, float)):
                clean["q"] = max(0, min(int(qty), 100_000))
            days[key] = clean

    return {"settings": settings, "days": days}


def merge(a: dict, b: dict) -> dict:
    """Per-day last-write-wins; settings win as a unit on their own timestamp."""
    a_set, b_set = a.get("settings") or {}, b.get("settings") or {}
    settings = b_set if (b_set.get("t") or 0) > (a_set.get("t") or 0) else a_set

    a_days, b_days = a.get("days") or {}, b.get("days") or {}
    days = {}
    for key in set(a_days) | set(b_days):
        x, y = a_days.get(key), b_days.get(key)
        if x is None:
            days[key] = y
        elif y is None:
            days[key] = x
        else:
            days[key] = y if (y.get("t") or 0) > (x.get("t") or 0) else x
    return {"settings": settings, "days": days}


def day_state(book: dict, key: str) -> tuple[str, int]:
    settings = book.get("settings") or {}
    entry = (book.get("days") or {}).get(key)
    dow = (datetime.strptime(key, "%Y-%m-%d").weekday() + 1) % 7  # python: Mon=0 -> js: Sun=0
    if entry and entry.get("s"):
        state = entry["s"]
    else:
        state = "off" if dow in (settings.get("skipWeekly") or []) else "yes"
    if state != "yes":
        return state, 0
    qty = entry.get("q") if entry and entry.get("q") is not None else settings.get("qtyMl", 1000)
    return state, int(qty)


def month_summary(book: dict, year: int, month: int, today_key: str) -> dict:
    settings = book.get("settings") or {}
    start = settings.get("startDate") or "0000-01-01"
    last = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).day

    total_ml = delivered = skipped = away = off = 0
    for d in range(1, last + 1):
        key = f"{year:04d}-{month:02d}-{d:02d}"
        if key < start or key > today_key:
            continue
        state, qty = day_state(book, key)
        if state == "yes":
            delivered += 1
            total_ml += qty
        elif state == "skip":
            skipped += 1
        elif state == "away":
            away += 1
        else:
            off += 1

    rate = int(settings.get("rateMinor") or 0)
    amount = round(total_ml * rate / 1000)
    return {"totalMl": total_ml, "delivered": delivered, "skipped": skipped,
            "away": away, "off": off, "amountMinor": amount}


# ------------------------------------------------------------------------- ics
def _esc(text: str) -> str:
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """RFC 5545 folds at 75 OCTETS, not characters, and a multi-octet character
    may not be split. Currency symbols and dashes make these differ."""
    if len(line.encode()) <= 75:
        return line
    segments, current, used, budget = [], "", 0, 75
    for ch in line:
        size = len(ch.encode())
        if used + size > budget:
            segments.append(current)
            current, used, budget = ch, size, 74  # continuations carry a leading space
        else:
            current += ch
            used += size
    segments.append(current)
    return "\r\n ".join(segments)


def _money(minor: int, currency: str) -> str:
    return f"{CURRENCY_SYMBOLS.get(currency, '')}{minor / 100:,.2f}"


def _runs(book: dict, start: date, end: date, wanted: str):
    runs, cur = [], None
    day = start
    while day <= end:
        key = day.isoformat()
        state, _ = day_state(book, key)
        if state == wanted:
            if cur:
                cur[1] = day
            else:
                cur = [day, day]
        elif cur:
            runs.append(cur)
            cur = None
        day += timedelta(days=1)
    if cur:
        runs.append(cur)
    return runs


def build_ics(book: dict, now: datetime | None = None, months: int = 6) -> str:
    now = now or datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    today_key = now.date().isoformat()
    settings = book.get("settings") or {}
    currency = settings.get("currency") or "INR"
    vendor = settings.get("vendor") or ""
    name = f"Milk \u2014 {vendor}" if vendor else "Milk"

    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Milkbook//Milk delivery//EN",
             "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
             f"X-WR-CALNAME:{_esc(name)}", "X-WR-TIMEZONE:UTC",
             "REFRESH-INTERVAL;VALUE=DURATION:PT6H", "X-PUBLISHED-TTL:PT6H"]

    def event(uid, start: date, end_exclusive: date, summary, desc=None, alarm=False):
        lines.extend([
            "BEGIN:VEVENT", f"UID:{uid}@milkbook", f"DTSTAMP:{stamp}",
            f"DTSTART;VALUE=DATE:{start.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{end_exclusive.strftime('%Y%m%d')}",
            f"SUMMARY:{_esc(summary)}", "TRANSP:TRANSPARENT",
        ])
        if desc:
            lines.append(f"DESCRIPTION:{_esc(desc)}")
        if alarm:
            lines.extend(["BEGIN:VALARM", "ACTION:DISPLAY",
                          f"DESCRIPTION:{_esc(summary)}", "TRIGGER:-PT9H", "END:VALARM"])
        lines.append("END:VEVENT")

    y, m = now.year, now.month
    first_y, first_m = y, m - (months - 1)
    while first_m <= 0:
        first_m += 12
        first_y -= 1
    window_start = date(first_y, first_m, 1)
    start_setting = settings.get("startDate")
    if start_setting and DAY_RE.match(start_setting):
        window_start = max(window_start, date.fromisoformat(start_setting))
    last_day = (date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1))

    if window_start <= last_day:
        for run_start, run_end in _runs(book, window_start, last_day, "away"):
            event(f"away-{run_start.isoformat()}", run_start, run_end + timedelta(days=1),
                  "No milk \u2014 away", "Delivery paused while away.")

    for i in range(months):
        my, mm = y, m - i
        while mm <= 0:
            mm += 12
            my -= 1
        summary = month_summary(book, my, mm, today_key)
        if summary["delivered"] == 0:
            continue
        month_end = (date(my + (mm == 12), (mm % 12) + 1, 1) - timedelta(days=1))
        litres = summary["totalMl"] / 1000
        event(
            f"bill-{my}-{mm:02d}", month_end, month_end + timedelta(days=1),
            f"Milk bill {_money(summary['amountMinor'], currency)} \u00b7 {litres:g} L",
            f"{MONTHS[mm - 1]} {my}\n{summary['delivered']} deliveries \u00b7 {litres:g} L "
            f"at {_money(int(settings.get('rateMinor') or 0), currency)}/L\n"
            f"Total {_money(summary['amountMinor'], currency)}",
            alarm=True,
        )

    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


# ------------------------------------------------------------------------ wsgi
_hits: dict[str, deque] = defaultdict(deque)


def _rate_ok(ip: str) -> bool:
    now = time.time()
    bucket = _hits[ip]
    while bucket and now - bucket[0] > RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT:
        return False
    bucket.append(now)
    if len(_hits) > 8000:
        for key in [k for k, v in _hits.items() if not v][:4000]:
            _hits.pop(key, None)
    return True


def _cors(origin: str | None) -> list[tuple[str, str]]:
    headers = [("Vary", "Origin")]
    if origin in ALLOWED_ORIGINS:
        headers += [
            ("Access-Control-Allow-Origin", origin),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Headers", "Content-Type"),
            ("Access-Control-Max-Age", "86400"),
        ]
    return headers


def _reply(start_response, status: HTTPStatus, body: bytes,
           content_type: str, extra: list[tuple[str, str]] | None = None):
    headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("Referrer-Policy", "no-referrer"),
        ("Cache-Control", "no-store"),
    ] + (extra or [])
    start_response(f"{status.value} {status.phrase}", headers)
    return [body]


def _json(start_response, status, payload, extra=None):
    return _reply(start_response, status, json.dumps(payload).encode(),
                  "application/json; charset=utf-8", extra)


def app(environ, start_response):
    path = environ.get("PATH_INFO", "/")
    method = environ.get("REQUEST_METHOD", "GET").upper()
    origin = environ.get("HTTP_ORIGIN")
    cors = _cors(origin)
    forwarded = environ.get("HTTP_X_FORWARDED_FOR", "")
    ip = forwarded.split(",")[0].strip() or environ.get("REMOTE_ADDR", "?")

    if method == "OPTIONS":
        return _reply(start_response, HTTPStatus.NO_CONTENT, b"", "text/plain", cors)

    if path == "/healthz":
        return _json(start_response, HTTPStatus.OK, {"ok": True}, cors)

    if path == "/":
        return _json(start_response, HTTPStatus.OK, {
            "service": "milkbook",
            "app": "https://rahulatrkm.github.io/milkbook/",
            "note": "Sync mirror and calendar feed. Your phone holds the real copy.",
        }, cors)

    if not _rate_ok(ip):
        return _json(start_response, HTTPStatus.TOO_MANY_REQUESTS, {"error": "slow down"}, cors)

    # /cal/<code>.ics
    if path.startswith("/cal/") and path.endswith(".ics"):
        code = path[len("/cal/"):-len(".ics")].upper()
        if not CODE_RE.match(code):
            return _json(start_response, HTTPStatus.BAD_REQUEST, {"error": "bad code"}, cors)
        ics = build_ics(read_book(code)).encode()
        return _reply(start_response, HTTPStatus.OK, ics,
                      "text/calendar; charset=utf-8",
                      cors + [("Content-Disposition", 'inline; filename="milkbook.ics"')])

    # /api/store/<code>
    if path.startswith("/api/store/"):
        code = path[len("/api/store/"):].upper()
        if not CODE_RE.match(code):
            return _json(start_response, HTTPStatus.BAD_REQUEST, {"error": "bad code"}, cors)

        if method == "GET":
            return _json(start_response, HTTPStatus.OK, read_book(code), cors)

        if method == "POST":
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return _json(start_response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                             {"error": "body too large"}, cors)
            try:
                incoming = sanitise(json.loads(environ["wsgi.input"].read(length)))
            except (ValueError, KeyError, TypeError):
                return _json(start_response, HTTPStatus.BAD_REQUEST, {"error": "bad body"}, cors)

            merged = merge(read_book(code), incoming)
            write_book(code, merged)
            return _json(start_response, HTTPStatus.OK, merged, cors)

        return _json(start_response, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "no"}, cors)

    return _json(start_response, HTTPStatus.NOT_FOUND, {"error": "not found"}, cors)
