# Lead Hunter

A terminal app that finds local businesses (cafes, clinics, grocery stores, startups…) that need a website, checks their online presence and saves them in one growing Excel file that doubles as your CRM.

## Setup (one time)

Everything runs inside the project's virtual environment (`.venv`). If it isn't created yet:

```bash
python -m venv .venv
```

```bash
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Fill in `.env` (copy it from `.env.example` if it's missing):

### Groq key (free, no card)
1. Go to https://console.groq.com/keys, sign in, and create a key.
2. Paste it as `GROQ_API_KEY`. This enables "help me decide" and owner-name lookup.

### Google Places key (free up to 1,000 requests/month)
1. Go to https://console.cloud.google.com and create a project.
2. Open **APIs & Services → Library**, find **Places API (New)** and enable it. Google will ask for a billing account (card) to do this.
3. Open **APIs & Services → Credentials → Create credentials → API key**. Restrict the key to *Places API (New)*.
4. **Protect yourself from charges:** open **APIs & Services → Places API (New) → Quotas** and set the requests-per-day limit to something like `40`.
5. Paste the key as `GOOGLE_PLACES_API_KEY`.

The app also counts its own requests and stops at `MONTHLY_REQUEST_LIMIT` (default 900). One request returns up to 20 businesses, so 50 leads a day is about 3–6 requests.

No Google key? The app falls back to free OpenStreetMap data, which has fewer phones and websites and no ratings.

## Run

```bash
.venv\Scripts\python.exe main.py
```

(Or activate the venv first with `.venv\Scripts\Activate.ps1`, then `python main.py`.)

1. **City hunt:** cover a whole city cluster by cluster, a bit more every day (see below).
2. **Quick search:** enter country, city, specific areas and business type, then how many new leads you want.
3. **Help me decide:** chat with the AI about what you sell and who you want to work with. It suggests a target; then choose a city hunt or a quick search.
4. **Sync statuses from Excel:** copies your Status / Last Contacted / Notes edits into memory.
5. **Usage & stats:** Google requests used this month, plus leads per sheet and status.

## Where your data is

- `data/leads.xlsx`: one sheet per business type plus a Summary sheet. Every run adds a coloured **Batch** row (number, date, area, count) with the new leads underneath, best leads first. Every lead has a **Date Added**.
- `data/leads.db`: the memory. Any business already saved (same Google ID, same phone, or same name + postcode) is never added again.
- Edit **Status**, **Last Contacted** and **Notes** in Excel. Don't delete the hidden *Place ID* column; it's how edits are matched.
- If Excel is open while the app saves, it asks you to close it. Leads are already safe in memory.

## City hunt: cover a whole city without missing anyone

Google shows at most 60 results per search, so "cafes in Berlin" can never list every café. A city hunt gets around that:

1. **Map squares:** the app gets the city's outline from OpenStreetMap (free) and splits it into ~4 km squares. Squares outside the city are ignored.
2. **Busiest first:** squares closest to the centre are searched first.
3. **Split when busy:** if a square has more results than one page, it's split into 4 smaller squares, again and again, until every business is visible.
4. **Neighbourhood clusters:** everything found goes into a queue grouped by postcode / neighbourhood. Leads are handed out one whole cluster at a time, so a day's batch is geographically tight.
5. **Resume anytime:** ask for 50 today and it stops at 50. Tomorrow it continues from the queue (no new searches needed) and then the next squares. Progress is saved in `data/leads.db`.
6. **Focus:** when a neighbourhood responds well (set Status to Replied / Meeting / Won in Excel), choose *Focus on a neighbourhood next* to hunt there and in nearby squares first.

**Cost:** a big city like Berlin takes roughly 250–400 Google searches in total, spread over the days you use it (free cap: 1,000/month). Each "get more leads" only searches as many squares as it needs.

## What gets skipped

- **Chains & franchises** (Starbucks, McDonald's, Tesco, Apollo…): they have no single owner to buy from you. Caught three ways:
  - names in `chains.txt` (add your own, one per line; matched at the start of the business name)
  - the same name or website at several locations in your search (catches local chains too)
  - OpenStreetMap brand tags
- **No way to contact them:** a lead is saved only if it has at least one of phone, email, Instagram, Facebook, WhatsApp or LinkedIn. These don't count toward your number, so the app keeps searching. They're remembered for 90 days so their sites aren't re-checked every run.
- Businesses already in your list, and closed businesses.

A summary line shows how many were skipped and why.

## Lead score

| Signal | Points |
|---|---|
| No website / Instagram-only / site down | +40 |
| Website with 2+ problems (no HTTPS, not mobile-friendly, outdated, slow, cheap builder) | +25 (1 problem: +10) |
| Rating ≥ 4.0 with 50+ reviews (busy, can pay) | +20 |
| Has Instagram (can DM them) | +10 |
| Email or phone found | +10 |

Hot ≥ 60 · Warm 35–59 · Cold < 35

## Tests

```bash
.venv\Scripts\python.exe tests\test_offline.py
```

## Be a good citizen
Only public business info is collected. Website checks are slow on purpose and respect robots.txt. When you reach out, keep it personal and low volume, and stop if someone asks you to.
