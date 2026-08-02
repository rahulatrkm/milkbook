# Milkbook

A daily milk delivery register. One tap a day, an exact monthly bill, and it
works with no account and no network.

**App: https://rahulatrkm.github.io/milkbook/**

## What it does

- **One tap per day.** Days count as delivered by default, because that is what
  usually happens. You only mark the exceptions.
- **Three states.** Delivered, not delivered, or away. Tap a day to cycle it,
  hold a day to set a different quantity.
- **Away in one go.** Mark a whole date range as away before you travel.
- **Vendor's weekly off.** If your vendor never delivers on Sundays, those days
  are never counted or billed.
- **An exact bill.** Share it as text to your vendor on WhatsApp or anywhere else.
- **Phone calendar.** Download an `.ics` for Apple Calendar, Google Calendar or
  anything else, or subscribe to a live feed that updates itself.
- **Family sync.** One code, shared between your household's phones.
- **Installable.** Add to home screen on iOS or Android; opens offline.

Free. No signup, no email, no ads, no tracking, no analytics.

## How your data is stored

The book lives in your browser's local storage on your own device. That is the
real copy. The app is fully usable with the network off and with sync never
turned on.

Sync is optional. When you turn it on you get a code; entering that code on
another phone points it at the same book. The server is a **mirror**, not the
owner: if it ever loses its database, the next device to open the app uploads
everything again.

Merges are per-day, not per-book. If one person marks the 3rd as skipped on one
phone while another marks the 7th as away on a different phone, both edits
survive. Only edits to the *same day* compete, and the later one wins.

Anyone holding the sync code can read and edit the book, so treat it like a
house key. There is no account to compromise because there is no account.

## Money

Amounts are held as integer minor units (paise, cents) and quantities as
integer millilitres. The month is totalled in millilitres and multiplied by the
rate **once**, the way a vendor totals litres and multiplies — rounding each
day separately and adding them up gives a different, wrong answer.

## Running the tests

```sh
python3 test_milkbook.py      # server: billing, merge, ics, input handling
node milkbook.test.mjs        # client: same, plus a cross-check against the
                              # fixtures the python tests emit
```

The client test runs the real page script against a stubbed DOM, so the tested
code is the shipped code. The last block asserts that the JavaScript and the
Python produce identical bills, since the same rules necessarily exist in both.

## Deploying the sync service

The app works without it. To enable sync and the live calendar feed, deploy
this repo to Render as a Blueprint (`render.yaml`, free plan) and make sure
`API` at the top of the script in `index.html` matches the resulting URL.

## Licence

MIT.
