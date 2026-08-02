"""Milkbook server tests. Run: python3 test_milkbook.py

Covers the parts where a bug costs someone real money or real data:
the billing arithmetic, the merge that decides whose edit survives,
the .ics that phone calendars have to accept, and the input handling.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

os.environ["MILKBOOK_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app as M  # noqa: E402

PASS = FAIL = 0


def ok(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}" + (f"  {extra}" if extra else ""))
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {extra}")


def book(days=None, **settings) -> dict:
    base = {"t": 1, "vendor": "Test Dairy", "qtyMl": 1000, "rateMinor": 6000,
            "currency": "INR", "startDate": "2026-01-01", "skipWeekly": []}
    base.update(settings)
    return {"settings": base, "days": days or {}}


def call(method: str, path: str, body: dict | None = None, origin: str | None = None):
    raw = json.dumps(body).encode() if body is not None else b""
    env = {
        "REQUEST_METHOD": method, "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(raw)), "wsgi.input": io.BytesIO(raw),
        "REMOTE_ADDR": "203.0.113.%d" % (hash(path) % 200),
    }
    if origin:
        env["HTTP_ORIGIN"] = origin
    captured = {}

    def start(status, headers):
        captured["status"] = int(status.split()[0])
        captured["headers"] = dict(headers)

    chunks = M.app(env, start)
    return captured["status"], captured.get("headers", {}), b"".join(chunks)


# --------------------------------------------------------------------- billing
print("\nMILKBOOK — billing arithmetic")
{
    ok("a full month bills exactly",
       M.month_summary(book(), 2026, 6, "2026-06-30")["amountMinor"] == 180000,
       f'{M.month_summary(book(), 2026, 6, "2026-06-30")["amountMinor"]} minor for 30 x 1L at 60.00')
}
s = M.month_summary(book(days={
    "2026-06-05": {"s": "skip", "t": 2},
    "2026-06-06": {"s": "skip", "t": 2},
    "2026-06-10": {"s": "away", "t": 2},
}), 2026, 6, "2026-06-30")
ok("skipped and away days are not billed",
   s["delivered"] == 27 and s["skipped"] == 2 and s["away"] == 1 and s["amountMinor"] == 162000,
   f'{s["delivered"]}d, {s["amountMinor"]} minor')

s = M.month_summary(book(days={"2026-06-03": {"s": "yes", "q": 2000, "t": 2}}), 2026, 6, "2026-06-30")
ok("a one-off larger quantity is billed", s["totalMl"] == 31000 and s["amountMinor"] == 186000,
   f'{s["totalMl"]}ml, {s["amountMinor"]} minor')

s = M.month_summary(book(qtyMl=500), 2026, 6, "2026-06-30")
ok("half-litre default bills correctly", s["amountMinor"] == 90000, f'{s["amountMinor"]}')

# rounding once at the end, the way a vendor totals litres then multiplies,
# differs from rounding each day and summing - and only one of them is the bill
odd = M.month_summary(book(qtyMl=333, rateMinor=3333), 2026, 6, "2026-06-30")
per_day_rounded = sum(round(333 * 3333 / 1000) for _ in range(30))
ok("the total is rounded once, not once per day",
   odd["amountMinor"] == 33297 and per_day_rounded == 33300,
   f'exact {odd["amountMinor"]} vs per-day rounding {per_day_rounded}')

s = M.month_summary(book(), 2026, 6, "2026-06-10")
ok("future days are never billed", s["delivered"] == 10 and s["amountMinor"] == 60000,
   f'{s["delivered"]} days to the 10th')

s = M.month_summary(book(startDate="2026-06-15"), 2026, 6, "2026-06-30")
ok("days before tracking started are ignored", s["delivered"] == 16, f'{s["delivered"]}')

# June 2026: the 7th, 14th, 21st, 28th are Sundays
s = M.month_summary(book(skipWeekly=[0]), 2026, 6, "2026-06-30")
ok("the vendor's weekly off is not counted or billed",
   s["off"] == 4 and s["delivered"] == 26 and s["amountMinor"] == 156000,
   f'{s["off"]} off, {s["delivered"]} delivered')

s = M.month_summary(book(skipWeekly=[0], days={"2026-06-07": {"s": "yes", "t": 5}}), 2026, 6, "2026-06-30")
ok("an explicit delivery overrides the weekly off", s["delivered"] == 27, f'{s["delivered"]}')

s = M.month_summary(book(), 2026, 2, "2026-02-28")
ok("February is handled", s["delivered"] == 28, f'{s["delivered"]}')
s = M.month_summary(book(startDate="2024-01-01"), 2024, 2, "2024-02-29")
ok("a leap year is handled", s["delivered"] == 29, f'{s["delivered"]}')
s = M.month_summary(book(), 2026, 12, "2026-12-31")
ok("December does not roll into the next year", s["delivered"] == 31, f'{s["delivered"]}')
ok("an empty book bills nothing", M.month_summary(book(), 2026, 6, "2025-01-01")["amountMinor"] == 0)


# ----------------------------------------------------------------------- merge
print("\nMILKBOOK — merge (whose edit survives?)")
a = book(days={"2026-06-01": {"s": "skip", "t": 100}})
b = book(days={"2026-06-02": {"s": "away", "t": 100}})
m = M.merge(a, b)
ok("edits to different days both survive",
   m["days"]["2026-06-01"]["s"] == "skip" and m["days"]["2026-06-02"]["s"] == "away")

a = book(days={"2026-06-01": {"s": "skip", "t": 100}})
b = book(days={"2026-06-01": {"s": "away", "t": 200}})
ok("the newer edit to the same day wins", M.merge(a, b)["days"]["2026-06-01"]["s"] == "away")
ok("merge order does not change the result",
   M.merge(a, b)["days"]["2026-06-01"] == M.merge(b, a)["days"]["2026-06-01"])

a2 = book(days={"2026-06-01": {"s": "skip", "t": 100}})
ok("merging is idempotent", M.merge(M.merge(a2, b), b) == M.merge(a2, b))

old = book(t=100, rateMinor=5000)
new = book(t=200, rateMinor=7000)
ok("the newer settings win as a unit", M.merge(old, new)["settings"]["rateMinor"] == 7000)
ok("older settings do not clobber newer", M.merge(new, old)["settings"]["rateMinor"] == 7000)
ok("a device with no history does not wipe the server",
   len(M.merge({"settings": {}, "days": {}}, a2)["days"]) == 1)


# -------------------------------------------------------------------- sanitise
print("\nMILKBOOK — input handling")
ok("an unknown state is dropped",
   "2026-06-01" not in M.sanitise({"days": {"2026-06-01": {"s": "hacked", "t": 1}}})["days"])
ok("a malformed date key is dropped",
   M.sanitise({"days": {"not-a-date": {"s": "yes", "t": 1}}})["days"] == {})
ok("the vendor name is length-capped",
   len(M.sanitise({"settings": {"vendor": "x" * 500}})["settings"]["vendor"]) == 60)
ok("an absurd rate is clamped",
   M.sanitise({"settings": {"rateMinor": 10 ** 12}})["settings"]["rateMinor"] == 10_000_000)
ok("a negative quantity cannot become a credit",
   M.sanitise({"days": {"2026-06-01": {"s": "yes", "q": -5000, "t": 1}}})["days"]["2026-06-01"]["q"] == 0)
try:
    M.sanitise({"days": {f"2026-06-{i:02d}": {"s": "yes", "t": 1} for i in range(1, 3)} |
                {str(i): {"s": "yes", "t": 1} for i in range(M.MAX_DAYS + 10)}})
    ok("an oversized book is refused", False)
except ValueError:
    ok("an oversized book is refused", True)
try:
    M.sanitise("not a dict")
    ok("a non-object body is refused", False)
except ValueError:
    ok("a non-object body is refused", True)


# ------------------------------------------------------------------------ http
print("\nMILKBOOK — http surface")
CODE = "K4M2-P8QX-7T3B"
status, _, _ = call("GET", "/healthz")
ok("health check responds", status == 200, str(status))

status, _, body = call("POST", f"/api/store/{CODE}", {"settings": {"t": 5, "rateMinor": 6000}, "days": {"2026-06-01": {"s": "skip", "t": 5}}})
ok("a book can be pushed", status == 200 and json.loads(body)["days"]["2026-06-01"]["s"] == "skip", str(status))

status, _, body = call("GET", f"/api/store/{CODE}")
ok("a book can be pulled back", status == 200 and json.loads(body)["days"]["2026-06-01"]["s"] == "skip")

status, _, body = call("POST", f"/api/store/{CODE}", {"settings": {"t": 1}, "days": {"2026-06-02": {"s": "away", "t": 6}}})
merged = json.loads(body)
ok("a second device merges rather than overwrites",
   set(merged["days"]) == {"2026-06-01", "2026-06-02"}, str(sorted(merged["days"])))

for bad in ["short", "k4m2-p8qx-7t3b-extra", "AAAA-BBBB-CCC", "../../etc/passwd", "A'--"]:
    status, _, _ = call("GET", f"/api/store/{bad}")
    if status != 400:
        break
ok("malformed sync codes are rejected", status == 400, f"last status {status}")

status, _, _ = call("GET", "/api/store/K4M2-P8QX-7T3B'; DROP TABLE books;--")
ok("a SQL injection attempt is rejected by format check", status == 400, str(status))
status, _, _ = call("GET", f"/api/store/{CODE}")
ok("the table still exists afterwards", status == 200, str(status))

env = {"REQUEST_METHOD": "POST", "PATH_INFO": f"/api/store/{CODE}",
       "CONTENT_LENGTH": str(M.MAX_BODY + 1), "wsgi.input": io.BytesIO(b"{}"),
       "REMOTE_ADDR": "203.0.113.250"}
cap = {}
M.app(env, lambda s, h: cap.update(status=int(s.split()[0])))
ok("an oversized body is refused before reading", cap["status"] == 413, str(cap["status"]))

status, _, _ = call("POST", f"/api/store/{CODE}", None)
ok("an empty body is refused", status == 413, str(status))

_, headers, _ = call("GET", "/healthz", origin="https://rahulatrkm.github.io")
ok("the app origin is allowed by CORS",
   headers.get("Access-Control-Allow-Origin") == "https://rahulatrkm.github.io")
_, headers, _ = call("GET", "/healthz", origin="https://evil.example")
ok("an unknown origin gets no CORS grant", "Access-Control-Allow-Origin" not in headers)
ok("responses are not sniffable", headers.get("X-Content-Type-Options") == "nosniff")


# ------------------------------------------------------------------------- ics
print("\nMILKBOOK — calendar feed")
cal_book = book(days={
    "2026-06-10": {"s": "away", "t": 1}, "2026-06-11": {"s": "away", "t": 1},
    "2026-06-12": {"s": "away", "t": 1}, "2026-06-20": {"s": "skip", "t": 1},
})
ics = M.build_ics(cal_book, now=datetime(2026, 6, 30, 9, 0, tzinfo=timezone.utc), months=1)

ok("every line ends CRLF", ics.endswith("\r\n") and "\n" not in ics.replace("\r\n", ""))
ok("the calendar is balanced",
   ics.count("BEGIN:VCALENDAR") == 1 and ics.count("END:VCALENDAR") == 1 and
   ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT"))
ics_lines = ics.split("\r\n")
longest = max(len(line.encode()) for line in ics_lines)
ok("no unfolded line exceeds 75 octets", longest <= 75, f"longest {longest}")
ok("a three-day away run becomes one event, not three",
   ics.count("SUMMARY:No milk \u2014 away") == 1)
ok("the away run ends the day after it finishes (DTEND is exclusive)",
   "DTSTART;VALUE=DATE:20260610" in ics and "DTEND;VALUE=DATE:20260613" in ics)
ok("a bill event is produced with a reminder",
   "Milk bill" in ics and "BEGIN:VALARM" in ics)
expected_total = M._esc(M._money(M.month_summary(cal_book, 2026, 6, "2026-06-30")["amountMinor"], "INR"))
ok("the bill total in the calendar matches the ledger",
   expected_total in ics.replace("\r\n ", ""), expected_total)
ok("every event carries a stable unique id", ics.count("UID:") == ics.count("BEGIN:VEVENT"))
ok("an empty book still yields a valid calendar",
   M.build_ics({"settings": {}, "days": {}}).startswith("BEGIN:VCALENDAR"))

status, headers, body = call("GET", f"/cal/{CODE}.ics")
ok("the feed is served with the calendar content type",
   status == 200 and headers["Content-Type"].startswith("text/calendar"), str(status))
ok("the feed body is a calendar", body.startswith(b"BEGIN:VCALENDAR"))
status, _, _ = call("GET", "/cal/nope.ics")
ok("a malformed code cannot fetch a feed", status == 400, str(status))


# ------------------------------------------------- fixture for the client tests
fixture = {
    "book": book(days={
        "2026-06-05": {"s": "skip", "t": 2},
        "2026-06-10": {"s": "away", "t": 2},
        "2026-06-11": {"s": "away", "t": 2},
        "2026-06-15": {"s": "yes", "q": 2500, "t": 2},
    }, skipWeekly=[0], rateMinor=5750, qtyMl=750),
    "cases": [],
}
for year, month, today in [(2026, 6, "2026-06-30"), (2026, 6, "2026-06-12"), (2026, 7, "2026-07-31")]:
    fixture["cases"].append({
        "year": year, "month": month, "today": today,
        "expected": M.month_summary(fixture["book"], year, month, today),
    })
with open("/tmp/milkbook_fixture.json", "w") as fh:
    json.dump(fixture, fh)
print(f"\n  wrote /tmp/milkbook_fixture.json for the client cross-check")

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
