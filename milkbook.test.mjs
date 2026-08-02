// Headless harness for the Milkbook client. Runs the real page script against
// a stubbed DOM and checks the billing maths, the merge, and the .ics output —
// then cross-checks the bill against the Python server on shared fixtures,
// because the same rules now live in two languages.
import fs from "node:fs";
import vm from "node:vm";

const FILE = process.argv[2] || "/Users/rahul./Downloads/code/automaton/businesses/milkbook/index.html";
const js = fs.readFileSync(FILE, "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];

const stubEl = () => new Proxy({ style: {}, classList: { add(){}, remove(){}, toggle(){}, contains(){ return false; } }, dataset: {}, children: [], value: "", textContent: "", innerHTML: "" }, {
  get(t, k){
    if (k in t) return t[k];
    if (k === "appendChild" || k === "addEventListener" || k === "setAttribute" || k === "remove" || k === "click") return () => {};
    if (k === "getBoundingClientRect") return () => ({ left:0, top:0, width:10, height:10 });
    return undefined;
  },
  set(t, k, v){ t[k] = v; return true; },
});

const sandbox = {
  console, Math, Date, JSON, Object, Array, String, Number, Error, Set, Intl,
  TextEncoder, Uint8Array, Blob: class {}, URL: { createObjectURL: () => "", revokeObjectURL(){} },
  setTimeout: () => 0, clearTimeout(){}, fetch: () => Promise.reject(new Error("offline")),
  crypto: { getRandomValues: a => { for (let i = 0; i < a.length; i++) a[i] = (i * 37 + 11) & 255; return a; } },
  localStorage: { _d: {}, getItem(k){ return this._d[k] ?? null; }, setItem(k, v){ this._d[k] = v; } },
  navigator: {},
  document: { getElementById: stubEl, createElement: stubEl, createDocumentFragment: stubEl, addEventListener(){}, body: stubEl(), hidden: false },
};
sandbox.window = sandbox; sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(js, sandbox, { filename: "milkbook.js" });

const g = n => vm.runInContext(n, sandbox);
const monthSummary = g("monthSummary"), mergeStores = g("mergeStores"), buildICS = g("buildICS");
const dayInfo = g("dayInfo"), billText = g("billText"), icsFold = g("icsFold"), addDays = g("addDays");

let pass = 0, fail = 0;
const ok = (name, cond, extra = "") => {
  if (cond) { pass++; console.log(`  PASS  ${name}${extra ? "  " + extra : ""}`); }
  else { fail++; console.log(`  FAIL  ${name}  ${extra}`); }
};

const book = (days = {}, s = {}) => ({
  settings: Object.assign({ t: 1, vendor: "Test Dairy", qtyMl: 1000, rateMinor: 6000,
    currency: "INR", startDate: "2026-01-01", skipWeekly: [] }, s),
  days,
});

console.log("\nMILKBOOK client — billing");
{
  ok("a full month bills exactly", monthSummary(book(), 2026, 5, "2026-06-30").amountMinor === 180000,
     `${monthSummary(book(), 2026, 5, "2026-06-30").amountMinor}`);
  const s = monthSummary(book({ "2026-06-05": { s:"skip", t:2 }, "2026-06-10": { s:"away", t:2 } }), 2026, 5, "2026-06-30");
  ok("skipped and away are excluded", s.delivered === 28 && s.amountMinor === 168000, `${s.delivered}d ${s.amountMinor}`);
  ok("future days are not billed", monthSummary(book(), 2026, 5, "2026-06-10").delivered === 10);
  ok("pre-start days are not billed", monthSummary(book({}, { startDate:"2026-06-15" }), 2026, 5, "2026-06-30").delivered === 16);
  ok("weekly off is excluded", monthSummary(book({}, { skipWeekly:[0] }), 2026, 5, "2026-06-30").off === 4);
  const q = monthSummary(book({ "2026-06-03": { s:"yes", q:2000, t:2 } }), 2026, 5, "2026-06-30");
  ok("a custom quantity is billed", q.totalMl === 31000, `${q.totalMl}ml`);
  ok("an empty month is zero", monthSummary(book(), 2026, 5, "2025-01-01").amountMinor === 0);
  ok("a leap February is 29 days", monthSummary(book({}, { startDate:"2024-01-01" }), 2024, 1, "2024-02-29").delivered === 29);
}

console.log("\nMILKBOOK client — day states");
{
  ok("an unmarked day defaults to delivered", dayInfo(book(), "2026-06-03").state === "yes");
  ok("a weekly-off day defaults to off", dayInfo(book({}, { skipWeekly:[0] }), "2026-06-07").state === "off");
  ok("an explicit mark beats the weekly off",
     dayInfo(book({ "2026-06-07": { s:"yes", t:9 } }, { skipWeekly:[0] }), "2026-06-07").state === "yes");
  ok("a skipped day carries no quantity", dayInfo(book({ "2026-06-04": { s:"skip", t:1 } }), "2026-06-04").qtyMl === 0);
  ok("a custom quantity is reported as custom",
     dayInfo(book({ "2026-06-04": { s:"yes", q:1500, t:1 } }), "2026-06-04").custom === true);
}

console.log("\nMILKBOOK client — merge");
{
  const a = book({ "2026-06-01": { s:"skip", t:100 } });
  const b = book({ "2026-06-02": { s:"away", t:100 } });
  const m = mergeStores(a, b);
  ok("edits to different days both survive", m.days["2026-06-01"].s === "skip" && m.days["2026-06-02"].s === "away");
  const c = book({ "2026-06-01": { s:"away", t:200 } });
  ok("the newer edit wins", mergeStores(a, c).days["2026-06-01"].s === "away");
  ok("merge is commutative", JSON.stringify(mergeStores(a, c).days) === JSON.stringify(mergeStores(c, a).days));
  ok("merge is idempotent", JSON.stringify(mergeStores(mergeStores(a, c), c)) === JSON.stringify(mergeStores(a, c)));
  ok("newer settings win", mergeStores(book({}, { t:1, rateMinor:5000 }), book({}, { t:2, rateMinor:7000 })).settings.rateMinor === 7000);
  ok("an empty peer does not wipe local data", Object.keys(mergeStores(a, { settings:{}, days:{} }).days).length === 1);
}

console.log("\nMILKBOOK client — calendar file");
{
  const b = book({
    "2026-06-10": { s:"away", t:1 }, "2026-06-11": { s:"away", t:1 }, "2026-06-12": { s:"away", t:1 },
    "2026-06-20": { s:"skip", t:1 },
  });
  const ics = buildICS(b, { now: new Date(2026, 5, 30, 9, 0), months: 1, away: true, skipped: false, bills: true });
  const lines = ics.split("\r\n");
  const enc = new TextEncoder();
  const longest = Math.max(...lines.map(l => enc.encode(l).length));

  ok("uses CRLF throughout", ics.endsWith("\r\n") && !ics.replace(/\r\n/g, "").includes("\n"));
  ok("calendar block is balanced",
     (ics.match(/BEGIN:VCALENDAR/g) || []).length === 1 &&
     (ics.match(/BEGIN:VEVENT/g) || []).length === (ics.match(/END:VEVENT/g) || []).length);
  ok("no line exceeds 75 octets", longest <= 75, `longest ${longest}`);
  ok("a 3-day away run is a single event", (ics.match(/SUMMARY:No milk/g) || []).length === 1);
  ok("DTEND is exclusive", ics.includes("DTSTART;VALUE=DATE:20260610") && ics.includes("DTEND;VALUE=DATE:20260613"));
  ok("a bill event with a reminder is present", ics.includes("Milk bill") && ics.includes("BEGIN:VALARM"));
  ok("every event has a UID", (ics.match(/UID:/g) || []).length === (ics.match(/BEGIN:VEVENT/g) || []).length);
  ok("an empty book still produces a valid calendar", buildICS(book(), { now: new Date(2026, 5, 30) }).startsWith("BEGIN:VCALENDAR"));

  const folded = icsFold("SUMMARY:" + "\u20b9 milk bill \u2014 very long line ".repeat(6));
  ok("folded continuation lines stay within 75 octets",
     folded.split("\r\n").every(l => enc.encode(l).length <= 75),
     `longest ${Math.max(...folded.split("\r\n").map(l => enc.encode(l).length))}`);
  ok("unfolding restores the original text",
     folded.split("\r\n ").join("") === "SUMMARY:" + "\u20b9 milk bill \u2014 very long line ".repeat(6));
}

console.log("\nMILKBOOK client — bill text");
{
  const t = billText(book({ "2026-06-05": { s:"skip", t:1 } }), 2026, 5, "2026-06-30");
  ok("the shareable bill names the month", t.includes("June 2026"));
  ok("the shareable bill shows the vendor", t.includes("Test Dairy"));
  ok("the shareable bill shows a total", /Total:\s+\u20b9/.test(t), t.split("\n").pop());
}

console.log("\nMILKBOOK — client and server agree (same rules, two languages)");
{
  let fixture = null;
  try { fixture = JSON.parse(fs.readFileSync("/tmp/milkbook_fixture.json", "utf8")); }
  catch (e) { ok("fixture from the python tests is available", false, "run test_milkbook.py first"); }

  if (fixture) {
    ok("fixture from the python tests is available", true, `${fixture.cases.length} cases`);
    for (const c of fixture.cases) {
      // python months are 1-based, the client is 0-based
      const got = monthSummary(fixture.book, c.year, c.month - 1, c.today);
      const want = c.expected;
      const same = ["totalMl", "delivered", "skipped", "away", "off", "amountMinor"]
        .every(k => got[k] === want[k]);
      ok(`${c.year}-${String(c.month).padStart(2, "0")} as of ${c.today} matches the server`, same,
         same ? `${got.delivered}d ${got.amountMinor} minor`
              : `client ${JSON.stringify(got)} vs server ${JSON.stringify(want)}`);
    }
  }
}

console.log("\nMILKBOOK — date helpers");
{
  ok("addDays crosses a month boundary", addDays("2026-06-30", 1) === "2026-07-01");
  ok("addDays crosses a year boundary", addDays("2026-12-31", 1) === "2027-01-01");
  ok("addDays handles a leap day", addDays("2024-02-28", 1) === "2024-02-29");
  ok("addDays goes backwards", addDays("2026-03-01", -1) === "2026-02-28");
}

console.log(`\n${pass} passed, ${fail} failed\n`);
process.exit(fail ? 1 : 0);
