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
import time
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

# Handing protection back drops the decision rather than making one, so the
# automatic rule applies again. bool(None) is False, so coercing the tombstone
# turned "protect this again" into "leave it unlocked" on the next merge.
handed_back = {"settings": {}, "days": {}, "locks": {"2026-06": {"on": None, "t": 300}}}
ok("handing protection back is kept as its own state, not squashed to unlocked",
   M.sanitise({"locks": {"2026-06": {"on": None, "t": 5}}})["locks"]["2026-06"]["on"] is None)
ok("and it reaches the other phones",
   M.merge(opened, handed_back)["locks"]["2026-06"]["on"] is None)
ok("an older unlock cannot bring itself back afterwards",
   M.merge(handed_back, opened)["locks"]["2026-06"]["on"] is None)
ok("a missing key is still a lock, not a hand-back",
   M.sanitise({"locks": {"2026-06": {"t": 5}}})["locks"]["2026-06"]["on"] is None)
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
   (M.KNOWN_TOP - {"roster"}) <= set(_junk), str(M.KNOWN_TOP - {"roster"} - set(_junk)))
# The roster is the one section a client does not get to send. It is the list of
# phones allowed to write, so accepting one from the body would let any phone
# write itself an invitation.
ok("a roster in the body is ignored, not stored",
   "roster" not in M.sanitise({"settings": {}, "days": {},
                               "roster": {"evil": {"h": "x", "ok": True}}}))
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

print("\nSETTINGS — two phones changing two different things")
# The whole settings object used to be one item with one timestamp. Because
# sanitise gives every known field a default whether the client sent it or not,
# a phone that merely saved later overwrote settings it had never touched. A
# two-phone test against the live mirror is what caught it: one phone's vendor
# name simply vanished.
_dad = M.sanitise({"settings": {"t": 10, "vendor": "Verma Dairy", "at": {"vendor": 10}},
                   "days": {}})
_mum = M.sanitise({"settings": {"t": 11, "qtyMl": 500, "at": {"qtyMl": 11}}, "days": {}})
_both = M.merge(_dad, _mum)["settings"]
ok("one phone's vendor name survives", _both["vendor"] == "Verma Dairy", _both["vendor"])
ok("the other phone's litres survive", _both["qtyMl"] == 500, str(_both["qtyMl"]))
ok("whichever order they merge in",
   M.merge(_mum, _dad)["settings"]["vendor"] == "Verma Dairy"
   and M.merge(_mum, _dad)["settings"]["qtyMl"] == 500)
ok("the stamps travel with the settings", "at" in _both and _both["at"].get("vendor") == 10)

_early = M.sanitise({"settings": {"t": 1, "vendor": "A", "at": {"vendor": 1}}, "days": {}})
_late = M.sanitise({"settings": {"t": 2, "vendor": "B", "at": {"vendor": 2}}, "days": {}})
ok("the same setting edited twice takes the later one",
   M.merge(_early, _late)["settings"]["vendor"] == "B")

# Books written before stamping existed still merge the old way.
_oldA = M.sanitise({"settings": {"t": 5, "vendor": "Old"}, "days": {}})
_oldB = M.sanitise({"settings": {"t": 9, "vendor": "Newer"}, "days": {}})
ok("books from before stamping fall back to the whole-book timestamp",
   M.merge(_oldA, _oldB)["settings"]["vendor"] == "Newer")

print("\nRATES — what a litre cost on the day")
_rated = M.sanitise({"settings": {"t": 1, "rateMinor": 6500, "startDate": "2026-07-01", "qtyMl": 1000},
                     "days": {f"2026-07-{d:02d}": {"s": "yes", "t": 1} for d in range(1, 32)},
                     "rates": {"2026-07-01": {"r": 6000, "t": 1},
                               "2026-08-04": {"r": 6500, "t": 1}}})
ok("rate changes are stored", len(_rated["rates"]) == 2, str(sorted(_rated["rates"])))
_july = M.month_summary(_rated, 2026, 7, "2026-08-20")
ok("a month is billed at the rate in force, not the latest one",
   _july["amountMinor"] == 186000, str(_july["amountMinor"]))
ok("the server and the page round the same way",
   M._round_half_up(2.5) == 3 and M._round_half_up(3.5) == 4,
   "Python's round is half-to-even and would give 2 and 4")
_mixed = M.sanitise({"settings": {"t": 1, "rateMinor": 8000, "startDate": "2026-09-01", "qtyMl": 1000},
                     "days": {f"2026-09-{d:02d}": {"s": "yes", "t": 1} for d in range(1, 11)},
                     "rates": {"2026-09-01": {"r": 6000, "t": 1}, "2026-09-06": {"r": 8000, "t": 1}}})
ok("a rise mid-month splits the month",
   M.month_summary(_mixed, 2026, 9, "2026-09-10")["amountMinor"] == 5 * 6000 + 5 * 8000,
   str(M.month_summary(_mixed, 2026, 9, "2026-09-10")["amountMinor"]))

print("\nA RECORDED DAY COUNTS WHEREVER IT FALLS")
# The bill skipped everything before the start date, record or no record. A
# phone that joined a family book kept its own later start, so the family's
# earlier months sat on the phone and billed as nothing. The page and the
# mirror both did it, so they agreed on the wrong answer.
_early = M.sanitise({
    "settings": {"t": 1, "qtyMl": 1000, "rateMinor": 6000, "startDate": "2026-08-01",
                 "defaultState": "yes"},
    "days": {f"2026-06-{d:02d}": {"s": "yes", "t": 1} for d in range(1, 31)},
})
_june = M.month_summary(_early, 2026, 6, "2026-08-04")
ok("30 recorded June days are billed", _june["delivered"] == 30, str(_june["delivered"]))
ok("and for the real amount", _june["amountMinor"] == 30 * 6000, str(_june["amountMinor"]))

_blank = M.sanitise({
    "settings": {"t": 1, "qtyMl": 1000, "rateMinor": 6000, "startDate": "2026-08-01",
                 "defaultState": "yes"},
    "days": {},
})
ok("an unrecorded day before the start is still not billed",
   M.month_summary(_blank, 2026, 6, "2026-08-04")["delivered"] == 0)
ok("the start date still governs days nobody touched",
   M.month_summary(_blank, 2026, 8, "2026-08-04")["delivered"] == 4, "1 to 4 August")

print("\nRATE LIMITS A CALLER CANNOT CHOOSE FOR ITSELF")
# The bucket was the first entry of X-Forwarded-For, which is whatever the
# caller wrote before any proxy saw the request. Measured: 400 of 400 got
# through a limit of 90 just by changing that header each time.
import io as _io


def _call(headers=None, path="/api/store/AAAA-BBBB-CCCC"):
    env = {"PATH_INFO": path, "REQUEST_METHOD": "GET", "REMOTE_ADDR": "203.0.113.9",
           "wsgi.input": _io.BytesIO(b""), "CONTENT_LENGTH": "0"}
    env.update(headers or {})
    got = {}
    body = M.app(env, lambda s, h: got.__setitem__("status", int(s.split()[0])))
    b"".join(body)
    return got["status"]


def _allowed(make, n=400, path="/api/store/AAAA-BBBB-CCCC"):
    M._hits.clear()
    return sum(1 for i in range(n) if _call(make(i), path) != 429)


ok("an honest caller gets the limit it is given",
   _allowed(lambda i: {"HTTP_X_FORWARDED_FOR": "198.51.100.7, 172.16.0.1"}) == M.RATE_LIMIT)
ok("rewriting the front of the chain buys nothing",
   _allowed(lambda i: {"HTTP_X_FORWARDED_FOR": f"10.0.0.{i % 256}, 172.16.0.1"}) == M.RATE_LIMIT,
   "the last hop is written by the proxy, not the caller")
ok("forging every address header still hits the per-book ceiling",
   _allowed(lambda i: {"HTTP_CF_CONNECTING_IP": f"10.0.0.{i % 256}",
                       "HTTP_TRUE_CLIENT_IP": f"10.1.0.{i % 256}",
                       "HTTP_X_FORWARDED_FOR": f"10.2.0.{i % 256}"}) == M.CODE_RATE_LIMIT,
   "no header can widen a single book's ceiling")
M._hits.clear()
ok("one household cannot throttle another",
   sum(1 for i in range(300)
       if _call({"HTTP_CF_CONNECTING_IP": f"10.0.0.{i % 256}"},
                f"/api/store/{'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'[i % 32]}AAA-BBBB-CCCC") != 429) == 300)
ok("Cloudflare's header is preferred over a chain the caller can write into",
   M.client_key({"HTTP_CF_CONNECTING_IP": "9.9.9.9",
                 "HTTP_X_FORWARDED_FOR": "1.1.1.1, 2.2.2.2"}) == "9.9.9.9")
ok("junk in a trusted header falls through rather than making a new bucket",
   M.client_key({"HTTP_CF_CONNECTING_IP": "not-an-address",
                 "HTTP_X_FORWARDED_FOR": "1.1.1.1, 2.2.2.2"}) == "2.2.2.2")
ok("with no headers at all it counts the connection itself",
   M.client_key({"REMOTE_ADDR": "203.0.113.9"}) == "203.0.113.9")
M._hits.clear()

print("\nWHAT A SYNC CODE IS WORTH GUESSING")
_alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
ok("the alphabet leaves out the characters people misread",
   not any(c in _alphabet for c in "OI01"))
ok("a code is 12 characters from 32, so about 60 bits",
   len(_alphabet) == 32 and M.CODE_RE.match("ABCD-EFGH-JKLM") is not None)
ok("lower case and stray punctuation are refused, not guessed at",
   M.CODE_RE.match("abcd-efgh-jklm") is None and M.CODE_RE.match("ABCD-EFGH-JKL0") is None)
ok("a path that is not a code never reaches the store",
   _call(path="/api/store/../../etc/passwd") == 400)
ok("nor does one with a wildcard in it", _call(path="/api/store/%2A") == 400)

print("\nONLY PHONES THE HOUSEHOLD LET IN CAN CHANGE THE BOOK")
# The code used to be the whole key: anyone it was forwarded to could rewrite
# months of somebody's records. A phone now has to be let in by one already on
# the book before anything it sends is applied.
_BOOK = "WXYZ-2345-6789"


def _post(body, code=_BOOK):
    payload = json.dumps(body).encode()
    env = {"PATH_INFO": f"/api/store/{code}", "REQUEST_METHOD": "POST",
           "REMOTE_ADDR": "203.0.113.9", "CONTENT_LENGTH": str(len(payload)),
           "wsgi.input": _io.BytesIO(payload)}
    got = {}
    body_out = M.app(env, lambda s, h: got.__setitem__("status", int(s.split()[0])))
    return got["status"], json.loads(b"".join(body_out).decode())


def _day(key, state):
    return {"settings": {"t": 1}, "days": {key: {"s": state, "t": int(time.time() * 1000)}}}


M._hits.clear()
_founder = {"id": "founder-device-1", "key": "k" * 40, "name": "Mum's phone"}
_stranger = {"id": "stranger-device", "key": "z" * 40, "name": "Someone else"}

_st, _r = _post(dict(_day("2026-06-01", "yes"), device=_founder))
ok("the first phone on a new code founds the book", _r["you"]["ok"] and _r["you"]["why"] == "founder")
ok("and its records are kept", "2026-06-01" in _r["days"])

_st, _r = _post(dict(_day("2026-06-02", "skip"), device=_stranger))
ok("a phone nobody let in is told it is waiting",
   _r["you"]["ok"] is False and _r["you"]["why"] == "pending")
ok("and nothing it sent is written", "2026-06-02" not in _r["days"])
ok("but it can still read the book, so nobody is ever locked out of their own records",
   "2026-06-01" in _r["days"])
ok("it shows up on the roster as waiting",
   _r["roster"][_stranger["id"]]["ok"] is False)
ok("both phones can show the same number to check against each other",
   _r["roster"][_stranger["id"]]["label"] == M.device_label(_stranger["id"]),
   _r["roster"][_stranger["id"]]["label"])

# A waiting phone must not be able to let itself in.
_st, _r = _post(dict(_day("2026-06-03", "skip"), device=_stranger,
                     approve=[_stranger["id"]]))
ok("a waiting phone cannot approve itself", _r["roster"][_stranger["id"]]["ok"] is False)
ok("nor slip a record in while trying", "2026-06-03" not in _r["days"])

# Nor claim to be a phone that was let in.
_st, _r = _post(dict(_day("2026-06-04", "skip"),
                     device={"id": _founder["id"], "key": "wrong-key-" + "q" * 30,
                             "name": "Impostor"}))
ok("claiming another phone's name without its secret is refused",
   _r["you"]["ok"] is False and _r["you"]["why"] == "wrong-key")
ok("and writes nothing", "2026-06-04" not in _r["days"])
ok("and does not rename the phone it was pretending to be",
   _r["roster"][_founder["id"]]["n"] == "Mum's phone")

# The founder lets it in, and only then does its work count.
_st, _r = _post(dict(_day("2026-06-05", "yes"), device=_founder, approve=[_stranger["id"]]))
ok("a phone already on the book can let another in", _r["roster"][_stranger["id"]]["ok"] is True)
_st, _r = _post(dict(_day("2026-06-06", "skip"), device=_stranger))
ok("once let in, its records are kept", "2026-06-06" in _r["days"] and _r["you"]["ok"])

# Removing a phone takes its write access away again.
_st, _r = _post(dict(_day("2026-06-07", "yes"), device=_founder, revoke=[_stranger["id"]]))
ok("a phone can be removed", _stranger["id"] not in _r["roster"])
_st, _r = _post(dict(_day("2026-06-08", "skip"), device=_stranger))
ok("and is waiting again, not writing", "2026-06-08" not in _r["days"])

ok("the last phone on a book cannot remove itself and strand it",
   _post(dict(_day("2026-06-09", "yes"), device=_founder,
              revoke=[_founder["id"]]))[1]["roster"].get(_founder["id"], {}).get("ok") is True)

# A build from before any of this existed must not walk past the gate.
_st, _r = _post(_day("2026-06-10", "skip"))
ok("a caller that identifies no device at all is refused on an enrolled book",
   _r["you"]["ok"] is False and _r["you"]["why"] == "no-device")
ok("and writes nothing", "2026-06-10" not in _r["days"])

ok("no device secret ever leaves the server",
   all("h" not in entry for entry in _r["roster"].values())
   and "k" * 40 not in json.dumps(_r))
ok("a book that has never enrolled a phone still accepts an older build",
   _post(_day("2026-06-11", "yes"), code="2345-6789-WXYZ")[1]["days"].get("2026-06-11") is not None,
   "so nobody's phone stops syncing the moment this ships")
M._hits.clear()

print("\nTWO PHONES SYNCING AT THE SAME MOMENT")
# Reading and writing on separate connections meant both worked from the copy
# they had read, and whichever wrote last dropped the other's day. Forty pairs
# out of forty lost one before the write was made to hold the book still.
import threading as _threading

_RACE = "RACE-2345-6789"


def _race_post(body):
    payload = json.dumps(body).encode()
    env = {"PATH_INFO": f"/api/store/{_RACE}", "REQUEST_METHOD": "POST",
           "REMOTE_ADDR": "203.0.113.9", "CONTENT_LENGTH": str(len(payload)),
           "wsgi.input": _io.BytesIO(payload)}
    got = {}
    out = M.app(env, lambda s, h: got.__setitem__("s", int(s.split()[0])))
    b"".join(out)


_one = {"id": "race-device-one", "key": "r" * 40, "name": "One"}
_two = {"id": "race-device-two", "key": "w" * 40, "name": "Two"}
M._hits.clear()
_race_post({"settings": {"t": 1}, "days": {}, "device": _one})
_race_post({"settings": {"t": 1}, "days": {}, "device": _two})
_race_post({"settings": {"t": 1}, "days": {}, "device": _one, "approve": [_two["id"]]})


def _record(dev, day):
    _race_post({"settings": {"t": 1},
                "days": {day: {"s": "yes", "t": int(time.time() * 1000)}},
                "device": dev})


_lost = 0
for _i in range(12):
    _a, _b = f"2026-04-{_i + 1:02d}", f"2026-05-{_i + 1:02d}"
    _t1 = _threading.Thread(target=_record, args=(_one, _a))
    _t2 = _threading.Thread(target=_record, args=(_two, _b))
    _t1.start(); _t2.start(); _t1.join(); _t2.join()
    _days = M.read_book(_RACE).get("days") or {}
    if _a not in _days or _b not in _days:
        _lost += 1
ok("neither phone's day goes missing", _lost == 0, f"{_lost} of 12 pairs lost one")
ok("the write holds the book still while it changes it",
   "BEGIN IMMEDIATE" in Path(__file__).with_name("app.py").read_text(encoding="utf-8"))
M._hits.clear()

print("\nTHE CHANGE LOG TRAVELS BETWEEN PHONES")
_a = M.sanitise({"settings": {"t": 1}, "days": {},
                 "log": [{"i": "one", "t": 3, "d": "dev-a", "m": "12 Aug marked away"}]})
_b = M.sanitise({"settings": {"t": 1}, "days": {},
                 "log": [{"i": "two", "t": 5, "d": "dev-b", "m": "Payment recorded"}]})
_both = M.merge(_a, _b)["log"]
ok("both phones' lines survive a merge", len(_both) == 2, str(len(_both)))
ok("newest first", _both[0]["i"] == "two")
ok("merging twice does not double them", len(M.merge(M.merge(_a, _b), _b)["log"]) == 2)
ok("a line with no id is dropped rather than stored",
   M.sanitise({"settings": {}, "days": {}, "log": [{"m": "no id"}]})["log"] == [])
ok("the message is capped so the log cannot become storage",
   len(M.sanitise({"settings": {}, "days": {},
                   "log": [{"i": "x", "m": "y" * 500}]})["log"][0]["m"]) == 120)
_flood = M.sanitise({"settings": {}, "days": {},
                     "log": [{"i": f"n{i}", "t": i, "m": "x"} for i in range(M.MAX_LOG + 200)]})
ok("and the whole log is bounded", len(_flood["log"]) == M.MAX_LOG, str(len(_flood["log"])))

print("\nKINDS OF MILK — a day says which one it was")
# The whole feature rests on one thing: sanitise() rebuilds every day from
# scratch, so a per-day field it has not been told about is silently dropped.
# A day recorded as the expensive kind would come back from the first sync
# priced as the default, and nobody would see it happen.
_kinds = [{"id": "toned", "name": "Toned", "rateMinor": 5000, "t": 5},
          {"id": "full", "name": "Full cream", "rateMinor": 8000, "t": 5}]
_kb = M.sanitise({"settings": {"t": 1, "rateMinor": 6000, "qtyMl": 1000,
                               "startDate": "2026-03-01", "kinds": _kinds},
                  "days": {"2026-03-01": {"s": "yes", "t": 1},
                           "2026-03-02": {"s": "yes", "t": 1, "k": "toned"},
                           "2026-03-03": {"s": "yes", "t": 1, "k": "full"}}})
ok("the kind survives a round trip", _kb["days"]["2026-03-02"].get("k") == "toned",
   json.dumps(_kb["days"]["2026-03-02"]))
ok("a day with no kind stays that way", "k" not in _kb["days"]["2026-03-01"])
ok("the kinds themselves survive", len(_kb["settings"]["kinds"]) == 2,
   json.dumps(_kb["settings"].get("kinds")))

_march = M.month_summary(_kb, 2026, 3, "2026-03-03")
ok("each day is billed at its own kind's price",
   _march["amountMinor"] == 6000 + 5000 + 8000, str(_march["amountMinor"]))
ok("and the litres still add up", _march["totalMl"] == 3000 and _march["delivered"] == 3,
   f"{_march['totalMl']}ml over {_march['delivered']}d")

ok("a kind nobody has heard of falls back to the default price rather than nothing",
   M.month_summary(M.sanitise({"settings": {"t": 1, "rateMinor": 6000, "qtyMl": 1000,
                                            "startDate": "2026-03-01"},
                               "days": {"2026-03-01": {"s": "yes", "t": 1, "k": "ghost"}}}),
                   2026, 3, "2026-03-01")["amountMinor"] == 6000)

ok("a junk kind id on a day is dropped",
   "k" not in M.sanitise({"days": {"2026-03-01": {"s": "yes", "t": 1, "k": "../../etc"}}})["days"]["2026-03-01"])
ok("so is one that is not a string",
   "k" not in M.sanitise({"days": {"2026-03-01": {"s": "yes", "t": 1, "k": 7}}})["days"]["2026-03-01"])
ok("a kind with no id is not stored",
   M.sanitise({"settings": {"kinds": [{"name": "Nameless", "rateMinor": 1}]}})["settings"]["kinds"] == [])
ok("a kind's price is capped like any other",
   M.sanitise({"settings": {"kinds": [{"id": "a", "rateMinor": 10 ** 12}]}})
   ["settings"]["kinds"][0]["rateMinor"] == 10_000_000)
ok("a kind's name is trimmed",
   len(M.sanitise({"settings": {"kinds": [{"id": "a", "name": "x" * 500}]}})
       ["settings"]["kinds"][0]["name"]) == 40)
ok("too many kinds are cut off",
   len(M.sanitise({"settings": {"kinds": [{"id": f"k{i}"} for i in range(M.MAX_KINDS + 20)]}})
       ["settings"]["kinds"]) == M.MAX_KINDS)
ok("a duplicate id is not stored twice",
   len(M.sanitise({"settings": {"kinds": [{"id": "a", "name": "One"}, {"id": "a", "name": "Two"}]}})
       ["settings"]["kinds"]) == 1)
ok("kinds that are not a list are ignored",
   M.sanitise({"settings": {"kinds": "toned"}})["settings"]["kinds"] == [])
ok("a phone that has never heard of kinds does not send an empty list",
   "kinds" not in M.sanitise({"settings": {"t": 1, "vendor": "Dairy"}})["settings"])

print("\nKINDS OF MILK — two phones adding one each")
# Last-write-wins on the whole list would drop one of them, and every day
# already recorded against the lost kind would quietly revert to the default
# price. They are unioned by id instead.
_phoneA = M.sanitise({"settings": {"t": 10, "at": {"kinds": 10},
                                   "kinds": [{"id": "toned", "name": "Toned", "rateMinor": 5000, "t": 10}]},
                      "days": {}})
_phoneB = M.sanitise({"settings": {"t": 11, "at": {"kinds": 11},
                                   "kinds": [{"id": "full", "name": "Full cream", "rateMinor": 8000, "t": 11}]},
                      "days": {}})
_both = M.merge(_phoneA, _phoneB)["settings"]["kinds"]
ok("both kinds survive the merge", sorted(k["id"] for k in _both) == ["full", "toned"],
   json.dumps(_both))

_edited = M.sanitise({"settings": {"t": 20, "at": {"kinds": 20},
                                   "kinds": [{"id": "toned", "name": "Toned", "rateMinor": 5500, "t": 20}]},
                      "days": {}})
_after = {k["id"]: k for k in M.merge(_phoneA, _edited)["settings"]["kinds"]}
ok("a price change to one kind wins on that kind alone",
   _after["toned"]["rateMinor"] == 5500, json.dumps(_after))
_stale = {k["id"]: k for k in M.merge(_edited, _phoneA)["settings"]["kinds"]}
ok("and an older copy of it does not win back",
   _stale["toned"]["rateMinor"] == 5500, json.dumps(_stale))

print("\nMORE THAN ONE VENDOR")
# Two vendors selling the same milk at different prices are two kinds, each
# naming its vendor. The day names the kind, so it names the vendor too.
_vend = M.sanitise({
    "settings": {"t": 1, "vendor": "Ramesh", "rateMinor": 6000, "qtyMl": 1000,
                 "startDate": "2026-03-01", "defaultState": "none",
                 "vendors": [{"id": "suresh", "name": "Suresh", "t": 5}],
                 "kinds": [{"id": "rcow", "name": "Cow", "rateMinor": 5500, "v": "", "t": 5},
                           {"id": "scow", "name": "Cow", "rateMinor": 6200, "v": "suresh", "t": 5},
                           {"id": "sbuf", "name": "Buffalo", "rateMinor": 9000, "v": "suresh", "t": 5}]},
    "days": {"2026-03-01": {"s": "yes", "t": 1, "k": "rcow", "more": [{"k": "sbuf"}]},
             "2026-03-02": {"s": "yes", "t": 1, "k": "scow"}},
})
ok("vendors survive a round trip", len(_vend["settings"]["vendors"]) == 1,
   json.dumps(_vend["settings"].get("vendors")))
ok("a kind remembers who sells it",
   {k["id"]: k.get("v", "") for k in _vend["settings"]["kinds"]}
   == {"rcow": "", "scow": "suresh", "sbuf": "suresh"})
ok("the same milk can carry two prices",
   {k["id"]: k["rateMinor"] for k in _vend["settings"]["kinds"]}["rcow"] == 5500
   and {k["id"]: k["rateMinor"] for k in _vend["settings"]["kinds"]}["scow"] == 6200)

ok("a second delivery on one day survives", _vend["days"]["2026-03-01"].get("more") == [{"k": "sbuf"}],
   json.dumps(_vend["days"]["2026-03-01"]))
_march = M.month_summary(_vend, 2026, 3, "2026-03-02")
ok("both deliveries on a day are billed", _march["amountMinor"] == 5500 + 9000 + 6200,
   str(_march["amountMinor"]))
ok("and both sets of litres counted", _march["totalMl"] == 3000, str(_march["totalMl"]))
ok("but a day with two deliveries is still one day", _march["delivered"] == 2, str(_march["delivered"]))
ok("the server and the page agree on the total",
   _march["amountMinor"] == 20700, str(_march["amountMinor"]))

_q = M.sanitise({"settings": {"t": 1, "rateMinor": 6000, "qtyMl": 1000, "startDate": "2026-03-01",
                              "defaultState": "none",
                              "kinds": [{"id": "a", "rateMinor": 5000, "t": 1},
                                        {"id": "b", "rateMinor": 9000, "t": 1}]},
                 "days": {"2026-03-01": {"s": "yes", "t": 1, "k": "a", "more": [{"k": "b", "q": 500}]}}})
ok("a second delivery keeps its own quantity",
   M.month_summary(_q, 2026, 3, "2026-03-01")["amountMinor"] == 5000 + 4500,
   str(M.month_summary(_q, 2026, 3, "2026-03-01")["amountMinor"]))

ok("a junk extra is dropped",
   "more" not in M.sanitise({"days": {"2026-03-01": {"s": "yes", "t": 1, "more": [{"k": "../etc"}]}}})["days"]["2026-03-01"])
ok("extras that are not a list are ignored",
   "more" not in M.sanitise({"days": {"2026-03-01": {"s": "yes", "t": 1, "more": "b"}}})["days"]["2026-03-01"])
ok("too many extras on one day are cut off",
   len(M.sanitise({"days": {"2026-03-01": {"s": "yes", "t": 1,
                                           "more": [{"k": f"k{i}"} for i in range(M.MAX_EXTRA + 10)]}}})
       ["days"]["2026-03-01"]["more"]) == M.MAX_EXTRA)
ok("too many vendors are cut off",
   len(M.sanitise({"settings": {"vendors": [{"id": f"v{i}"} for i in range(M.MAX_VENDORS + 10)]}})
       ["settings"]["vendors"]) == M.MAX_VENDORS)
ok("a phone that has never heard of vendors does not send an empty list",
   "vendors" not in M.sanitise({"settings": {"t": 1, "vendor": "Dairy"}})["settings"])

ok("a payment can name the vendor it went to",
   M.sanitise({"settings": {}, "payments": {"p1": {"on": "2026-03-02", "a": 100, "t": 1, "v": "suresh"}}})
   ["payments"]["p1"]["v"] == "suresh")
ok("a payment from before vendors existed names nobody",
   "v" not in M.sanitise({"settings": {}, "payments": {"p1": {"on": "2026-03-02", "a": 100, "t": 1}}})
   ["payments"]["p1"])

_va = M.sanitise({"settings": {"t": 10, "at": {"vendors": 10},
                               "vendors": [{"id": "suresh", "name": "Suresh", "t": 10}]}, "days": {}})
_vb = M.sanitise({"settings": {"t": 11, "at": {"vendors": 11},
                               "vendors": [{"id": "gopal", "name": "Gopal", "t": 11}]}, "days": {}})
ok("two phones each adding a vendor keep both",
   sorted(v["id"] for v in M.merge(_va, _vb)["settings"]["vendors"]) == ["gopal", "suresh"],
   json.dumps(M.merge(_va, _vb)["settings"]["vendors"]))

print("\nWHEN TWO PHONES DISAGREE, THE LATER EDIT WINS")
# One rule, everywhere in the book, so there is never a question of which part
# resolves conflicts which way.
_old = {"settings": {"t": 1, "at": {"vendor": 1}, "vendor": "Old"},
        "days": {"2026-05-01": {"s": "yes", "t": 100}},
        "locks": {"2026-05": {"on": True, "t": 100}},
        "payments": {"p1": {"on": "2026-05-01", "a": 100, "t": 100, "del": False}},
        "rates": {"2026-05-01": {"r": 5000, "t": 100}}}
_new = {"settings": {"t": 2, "at": {"vendor": 2}, "vendor": "New"},
        "days": {"2026-05-01": {"s": "skip", "t": 200}},
        "locks": {"2026-05": {"on": False, "t": 200}},
        "payments": {"p1": {"on": "2026-05-01", "a": 250, "t": 200, "del": False}},
        "rates": {"2026-05-01": {"r": 6000, "t": 200}}}
_a, _b = M.sanitise(_old), M.sanitise(_new)
for _name, _merged in (("older first", M.merge(_a, _b)), ("newer first", M.merge(_b, _a))):
    ok(f"a day takes the later edit ({_name})", _merged["days"]["2026-05-01"]["s"] == "skip",
       json.dumps(_merged["days"]["2026-05-01"]))
    ok(f"a lock does too ({_name})", _merged["locks"]["2026-05"]["on"] is False)
    ok(f"a payment does too ({_name})", _merged["payments"]["p1"]["a"] == 250)
    ok(f"a rate does too ({_name})", _merged["rates"]["2026-05-01"]["r"] == 6000)
    ok(f"and a setting does too ({_name})", _merged["settings"]["vendor"] == "New")

# The log is the exception, and deliberately: it only ever gains entries, so
# nothing in it is ever overwritten by anything else.
_la = M.sanitise({"settings": {}, "log": [{"i": "aaaaaaaa", "t": 100, "m": "one"}]})
_lb = M.sanitise({"settings": {}, "log": [{"i": "bbbbbbbb", "t": 200, "m": "two"}]})
ok("the change log keeps both sides rather than choosing",
   len(M.merge(_la, _lb)["log"]) == 2, json.dumps([e["m"] for e in M.merge(_la, _lb)["log"]]))

print("\nA PHONE WITH A WRONG CLOCK CANNOT WIN EVERY ARGUMENT")
# "Newest wins" is only as good as the clock behind it. A phone days fast would
# beat every later correction anyone else made, and hand back its own stale copy
# over the top of them.
_far = int(time.time() * 1000) + 30 * 24 * 3600 * 1000
_skewed = M.sanitise({"settings": {"t": 1},
                      "days": {"2026-05-01": {"s": "yes", "t": _far}},
                      "payments": {"p1": {"on": "2026-05-01", "a": 100, "t": _far, "del": False}},
                      "rates": {"2026-05-01": {"r": 5000, "t": _far}},
                      "log": [{"i": "cccccccc", "t": _far, "m": "from the future"}]})
_now = int(time.time() * 1000)
ok("a day stamped a month ahead is pulled back to now",
   _skewed["days"]["2026-05-01"]["t"] <= _now + 1000, str(_skewed["days"]["2026-05-01"]["t"] - _now))
ok("so is a payment", _skewed["payments"]["p1"]["t"] <= _now + 1000)
ok("so is a rate change", _skewed["rates"]["2026-05-01"]["t"] <= _now + 1000)
ok("so is a log entry", _skewed["log"][0]["t"] <= _now + 1000)

_slight = _now + 60 * 1000
ok("a clock a minute out is left alone, because that is ordinary",
   M.sanitise({"days": {"2026-05-01": {"s": "yes", "t": _slight}}})["days"]["2026-05-01"]["t"] == _slight)
ok("an edit made in the past is never moved",
   M.sanitise({"days": {"2026-05-01": {"s": "yes", "t": 100}}})["days"]["2026-05-01"]["t"] == 100,
   "a phone that was offline for a week must keep its own times")
ok("a nonsense timestamp becomes nothing rather than throwing",
   M.sanitise({"days": {"2026-05-01": {"s": "yes", "t": "soon"}}})["days"]["2026-05-01"]["t"] == 0)
ok("and so does a negative one",
   M.sanitise({"days": {"2026-05-01": {"s": "yes", "t": -5}}})["days"]["2026-05-01"]["t"] == 0)

print("\nTHE GROUP SURVIVES THE MIRROR FORGETTING IT")
# The store is a file in a container with no disk under it, so a deploy or an
# idle spin-down loses every roster. What happened next is the bug: the first
# phone to sync founded a new group containing only itself, and every other
# phone in the house became a stranger asking to be let in.
_KEY_A, _KEY_B = "a" * 40, "b" * 40
_dev_a = {"id": "phoneaaaaaaaaaaaaaaa", "key": _KEY_A, "name": "Mum's phone"}
_dev_b = {"id": "phonebbbbbbbbbbbbbbb", "key": _KEY_B, "name": "Dad's phone"}

_book, _ok, _why = M.enrol({}, _dev_a)
ok("the first phone founds the group", _ok and _why == "founder")
_book, _ok, _why = M.enrol(_book, _dev_b)
ok("the second arrives waiting", not _ok and _why == "pending")
_book = M.apply_roster_changes(_book, _dev_a["id"], [_dev_b["id"]], [])
ok("and is let in by the first", _book["roster"][_dev_b["id"]]["ok"] is True)

# What a phone remembers is what an approved phone is given: names, standing and
# the fingerprint of each device's secret — never the secret.
_remembered = M.public_roster(_book, _dev_a["id"])
ok("what a phone remembers carries no secrets",
   all(v.get("h") not in (_KEY_A, _KEY_B) for v in _remembered.values()), json.dumps(_remembered))

_wiped: dict = {}
_back = M.adopt_roster(_remembered, _dev_b["id"])
ok("a phone that was in the group can put it back", set(_back) == {_dev_a["id"], _dev_b["id"]},
   json.dumps(sorted(_back)))
ok("and everybody keeps the standing they had", _back[_dev_a["id"]]["ok"] and _back[_dev_b["id"]]["ok"])
ok("the fingerprints come back so a returning phone still has to prove itself",
   all(v.get("h") for v in _back.values()))

_wiped["roster"] = _back
_wiped, _ok, _why = M.enrol(_wiped, _dev_b)
ok("that phone is straight back in, not waiting", _ok and _why == "in")
ok("its key still matches the fingerprint that came back",
   _wiped["roster"][_dev_b["id"]]["h"] == M._hash_key(_KEY_B))
_wiped, _ok, _why = M.enrol(_wiped, _dev_a)
ok("and so is the other one, without anybody approving anything again", _ok and _why == "in")
ok("once bound, the wrong key is still refused",
   M.enrol(_wiped, {"id": _dev_a["id"], "key": "z" * 40, "name": "Impostor"})[2] == "wrong-key")

ok("a phone that was not in the group cannot put one back",
   M.adopt_roster(_remembered, "strangerrrrrrrrrrrrr") == {})
ok("nor can one that was only ever waiting",
   M.adopt_roster({_dev_b["id"]: {"n": "x", "ok": False, "t": 1}}, _dev_b["id"]) == {})
ok("a roster with nobody approved in it is not adopted",
   M.adopt_roster({_dev_a["id"]: {"n": "x", "ok": False, "t": 1}}, _dev_a["id"]) == {})
ok("junk is not adopted", M.adopt_roster("roster", _dev_a["id"]) == {}
   and M.adopt_roster(None, _dev_a["id"]) == {})
ok("a bad device id in a restored roster is dropped",
   "../x" not in M.adopt_roster({**_remembered, "../x": {"n": "x", "ok": True, "t": 1}}, _dev_b["id"]))

print("\nPUTTING THE GROUP BACK MUST NOT LET A STRANGER IN")
# The first cut of the restore did. Device ids are readable by anyone holding
# the code — they are roster keys, and they are on every change-log entry — and
# restored entries carried no key, so whoever claimed an id first got it. A
# code holder could walk into a household's book that way, which before the
# restore existed they could not: they would have sat pending.
#
# What travels now is the fingerprint of each device's secret, never the secret,
# and only to a phone already approved. Inverting SHA-256 of a 48-character
# random key is not a thing, so the fingerprint is safe to hold; presenting it
# is not enough, only the key that matches it is.
_m = {"id": "mumphoneaaaaaaaaaaaa", "key": "a" * 40, "name": "Mum's phone"}
_d = {"id": "dadphonebbbbbbbbbbbb", "key": "b" * 40, "name": "Dad's phone"}
_grp, _, _ = M.enrol({}, _m)
_grp, _, _ = M.enrol(_grp, _d)
_grp = M.apply_roster_changes(_grp, _m["id"], [_d["id"]], [])

_anyone = M.public_roster(_grp)
_approved = M.public_roster(_grp, _m["id"])
ok("a bare code holder is given no fingerprints",
   all("h" not in v for v in _anyone.values()), json.dumps(_anyone))
ok("an approved phone is given them", all("h" in v for v in _approved.values()))
ok("but never the secret itself",
   all(v["h"] not in ("a" * 40, "b" * 40) for v in _approved.values()))
ok("an unapproved phone in the roster gets none either",
   all("h" not in v for v in M.public_roster(_grp, "strangerrrrrrrrrrrrr").values()))

_w = {"roster": M.adopt_roster(_approved, _m["id"])}
_w, _okm, _whym = M.enrol(_w, _m)
ok("the phone that restores the group is in", _okm and _whym == "in")
_w, _okd, _whyd = M.enrol(_w, _d)
ok("and so is the other one, with nobody approving anything again", _okd and _whyd == "in")
ok("an impostor claiming that phone's id is refused outright",
   M.enrol({"roster": M.adopt_roster(_approved, _m["id"])},
           {"id": _d["id"], "key": "z" * 40, "name": "Impostor"})[2] == "wrong-key")

# A phone that remembered the group before fingerprints travelled cannot prove
# anyone. Those come back named so they are recognisable, but waiting.
_old = {"roster": M.adopt_roster(_anyone, _m["id"])}
_old, _oko, _ = M.enrol(_old, _m)
ok("restoring from an older phone still lets that phone in", _oko)
ok("but the others come back waiting rather than trusted",
   M.enrol({"roster": M.adopt_roster(_anyone, _m["id"])}, _d)[2] == "pending",
   "a person confirms, which is what happened before any of this existed")
ok("so an impostor gains nothing there either",
   M.enrol({"roster": M.adopt_roster(_anyone, _m["id"])},
           {"id": _d["id"], "key": "z" * 40, "name": "Impostor"})[1] is False)
ok("their name still comes back, so the person approving knows who it is",
   M.adopt_roster(_anyone, _m["id"])[_d["id"]]["n"] == "Dad's phone")

ok("a made-up fingerprint is not accepted as one",
   "h" not in M.adopt_roster({_m["id"]: dict(_approved[_m["id"]]),
                              _d["id"]: {"n": "x", "ok": True, "t": 1, "h": "not-a-digest"}},
                             _m["id"])[_d["id"]])
ok("and that entry is therefore not trusted",
   M.adopt_roster({_m["id"]: dict(_approved[_m["id"]]),
                   _d["id"]: {"n": "x", "ok": True, "t": 1, "h": "nope"}},
                  _m["id"])[_d["id"]]["ok"] is False)

print(f"\n{PASS} passed, {FAIL} failed\n")
sys.exit(1 if FAIL else 0)
