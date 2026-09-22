# Lead Hunter

Finds independent local businesses (cafes, clinics, shops…) for freelance website work and saves them to `data/leads.xlsx`.

- When the user asks to run the project or find leads, follow the `lead-hunter` skill (`.claude/skills/lead-hunter/SKILL.md`): ask the questions in chat, collect from Google Maps with Claude in Chrome, import with `tools/leads.py`. Don't run `main.py` from here; it's an interactive terminal app for the user.
- No Google Places API key: zero budget. Free sources otherwise: Overture (default) and OpenStreetMap.
- Always use `.venv\Scripts\python.exe`. Offline tests: `.venv\Scripts\python.exe tests\test_offline.py`.
