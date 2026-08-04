"""Milkbook server tests. Run: python3 test_milkbook.py

Covers the parts where a bug costs someone real money or real data:
the billing arithmetic, the merge that decides whose edit survives,
the .ics that phone calendars have to accept, and the input handling.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
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


# ------------------------------------------------------------------------ locks
print("\nMILKBOOK — month locks travel between phones")
ok("a lock survives the round trip",
   M.sanitise({"locks": {"2026-06": {"on": True, "t": 5}}})["locks"]["2026-06"]["on"] is True)
ok("a malformed month key is dropped",
   M.sanitise({"locks": {"2026-06-01": {"on": True, "t": 5}}})["locks"] == {})
ok("a lock flag is coerced to a boolean",
   M.sanitise({"locks": {"2026-06": {"on": "yes please", "t": 5}}})["locks"]["2026-06"]["on"] is True)
ok("a book with no locks still answers with a locks map",
   M.sanitise({"days": {}})["locks"] == {})

locked = {"settings": {}, "days": {}, "locks": {"2026-06": {"on": True, "t": 100}}}
opened = {"settings": {}, "days": {}, "locks": {"2026-06": {"on": False, "t": 200}}}
ok("a lock reaches a phone that had none",
   M.merge({"settings": {}, "days": {}}, locked)["locks"]["2026-06"]["on"] is True)
ok("the later unlock wins", M.merge(locked, opened)["locks"]["2026-06"]["on"] is False)
ok("order does not change the outcome",
   M.merge(locked, opened)["locks"] == M.merge(opened, locked)["locks"])
ok("a peer with no locks does not clear ours",
   M.merge(locked, {"settings": {}, "days": {}})["locks"]["2026-06"]["on"] is True)
ok("merging locks is idempotent",
   M.merge(M.merge(locked, opened), opened) == M.merge(locked, opened))
try:
    M.sanitise({"locks": {f"20{i:02d}-01": {"on": True, "t": 1} for i in range(M.MAX_MONTHS + 10)}})
    ok("an absurd number of months is refused", False)
except ValueError:
    ok("an absurd number of months is refused", True)

ok("the automatic-close setting is kept",
   M.sanitise({"settings": {"lockAfterDays": 7}})["settings"]["lockAfterDays"] == 7)
ok("an absurd automatic-close window is clamped",
   M.sanitise({"settings": {"lockAfterDays": 99999}})["settings"]["lockAfterDays"] == 366)


# ------------------------------------------------------- what a blank day means
print("\nMILKBOOK — what an untouched day means")
ok("delivered is still the default",
   M.day_state({"settings": {}, "days": {}}, "2026-06-03")[0] == "yes")
ok("it can mean not delivered",
   M.day_state({"settings": {"defaultState": "skip"}, "days": {}}, "2026-06-03")[0] == "skip")
ok("it can mean nothing recorded",
   M.day_state({"settings": {"defaultState": "none"}, "days": {}}, "2026-06-03")[0] == "none")
ok("a nonsense default falls back to delivered",
   M.sanitise({"settings": {"defaultState": "whatever"}})["settings"]["defaultState"] == "yes")
ok("an unrecorded day carries no quantity",
   M.day_state({"settings": {"defaultState": "none"}, "days": {}}, "2026-06-03")[1] == 0)

_waiting = M.month_summary({"settings": {"defaultState": "none", "rateMinor": 6000,
                                         "qtyMl": 1000, "startDate": "2026-01-01"},
                            "days": {}}, 2026, 7, "2026-07-31")
ok("unrecorded days are counted", _waiting["pending"] == 31, str(_waiting["pending"]))
ok("unrecorded days are never billed", _waiting["amountMinor"] == 0)

ok("a settled day survives the round trip",
   M.sanitise({"days": {"2026-06-01": {"s": "off", "t": 1}}})["days"]["2026-06-01"]["s"] == "off")
ok("an unrecorded day survives the round trip",
   M.sanitise({"days": {"2026-06-01": {"s": "none", "t": 1}}})["days"]["2026-06-01"]["s"] == "none")


# --------------------------------------------- nothing a user has is ever lost
# The rule in docs/adr/0013: a build that does not recognise a field must carry
# it, not drop it. Clients ship before servers do, so this is not hypothetical.
print("\nMILKBOOK — an older build cannot erase a newer one's data")
_future = {"settings": {"t": 5, "vendor": "Dairy", "somethingNew": "keep me"},
           "days": {}, "unknownSection": {"x": 1}}
_clean = M.sanitise(_future)
ok("a setting this build never heard of is kept",
   _clean["settings"].get("somethingNew") == "keep me")
ok("a whole section this build never heard of is kept",
   _clean.get("unknownSection") == {"x": 1})
ok("the fields it does know are still cleaned",
   _clean["settings"]["vendor"] == "Dairy")
# The cap is on how much *unknown* data is carried; the sections this build
# knows about are always there. Derived rather than typed in, because adding a
# section is exactly when a hand-written number goes stale.
_junk = M.sanitise({"settings": {}, **{f"junk{i}": i for i in range(200)}})
ok("carried-through data is bounded",
   len(_junk) <= len(M.KNOWN_TOP) + 20, f"{len(_junk)} keys, {len(M.KNOWN_TOP)} known")
ok("and the known sections are all still there",
   M.KNOWN_TOP <= set(_junk), str(M.KNOWN_TOP - set(_junk)))
ok("an oversized unknown field is not carried",
   "big" not in M.sanitise({"settings": {}, "big": "x" * 9000}))

# a server that predates a setting answers without it; merging must not reset it
_new_client = {"settings": {"t": 9, "defaultState": "none", "lockAfterDays": 7}, "days": {}}
_old_server = {"settings": {"t": 12, "vendor": "Dairy"}, "days": {}}
_merged = M.merge(_new_client, _old_server)
ok("a newer reply does not erase settings it never carried",
   _merged["settings"].get("defaultState") == "none"
   and _merged["settings"].get("lockAfterDays") == 7,
   json.dumps(_merged["settings"]))
ok("the newer reply still wins where it does carry a value",
   _merged["settings"]["vendor"] == "Dairy")
ok("an empty peer cannot blank the settings",
   M.merge(_new_client, {"settings": {}, "days": {}})["settings"]["defaultState"] == "none")


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
with open(Path(__file__).resolve().parent / ".fixture.json", "w") as fh:
    json.dump(fixture, fh)
print("\n  wrote .fixture.json for the client cross-check")

print("\nPAYMENTS — money handed over is its own ledger")
# Two payments on one day are two payments, so they are keyed by id and merged
# one at a time. A whole-object merge would lose one of them the first time two
# people in a household both recorded something.
_pay = M.sanitise({"settings": {}, "days": {}, "payments": {
    "p1": {"on": "2026-01-20", "a": 5000, "note": "cash", "t": 3},
    "p2": {"on": "2026-01-20", "a": 3000, "t": 4},
}})
ok("payments are stored", len(_pay["payments"]) == 2, str(_pay["payments"]))
ok("the amount is kept as whole minor units", _pay["payments"]["p1"]["a"] == 5000)
ok("the note survives", _pay["payments"]["p1"]["note"] == "cash")
ok("a missing note becomes empty rather than None", _pay["payments"]["p2"]["note"] == "")
ok("the tombstone field is always present", _pay["payments"]["p1"]["del"] is False)

_bad = M.sanitise({"settings": {}, "payments": {
    "ok1": {"on": "2026-01-20", "a": 100},
    "no date": {"on": "", "a": 100},
    "bad$id": {"on": "2026-01-20", "a": 100},
    "negative": {"on": "2026-01-20", "a": -500},
    "notadict": "nope",
}})
ok("a payment with no usable date is dropped", "no date" not in _bad["payments"])
ok("an id that is not an id is dropped", "bad$id" not in _bad["payments"])
ok("a negative amount is clamped, not stored", _bad["payments"]["negative"]["a"] == 0)
ok("something that is not an object is dropped", "notadict" not in _bad["payments"])
ok("the good one still gets through", "ok1" in _bad["payments"])

_a = {"settings": {"t": 1}, "days": {}, "payments": {"p1": {"on": "2026-01-05", "a": 5000, "t": 10}}}
_b = {"settings": {"t": 1}, "days": {}, "payments": {"p2": {"on": "2026-01-05", "a": 3000, "t": 11}}}
ok("two phones' payments both survive a merge", len(M.merge(_a, _b)["payments"]) == 2)

_older = {"settings": {"t": 1}, "days": {}, "payments": {"p1": {"on": "2026-01-05", "a": 5000, "t": 10}}}
_newer = {"settings": {"t": 1}, "days": {}, "payments": {"p1": {"on": "2026-01-05", "a": 5500, "t": 20}}}
ok("the newer edit of one payment wins", M.merge(_older, _newer)["payments"]["p1"]["a"] == 5500)

_gone = {"settings": {"t": 1}, "days": {}, "payments": {"p1": {"on": "2026-01-05", "a": 5000, "t": 30, "del": True}}}
ok("a removal travels rather than being undone by the other copy",
   M.merge(_older, _gone)["payments"]["p1"]["del"] is True)

# A phone still running the previous build sends no payments key at all.
_old_client = {"settings": {"t": 2}, "days": {}}
ok("a client that has never heard of payments does not erase them",
   len(M.merge(_a, _old_client)["payments"]) == 1,
   "clients ship before servers, and old clients outlive both")

print("\nSETTINGS — a phone cannot erase a choice it has never heard of")
# The flags added for locking and full control are only written back when the
# client actually sent them. Giving them a default would mean an older phone --
# which does not know they exist -- answering with False and overwriting a
# choice somebody deliberately made. I introduced exactly that bug writing this
# and caught it by merging the two shapes together, so it is pinned here.
_chose = M.sanitise({"settings": {"t": 5, "lockAfterDays": 0, "lockChoice": True}, "days": {}})
_older = M.sanitise({"settings": {"t": 9, "vendor": "Dairy"}, "days": {}})
ok("a client that sent the flag has it stored", _chose["settings"]["lockChoice"] is True)
ok("a client that never sent it does not get a false one",
   "lockChoice" not in _older["settings"], str(_older["settings"]))
ok("so an older phone cannot undo a deliberate choice",
   M.merge(_chose, _older)["settings"].get("lockChoice") is True,
   "even though the older phone's settings are newer")
ok("and the same holds for full control",
   M.merge(M.sanitise({"settings": {"t": 5, "fullControl": True}, "days": {}}),
           _older)["settings"].get("fullControl") is True)
ok("full control is stored as a boolean when it is sent",
   M.sanitise({"settings": {"t": 1, "fullControl": "yes"}, "days": {}})["settings"]["fullControl"] is True)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
