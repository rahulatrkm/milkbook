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

import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from http import HTTPStatus

DB_PATH = os.environ.get("MILKBOOK_DB", os.path.join(os.path.dirname(__file__), "milkbook.db"))

CODE_RE = re.compile(r"^[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}-[A-HJ-NP-Z2-9]{4}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
STATES = {"yes", "skip", "away", "none", "off"}
DEFAULT_STATES = {"yes", "skip", "none"}

MAX_BODY = 512 * 1024
MAX_DAYS = 20_000
MAX_MONTHS = 1_200
MAX_PAYMENTS = 20_000
MAX_LOG = 300
LOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
PAYMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
RATE_LIMIT = 90          # requests per caller
RATE_WINDOW = 60         # seconds
CODE_RATE_LIMIT = 120    # requests per book, which no header can widen

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


def _book_from_row(row) -> dict:
    if not row:
        return {"settings": {}, "days": {}, "locks": {}}
    try:
        book = json.loads(row[0])
    except json.JSONDecodeError:
        return {"settings": {}, "days": {}, "locks": {}}
    book.setdefault("locks", {})       # books stored before locks existed
    return book


def read_book(code: str) -> dict:
    with _db() as conn:
        row = conn.execute("SELECT data FROM books WHERE code = ?", (code,)).fetchone()
    return _book_from_row(row)


def write_book(code: str, book: dict) -> None:
    payload = json.dumps(book, separators=(",", ":"))
    with _db() as conn:
        conn.execute(
            "INSERT INTO books(code, data, updated) VALUES(?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET data=excluded.data, updated=excluded.updated",
            (code, payload, int(time.time())),
        )


def update_book(code: str, change):
    """Read, change and write a book with nobody able to slip in between.

    Reading and writing on separate connections meant two phones syncing at the
    same moment both worked from the copy they had read, and whichever wrote
    last silently dropped the other's day. Measured at forty pairs out of forty
    before this. BEGIN IMMEDIATE takes the write lock up front, so the second
    request waits rather than working from a book that is already stale.

    `change` is handed the stored book and returns (book_to_store, reply);
    returning None for the book leaves what is there untouched.
    """
    conn = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS books("
            " code TEXT PRIMARY KEY, data TEXT NOT NULL, updated INTEGER NOT NULL)"
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT data FROM books WHERE code = ?", (code,)).fetchone()
            book = _book_from_row(row)
            to_store, reply = change(book)
            if to_store is not None:
                conn.execute(
                    "INSERT INTO books(code, data, updated) VALUES(?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET data=excluded.data, updated=excluded.updated",
                    (code, json.dumps(to_store, separators=(",", ":")), int(time.time())),
                )
            conn.execute("COMMIT")
            return reply
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


# ---------------------------------------------------------------------- domain
def _carry_unknown(raw: dict, known: set) -> dict:
    """Fields this build does not recognise, kept rather than dropped.

    A client is often newer than the server it talks to, and this endpoint
    answers with what it stored. Sanitising an unknown field away would mean a
    phone loses a setting it just saved, simply because this process has not
    been redeployed. Bounded so it cannot become a place to put anything.
    """
    out: dict = {}
    for key, value in raw.items():
        if key in known or not isinstance(key, str) or len(key) > 40 or len(out) >= 20:
            continue
        try:
            if len(json.dumps(value)) <= 4096:
                out[key] = value
        except (TypeError, ValueError):
            continue
    return out


KNOWN_SETTINGS = {"t", "at", "vendor", "qtyMl", "rateMinor", "currency", "startDate",
                  "skipWeekly", "lockAfterDays", "defaultState", "lockChoice", "fullControl",
                  "kinds", "vendors"}
# The roster is the server's, not the client's: it is never read out of an
# incoming body, only changed through approve/revoke by a device already in it.
# That way a phone that has not been let in cannot write itself an invitation.
KNOWN_TOP = {"settings", "days", "locks", "payments", "rates", "roster", "log"}
# Envelope, not book. These carry the caller's own credential, so letting the
# unknown-field rule sweep them up would store a device secret in the book and
# hand it back to anyone holding the code — which is everything this guards.
ENVELOPE = {"device", "approve", "revoke", "restore"}
NEVER_STORE = KNOWN_TOP | ENVELOPE
DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DEVICE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{20,128}$")
MAX_DEVICES = 50
KIND_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")
MAX_KINDS = 24
MAX_VENDORS = 12
# More than a few deliveries on one date is a different app. The ceiling is here
# so a bad merge cannot grow a day without end.
MAX_EXTRA = 6
# How far ahead of this mirror a phone's clock may be and still be believed.
# Everything merges by "newest wins", so a phone whose clock is days fast would
# win every disagreement for as long as it stayed wrong — including handing back
# its own stale copy over somebody else's later correction. Small differences
# are ordinary and kept; a wild one is pulled back to now, which is the latest
# the edit can honestly be.
SKEW_GRACE_MS = 5 * 60 * 1000


def _stamp(value) -> int:
    """A timestamp from a phone, never further ahead than this mirror's clock."""
    try:
        when = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if when < 0:
        return 0
    now = int(time.time() * 1000)
    return now if when > now + SKEW_GRACE_MS else when


def _clean_vendors(raw) -> list:
    """The people who bring the milk. The book's original one is not in here:
    it is settings['vendor'], and stays vendor "" so nothing has to migrate."""
    out: list = []
    seen: set = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_VENDORS]:
        if not isinstance(item, dict):
            continue
        vid = item.get("id")
        if not isinstance(vid, str) or not KIND_ID_RE.match(vid) or vid in seen:
            continue
        vendor = {
            "id": vid,
            "name": str(item.get("name") or "")[:60],
            "t": int(item["t"]) if isinstance(item.get("t"), (int, float)) else 0,
        }
        if item.get("off"):
            vendor["off"] = True
        seen.add(vid)
        out.append(vendor)
    return out


def _clean_kinds(raw) -> list:
    """The kinds of milk a book deals in, each with its own price.

    A day names the kind it was, so these have to survive a round trip or every
    day recorded as anything but the default silently reverts to the default
    price on the next sync.
    """
    out: list = []
    seen: set = set()
    if not isinstance(raw, list):
        return out
    for item in raw[:MAX_KINDS]:
        if not isinstance(item, dict):
            continue
        kid = item.get("id")
        if not isinstance(kid, str) or not KIND_ID_RE.match(kid) or kid in seen:
            continue
        rate = item.get("rateMinor")
        kind = {
            "id": kid,
            "name": str(item.get("name") or "")[:40],
            "rateMinor": max(0, min(int(rate), 10_000_000)) if isinstance(rate, (int, float)) else 0,
            "t": int(item["t"]) if isinstance(item.get("t"), (int, float)) else 0,
        }
        # Which vendor sells it. Two vendors charging differently for the same
        # milk are two kinds, which is the whole reason this field exists.
        vid = item.get("v")
        if isinstance(vid, str) and KIND_ID_RE.match(vid):
            kind["v"] = vid
        if item.get("off"):
            kind["off"] = True
        seen.add(kid)
        out.append(kind)
    return out


def _merge_kinds(a_kinds, b_kinds) -> list:
    """Union by id, newest wins per entry. Used for kinds and for vendors.

    Last-write-wins on the whole list would drop one added on one phone the
    moment another phone saved any setting — and every day already recorded
    against it would quietly fall back to the default price.
    """
    out: dict = {}
    for kind in list(a_kinds or []) + list(b_kinds or []):
        if not isinstance(kind, dict) or not kind.get("id"):
            continue
        prev = out.get(kind["id"])
        if prev is None or (kind.get("t") or 0) >= (prev.get("t") or 0):
            out[kind["id"]] = kind
    return list(out.values())[:MAX_KINDS]


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
            "lockAfterDays": max(0, min(int(raw_settings.get("lockAfterDays") or 0), 366)),
            "defaultState": (raw_settings.get("defaultState")
                             if raw_settings.get("defaultState") in DEFAULT_STATES else "yes"),
        }
        # Only written back when the client actually sent them. Forcing a
        # default in would mean a phone on an older build, which has never heard
        # of these, answering with False and erasing a choice somebody made --
        # the exact thing the field-wise merge below exists to prevent.
        for flag in ("lockChoice", "fullControl"):
            if flag in raw_settings:
                settings[flag] = bool(raw_settings[flag])
        # Same reasoning as the flags above: only written back when the client
        # sent it, so a phone that has never heard of milk kinds does not answer
        # with an empty list and erase them.
        if "kinds" in raw_settings:
            settings["kinds"] = _clean_kinds(raw_settings.get("kinds"))
        if "vendors" in raw_settings:
            settings["vendors"] = _clean_vendors(raw_settings.get("vendors"))
        # When each individual setting last changed. Without this the whole
        # settings object is one item with one timestamp, and because every
        # field above is given a default whether the client sent it or not, a
        # phone that merely saved later wipes a setting it never touched.
        raw_at = raw_settings.get("at")
        if isinstance(raw_at, dict):
            settings["at"] = {k: int(v) for k, v in list(raw_at.items())[:40]
                              if isinstance(k, str) and len(k) <= 40
                              and isinstance(v, (int, float))}
        if not DAY_RE.match(settings["startDate"]):
            settings["startDate"] = ""
        settings.update(_carry_unknown(raw_settings, KNOWN_SETTINGS))

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
            clean = {"s": state, "t": _stamp(entry.get("t"))}
            qty = entry.get("q")
            if isinstance(qty, (int, float)):
                clean["q"] = max(0, min(int(qty), 100_000))
            # Which kind of milk came that day. Unknown day fields are rebuilt
            # away here, so this has to be named or it never survives a sync.
            kind = entry.get("k")
            if isinstance(kind, str) and KIND_ID_RE.match(kind):
                clean["k"] = kind
            # Anything else that came the same day, which is how two vendors on
            # one date are held. Same rebuild rule: unnamed here, dropped.
            more = entry.get("more")
            if isinstance(more, list):
                extras = []
                for x in more[:MAX_EXTRA]:
                    if not isinstance(x, dict):
                        continue
                    xk = x.get("k")
                    if not isinstance(xk, str) or not KIND_ID_RE.match(xk):
                        continue
                    one = {"k": xk}
                    xq = x.get("q")
                    if isinstance(xq, (int, float)):
                        one["q"] = max(0, min(int(xq), 100_000))
                    extras.append(one)
                if extras:
                    clean["more"] = extras
            days[key] = clean

    # Month locks. Stored the same way as a day so they merge the same way: a
    # lock set on one phone has to reach the others, or it is not a lock.
    raw_locks = book.get("locks")
    locks: dict = {}
    if isinstance(raw_locks, dict):
        if len(raw_locks) > MAX_MONTHS:
            raise ValueError("too many months")
        for key, entry in raw_locks.items():
            if not MONTH_RE.match(key) or not isinstance(entry, dict):
                continue
            # null is a third state, not a falsy one: it means "no decision
            # here, use the automatic rule". Coercing it with bool() turned
            # handing protection back into an unlock on the next merge.
            on = entry.get("on")
            locks[key] = {"on": None if on is None else bool(on),
                          "t": _stamp(entry.get("t"))}

    # Money handed over, kept as its own ledger rather than folded into a day.
    # A payment is not a delivery: it has its own date, and two of them on the
    # same day are two payments, so they are keyed by id and merged one at a
    # time. A removed payment keeps a tombstone, because absence does not
    # travel over a merge — the other phone would hand it straight back.
    raw_payments = book.get("payments")
    payments: dict = {}
    if isinstance(raw_payments, dict):
        if len(raw_payments) > MAX_PAYMENTS:
            raise ValueError("too many payments")
        for key, entry in raw_payments.items():
            if not PAYMENT_ID_RE.match(key) or not isinstance(entry, dict):
                continue
            on = str(entry.get("on") or "")[:10]
            if not DAY_RE.match(on):
                continue
            payments[key] = {
                "on": on,
                "a": max(0, min(int(entry.get("a") or 0), 1_000_000_000)),
                "note": str(entry.get("note") or "")[:60],
                "t": _stamp(entry.get("t")),
                "del": bool(entry.get("del")),
            }
            # Who the money went to. Absent means the book's original vendor,
            # which is who every payment made before this went to.
            pv = entry.get("v")
            if isinstance(pv, str) and KIND_ID_RE.match(pv):
                payments[key]["v"] = pv

    # What a litre cost, from a given date onwards. Without this a price rise
    # restates every bill ever sent, so the history is part of the record.
    raw_rates = book.get("rates")
    rates: dict = {}
    if isinstance(raw_rates, dict):
        if len(raw_rates) > MAX_MONTHS:
            raise ValueError("too many rate changes")
        for key, entry in raw_rates.items():
            if not DAY_RE.match(key) or not isinstance(entry, dict):
                continue
            rates[key] = {
                "r": max(0, min(int(entry.get("r") or 0), 10_000_000)),
                "t": _stamp(entry.get("t")),
            }

    raw_log = book.get("log")
    log: list = []
    if isinstance(raw_log, list):
        for item in raw_log[:MAX_LOG]:
            if not isinstance(item, dict):
                continue
            entry_id = str(item.get("i") or "")
            if not LOG_ID_RE.match(entry_id):
                continue
            log.append({
                "i": entry_id,
                "t": _stamp(item.get("t")),
                "d": str(item.get("d") or "")[:64],
                "n": str(item.get("n") or "")[:40],
                "m": str(item.get("m") or "")[:120],
            })

    return {"settings": settings, "days": days, "locks": locks, "payments": payments,
            "rates": rates, "log": log, **_carry_unknown(book, NEVER_STORE)}


# ------------------------------------------------------------------- devices
def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def device_label(device_id: str) -> str:
    """A short number both phones can show, so nobody approves blind.

    Derived from the id rather than sent alongside it, so what the joining phone
    displays and what the approving phone displays cannot disagree.
    """
    digest = hashlib.sha256(("milkbook-label:" + device_id).encode()).hexdigest()
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(alphabet[int(digest[i * 2:i * 2 + 2], 16) % 32] for i in range(4))


def enrol(book: dict, device: dict) -> tuple[dict, bool, str]:
    """Place a device in the book's roster and say whether it may write.

    The first device on a book that has no roster founds it. Everyone after
    that arrives pending and stays pending until somebody already in the roster
    lets them in, so a code passed on to the wrong person is enough to read a
    household's book but never to change it.
    """
    roster = book.get("roster")
    device_id = str(device.get("id") or "")
    key = str(device.get("key") or "")
    name = str(device.get("name") or "A phone")[:40]
    if not DEVICE_ID_RE.match(device_id) or not DEVICE_KEY_RE.match(key):
        return book, False, "bad-device"

    digest = _hash_key(key)
    if not isinstance(roster, dict) or not roster:
        book["roster"] = {device_id: {"h": digest, "n": name, "ok": True,
                                      "t": int(time.time() * 1000), "by": None}}
        return book, True, "founder"

    entry = roster.get(device_id)
    if entry is None:
        if len(roster) >= MAX_DEVICES:
            return book, False, "too-many"
        roster[device_id] = {"h": digest, "n": name, "ok": False,
                             "t": int(time.time() * 1000), "by": None}
        return book, False, "pending"

    stored_digest = str(entry.get("h") or "")
    if not stored_digest:
        # An entry put back from a phone's memory after this mirror lost its
        # roster. The secret was never in what a phone is allowed to see, so
        # there is nothing to compare against and it binds now.
        entry["h"] = digest
    # An id that is already taken has to prove it owns the secret, or anyone
    # could name an approved device and inherit its permission.
    elif not hmac.compare_digest(stored_digest, digest):
        return book, False, "wrong-key"
    entry["n"] = name
    entry["t"] = int(time.time() * 1000)
    return book, bool(entry.get("ok")), "in" if entry.get("ok") else "pending"


def adopt_roster(raw, device_id: str) -> dict:
    """A group a phone remembered, taken back after the mirror lost its own.

    The store is a file in a container with no disk under it, so a deploy or an
    idle spin-down loses every book and every roster. What happened next was the
    thing being fixed: the first phone to sync afterwards founded a brand new
    group containing only itself, and every other phone in the household turned
    into a stranger asking to be let in.

    Only a phone that was itself in the group, and approved, may put it back —
    which is no more trust than was already given to whoever synced first.
    Keys are not restored, because a phone has never been allowed to see them;
    each device binds its own again on its next visit.
    """
    if not isinstance(raw, dict):
        return {}
    mine = raw.get(device_id)
    if not isinstance(mine, dict) or not mine.get("ok"):
        return {}
    out: dict = {}
    for did, entry in list(raw.items())[:MAX_DEVICES]:
        if not isinstance(did, str) or not DEVICE_ID_RE.match(did) or not isinstance(entry, dict):
            continue
        out[did] = {"n": str(entry.get("n") or "A phone")[:40],
                    "ok": bool(entry.get("ok")),
                    "t": int(entry.get("t") or 0) if isinstance(entry.get("t"), (int, float)) else 0,
                    "by": None}
    return out if any(v["ok"] for v in out.values()) else {}


def apply_roster_changes(book: dict, actor: str, approve, revoke) -> dict:
    """Only a device already approved may let another in or put it out."""
    roster = book.get("roster") or {}
    if not roster.get(actor, {}).get("ok"):
        return book
    now = int(time.time() * 1000)
    for device_id in (approve or [])[:MAX_DEVICES]:
        entry = roster.get(str(device_id))
        if entry is not None and not entry.get("ok"):
            entry["ok"] = True
            entry["by"] = actor
            entry["t"] = now
    for device_id in (revoke or [])[:MAX_DEVICES]:
        device_id = str(device_id)
        # Removing the last approved device would leave a book nobody can write
        # to, and reading is not enough to keep a household going.
        if device_id in roster:
            remaining = [k for k, v in roster.items()
                         if v.get("ok") and k != device_id]
            if roster[device_id].get("ok") and not remaining:
                continue
            roster.pop(device_id)
    book["roster"] = roster
    return book


def public_roster(book: dict) -> dict:
    """What a phone is allowed to see: who is on the book, never their secrets."""
    out = {}
    for device_id, entry in (book.get("roster") or {}).items():
        out[device_id] = {"n": entry.get("n") or "A phone", "ok": bool(entry.get("ok")),
                          "t": int(entry.get("t") or 0), "label": device_label(device_id)}
    return out


def _seen(book: dict) -> dict:
    """The book as a phone may see it: the roster without the hashed secrets.

    Every reply goes through here, so a device secret cannot leave the server by
    being forgotten on one path.
    """
    out = {k: v for k, v in book.items() if k != "roster"}
    if book.get("roster"):
        out["roster"] = public_roster(book)
    return out


def merge(a: dict, b: dict) -> dict:
    """Per-day and per-setting last-write-wins."""
    a_set, b_set = a.get("settings") or {}, b.get("settings") or {}
    a_at, b_at = a_set.get("at") or {}, b_set.get("at") or {}
    settings: dict = {**a_set, **b_set}
    at: dict = {}
    for key in set(a_set) | set(b_set):
        if key in ("at", "t"):
            continue
        a_when, b_when = a_at.get(key), b_at.get(key)
        if key not in a_set:
            take_b = True
        elif key not in b_set:
            take_b = False
        elif a_when is None and b_when is None:
            # Books written before settings carried their own timestamps.
            take_b = (b_set.get("t") or 0) > (a_set.get("t") or 0)
        else:
            # A setting nobody stamped is being carried, not changed, so it
            # must not beat one somebody actually edited.
            take_b = (b_when or 0) > (a_when or 0)
        settings[key] = b_set[key] if take_b else a_set[key]
        chosen = b_when if take_b else a_when
        if chosen is not None:
            at[key] = chosen
    if at:
        settings["at"] = at
    settings["t"] = max(a_set.get("t") or 0, b_set.get("t") or 0)
    if "kinds" in a_set or "kinds" in b_set:
        settings["kinds"] = _merge_kinds(a_set.get("kinds"), b_set.get("kinds"))
    if "vendors" in a_set or "vendors" in b_set:
        settings["vendors"] = _merge_kinds(a_set.get("vendors"), b_set.get("vendors"))

    def newest(x_map: dict, y_map: dict) -> dict:
        out = {}
        for key in set(x_map) | set(y_map):
            x, y = x_map.get(key), y_map.get(key)
            if x is None:
                out[key] = y
            elif y is None:
                out[key] = x
            else:
                out[key] = y if (y.get("t") or 0) > (x.get("t") or 0) else x
        return out

    merged = {
        "settings": settings,
        "days": newest(a.get("days") or {}, b.get("days") or {}),
        "locks": newest(a.get("locks") or {}, b.get("locks") or {}),
        "payments": newest(a.get("payments") or {}, b.get("payments") or {}),
        "rates": newest(a.get("rates") or {}, b.get("rates") or {}),
        **_carry_unknown(a, NEVER_STORE),
        **_carry_unknown(b, NEVER_STORE),
    }
    # The log only gains entries, so the union of both is the whole story. It is
    # trimmed to the same ceiling here as on the phones, or two of them would
    # keep handing each other back what the other had dropped.
    seen: dict = {}
    for item in list(a.get("log") or []) + list(b.get("log") or []):
        if isinstance(item, dict) and item.get("i") and item["i"] not in seen:
            seen[item["i"]] = item
    merged["log"] = sorted(seen.values(), key=lambda e: e.get("t") or 0, reverse=True)[:MAX_LOG]
    # The roster is only ever the stored one. It is not merged from the body,
    # because the body is the one thing an unapproved phone controls.
    if isinstance(a.get("roster"), dict):
        merged["roster"] = a["roster"]
    return merged


def day_state(book: dict, key: str) -> tuple[str, int]:
    settings = book.get("settings") or {}
    entry = (book.get("days") or {}).get(key)
    dow = (datetime.strptime(key, "%Y-%m-%d").weekday() + 1) % 7  # python: Mon=0 -> js: Sun=0
    if entry and entry.get("s"):
        state = entry["s"]
    elif dow in (settings.get("skipWeekly") or []):
        state = "off"
    else:
        default = settings.get("defaultState")
        state = default if default in DEFAULT_STATES else "yes"
    if state != "yes":
        return state, 0
    qty = entry.get("q") if entry and entry.get("q") is not None else settings.get("qtyMl", 1000)
    return state, int(qty)


def rate_for(book: dict, day_key: str) -> int:
    """What a litre cost on that day, from the rate history if there is one."""
    rates = book.get("rates") or {}
    best = None
    for start in rates:
        if start <= day_key and (best is None or start > best):
            best = start
    if best is None:
        return int((book.get("settings") or {}).get("rateMinor") or 0)
    return int(rates[best].get("r") or 0)


def rate_for_kind(book: dict, day_key: str, kind_id: str) -> int:
    """The price of one kind on a day: its own, or the default timeline.

    A kind nobody here has heard of falls back to the default rather than
    billing at nothing — that happens for a moment when one phone adds a kind
    and another has not synced it yet.
    """
    if kind_id:
        for kind in (book.get("settings") or {}).get("kinds") or []:
            if isinstance(kind, dict) and kind.get("id") == kind_id:
                return int(kind.get("rateMinor") or 0)
    return rate_for(book, day_key)


def rate_for_day(book: dict, day_key: str) -> int:
    entry = (book.get("days") or {}).get(day_key) or {}
    return rate_for_kind(book, day_key, entry.get("k") or "")


def _round_half_up(value: float) -> int:
    """JavaScript's Math.round, which is half away from zero.

    Python's round() is half to even, so 2.5 becomes 2 there and 3 in the page.
    One paisa of disagreement between the bill on the phone and the bill in the
    calendar feed is the kind of thing somebody notices and cannot explain.
    """
    return int(Decimal(value).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def deliveries_of(book: dict, day_key: str) -> list:
    """Every delivery on one day: the one on the record, then any others."""
    state, qty = day_state(book, day_key)
    if state != "yes":
        return []
    entry = (book.get("days") or {}).get(day_key) or {}
    settings = book.get("settings") or {}
    out = [(entry.get("k") or "", qty, rate_for_kind(book, day_key, entry.get("k") or ""))]
    for x in (entry.get("more") or []):
        if not isinstance(x, dict) or not x.get("k"):
            continue
        xq = x.get("q")
        out.append((x["k"],
                    int(xq) if isinstance(xq, (int, float)) else int(settings.get("qtyMl") or 1000),
                    rate_for_kind(book, day_key, x["k"])))
    return out


def month_summary(book: dict, year: int, month: int, today_key: str) -> dict:
    settings = book.get("settings") or {}
    start = settings.get("startDate") or "0000-01-01"
    last = (date(year + (month == 12), (month % 12) + 1, 1) - timedelta(days=1)).day

    total_ml = delivered = skipped = away = off = pending = 0
    by_rate: dict = {}
    days = book.get("days") or {}
    for d in range(1, last + 1):
        key = f"{year:04d}-{month:02d}-{d:02d}"
        if key > today_key:
            continue
        # A recorded day counts wherever it falls. Dropping everything before
        # the start date meant a phone carrying the family's earlier months but
        # its own later start date billed them as nothing.
        if key < start and key not in days:
            continue
        state, qty = day_state(book, key)
        if state == "yes":
            delivered += 1
            for _kind, got_qty, r in deliveries_of(book, key):
                total_ml += got_qty
                by_rate[r] = by_rate.get(r, 0) + got_qty
        elif state == "skip":
            skipped += 1
        elif state == "away":
            away += 1
        elif state == "none":
            pending += 1
        else:
            off += 1

    amount = sum(_round_half_up(ml * r / 1000) for r, ml in by_rate.items())
    return {"totalMl": total_ml, "delivered": delivered, "skipped": skipped,
            "away": away, "off": off, "pending": pending, "amountMinor": amount}


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


def _rate_ok(key: str, limit: int = RATE_LIMIT) -> bool:
    now = time.time()
    bucket = _hits[key]
    while bucket and now - bucket[0] > RATE_WINDOW:
        bucket.popleft()
    if len(bucket) >= limit:
        return False
    bucket.append(now)
    if len(_hits) > 8000:
        for k in [k for k, v in _hits.items() if not v][:4000]:
            _hits.pop(k, None)
    return True


_IP_RE = re.compile(r"^[0-9a-fA-F:.]{3,45}$")


def client_key(environ) -> str:
    """Who to count a request against.

    The first entry of X-Forwarded-For is whatever the caller wrote before any
    proxy saw it, so counting against it gave anyone a fresh bucket per request:
    measured at 400 of 400 getting through a limit of 90. Only values a proxy
    sets are any use — Cloudflare's own header, or failing that the last entry
    in the chain, which is written by the hop nearest to us and is the one part
    the caller cannot reach past.
    """
    for name in ("HTTP_CF_CONNECTING_IP", "HTTP_TRUE_CLIENT_IP"):
        value = (environ.get(name) or "").strip()
        if _IP_RE.match(value):
            return value
    chain = [p.strip() for p in (environ.get("HTTP_X_FORWARDED_FOR") or "").split(",") if p.strip()]
    if chain and _IP_RE.match(chain[-1]):
        return chain[-1]
    return environ.get("REMOTE_ADDR", "?")


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
    ip = client_key(environ)

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
        if not _rate_ok("code:" + code, CODE_RATE_LIMIT):
            return _json(start_response, HTTPStatus.TOO_MANY_REQUESTS, {"error": "slow down"}, cors)
        ics = build_ics(read_book(code)).encode()
        return _reply(start_response, HTTPStatus.OK, ics,
                      "text/calendar; charset=utf-8",
                      cors + [("Content-Disposition", 'inline; filename="milkbook.ics"')])

    # /api/store/<code>
    if path.startswith("/api/store/"):
        code = path[len("/api/store/"):].upper()
        if not CODE_RE.match(code):
            return _json(start_response, HTTPStatus.BAD_REQUEST, {"error": "bad code"}, cors)
        # A ceiling on one book that no header can widen, so however a caller
        # dresses itself up it cannot sit on somebody's book all day.
        if not _rate_ok("code:" + code, CODE_RATE_LIMIT):
            return _json(start_response, HTTPStatus.TOO_MANY_REQUESTS, {"error": "slow down"}, cors)

        if method == "GET":
            # Reading stays open to anyone holding the code. It is the only way
            # back to a book when every approved phone has been lost, and a
            # household losing its records is the worse failure of the two.
            return _json(start_response, HTTPStatus.OK, _seen(read_book(code)), cors)

        if method == "POST":
            try:
                length = int(environ.get("CONTENT_LENGTH") or 0)
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY:
                return _json(start_response, HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                             {"error": "body too large"}, cors)
            try:
                raw = json.loads(environ["wsgi.input"].read(length))
                incoming = sanitise(raw)
            except (ValueError, KeyError, TypeError):
                return _json(start_response, HTTPStatus.BAD_REQUEST, {"error": "bad body"}, cors)

            def change(stored):
                device = raw.get("device") if isinstance(raw, dict) else None

                if not isinstance(device, dict):
                    # A build from before devices existed. It may write only
                    # while the book has no roster; once a household has
                    # enrolled a phone, an unidentified caller is exactly what
                    # this is here to stop.
                    if stored.get("roster"):
                        return None, dict(_seen(stored), you={"ok": False, "why": "no-device"})
                    merged = merge(stored, incoming)
                    return merged, _seen(merged)

                device_id = str(device.get("id") or "")
                # Before anyone is enrolled: if this mirror holds no roster at
                # all and the phone remembers one it was part of, take it back.
                # There is no disk under the store, so this is the ordinary case
                # after a deploy rather than a rare one.
                revived = ""
                if not stored.get("roster"):
                    back = adopt_roster(
                        (raw.get("restore") or {}).get("roster")
                        if isinstance(raw.get("restore"), dict) else None,
                        device_id)
                    if back:
                        stored["roster"] = back
                        revived = "restored"

                stored, may_write, why = enrol(stored, device)
                standing = {"ok": may_write, "why": revived or why, "id": device_id,
                            "label": device_label(device_id)}

                if not may_write:
                    # The request is answered in full so the phone can show the
                    # book and say it is waiting, but nothing it sent is applied.
                    keep = stored if why in ("pending", "founder", "in") else None
                    return keep, dict(_seen(stored), you=standing)

                stored = apply_roster_changes(stored, device_id,
                                              raw.get("approve"), raw.get("revoke"))
                merged = merge(stored, incoming)
                return merged, dict(_seen(merged), you=standing)

            return _json(start_response, HTTPStatus.OK, update_book(code, change), cors)

        return _json(start_response, HTTPStatus.METHOD_NOT_ALLOWED, {"error": "no"}, cors)

    return _json(start_response, HTTPStatus.NOT_FOUND, {"error": "not found"}, cors)
