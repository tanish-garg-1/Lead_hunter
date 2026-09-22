---
name: lead-hunter
description: Run Lead Hunter from chat - ask the user the app's questions here, collect businesses from Google Maps with Claude in Chrome (instead of the Google Places API), then import them through the app's filters into leads.xlsx. Use when the user says "run the project", "find leads", "get me N cafes in <area>", "continue hunting <city>", or "help me decide where to look".
---

# Lead Hunter, driven from chat

The user runs the project through Claude: Claude asks the questions in chat, collects listings from Google Maps in the user's Chrome (Claude in Chrome), and the Python pipeline does the rest (dedup, chains, website/contact checks, scoring, Excel). Never run `main.py` here: it needs an interactive terminal.

All commands run from `F:\PER_PROJECTS\lead_hunter` with `.venv\Scripts\python.exe`.

## 1. Ask the questions (in chat, one message)
- Do they know the target, or want help deciding?
  - **Help deciding:** act as the strategist yourself (no Groq). Ask what they sell, price range, languages, local vs foreign clients. Suggest 2-3 options per step (country → city → areas → business type) with one-line reasons, and say neighbourhood knowledge may be dated.
- Then confirm: country, city, area(s) / sector(s), business type (short Google Maps category, e.g. "cafe"), and **how many NEW leads**.
- Check what's already saved: `.venv\Scripts\python.exe tools\leads.py status` (so you can mention batches already done in that area).

## 2. Collect from Google Maps with Claude in Chrome
Read the `anthropic-skills:chrome-browser` skill first. Load the Chrome tools in one ToolSearch, call `tabs_context_mcp`, open a new tab.

For each area (method verified live on 2026-09-22 with "cafe in Sector 29, Gurugram, India"; use `browser_batch` to chain steps):
1. Navigate to `https://www.google.com/maps/search/<type>+in+<area>,+<city>,+<country>` (spaces → `+`), wait ~4 s.
2. **Load more results with real mouse-wheel scrolls.** Use the `computer` `scroll` action over the results list (left panel, e.g. x≈220, y≈450 in the full-size frame), 10 ticks, then wait 3 s, and repeat. JavaScript `scrollBy`/`scrollIntoView` does NOT load more (stuck at 6). Three wheel scrolls took it from 6 to 20 results. Stop at about 1.5× the wanted count, or when the list says "You've reached the end of the list".
3. **List results:** `[...document.querySelectorAll('div[role="feed"] a[href*="/maps/place/"]')].map(a => ({name: a.getAttribute('aria-label'), maps_link: a.href}))`. Keep the FULL href: it carries the Google id (`!1s0x…:0x…`) and coordinates (`!3d…!4d…`). A shortened link opens the wrong place.
4. **Details per place:** open each result by clicking it in the list (`a.click()` on the matching link, or navigate to the full href), wait ~4 s, then read with `javascript_tool`:
   - address: `document.querySelector('button[data-item-id="address"]')?.getAttribute('aria-label')` (strip the leading "Address: ")
   - website: `document.querySelector('a[data-item-id="authority"]')?.href`
   - phone: `document.querySelector('[data-item-id^="phone:tel:"]')?.getAttribute('data-item-id').replace('phone:tel:', '')`
   - name, rating, reviews: the last `div[role="main"]`: its `aria-label` is the name; rating/reviews match `/(\d[.,]\d)\s*\n*\s*\(([\d,]+)\)/` on its innerText
   - closed: `/permanently closed|temporarily closed/i` on that innerText
   - category: the line under the rating (e.g. "Cafe", "Coffee shop", "Bar"). Skip listings clearly not the requested type (Google mixes bars, night clubs and restaurants into "cafe" searches). Store it as `"category"`.

   If a selector stops working, fall back to `get_page_text` and update this skill.
4. Go at a human pace (a few seconds per place; ~5-10 min for 50 places is expected and fine).
5. **Stop immediately** on a CAPTCHA, "unusual traffic" or sign-in wall. Tell the user to solve it in Chrome themselves, then continue. Never try to bypass it.
6. Save to `data/gmaps/<YYYY-MM-DD>_<type>_<area-slug>.json`: a list of `{"name", "address", "phone", "website", "rating", "reviews", "maps_link", "area", "category", "closed"}`. Keep partial results if interrupted. Collect the list first and run a `--dry-run` import, so you only open the places that are new.
7. Close the tabs you opened when done.

Tip: run `tools\leads.py import FILE ... --dry-run` after listing (before opening every place) if many might already be saved. It reports how many are new.

## 3. Import through the app
```
.venv\Scripts\python.exe tools\leads.py import data\gmaps\FILE.json --domain cafe --city "Gurugram" --country "India" --area "Sector 29" --count 50
```
Read the last `RESULT:` line:
- `saved` < `wanted` and `unused_candidates` = 0: collect more (scroll further, or ask the user for nearby areas) and import the new file.
- `excel=locked`: ask the user to close leads.xlsx, then run `tools\leads.py export`.
- Report to the user:
  - how many were saved and where (`data/leads.xlsx`, sheet per business type, new batch row with the date)
  - the skip summary (chains, no contact, already saved)
  - the best few leads

## Rules the user set
- No chains/franchises (handled by `chains.txt` + brand/branch checks).
- Never save a lead with zero contact details (handled by the import).
- No duplicates across days (handled by memory in `data/leads.db`).
- Zero budget: no paid APIs.
