"""SQLite memory: every lead ever found, the batches they came in, and API usage counters."""
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

LEAD_COLUMNS = [
    "place_id", "domain", "batch_id", "date_added", "name", "owner_name", "phone", "email",
    "has_website", "website", "website_issues", "instagram", "facebook", "whatsapp", "linkedin",
    "address", "area", "city", "country", "rating", "reviews", "maps_link", "score", "priority",
    "status", "last_contacted", "notes", "phone_norm", "name_key", "hunt_id", "lat", "lng", "cluster",
]

SKIP_RECHECK_DAYS = 90

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    batch_no INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    target TEXT NOT NULL,
    lead_count INTEGER NOT NULL DEFAULT 0,
    exported INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS leads (
    place_id TEXT PRIMARY KEY,
    domain TEXT NOT NULL,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    date_added TEXT, name TEXT, owner_name TEXT, phone TEXT, email TEXT,
    has_website TEXT, website TEXT, website_issues TEXT, instagram TEXT, facebook TEXT,
    whatsapp TEXT, linkedin TEXT, address TEXT, area TEXT, city TEXT, country TEXT,
    rating REAL, reviews INTEGER, maps_link TEXT, score INTEGER, priority TEXT,
    status TEXT DEFAULT 'New', last_contacted TEXT, notes TEXT,
    phone_norm TEXT, name_key TEXT,
    hunt_id INTEGER, lat REAL, lng REAL, cluster TEXT
);
CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone_norm);
CREATE INDEX IF NOT EXISTS idx_leads_name_key ON leads(name_key);
CREATE TABLE IF NOT EXISTS hunts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    center_lat REAL NOT NULL,
    center_lng REAL NOT NULL,
    polygons TEXT,
    focus_cluster TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hunt_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hunt_id INTEGER NOT NULL REFERENCES hunts(id),
    south REAL NOT NULL, west REAL NOT NULL, north REAL NOT NULL, east REAL NOT NULL,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL,          -- pending / done / split / outside
    dist_km REAL NOT NULL,         -- distance from the city centre: central squares first
    boost INTEGER NOT NULL DEFAULT 0,
    found INTEGER
);
CREATE INDEX IF NOT EXISTS idx_cells_hunt_status ON hunt_cells(hunt_id, status);
CREATE TABLE IF NOT EXISTS hunt_queue (
    hunt_id INTEGER NOT NULL,
    place_id TEXT NOT NULL,
    cluster TEXT NOT NULL,
    cluster_name TEXT,
    data TEXT NOT NULL,
    PRIMARY KEY (hunt_id, place_id)
);
CREATE TABLE IF NOT EXISTS neighbourhood_names (
    zone TEXT PRIMARY KEY,
    name TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hunt_seen (
    hunt_id INTEGER NOT NULL,
    place_id TEXT NOT NULL,
    keys TEXT,
    PRIMARY KEY (hunt_id, place_id)
);
CREATE TABLE IF NOT EXISTS skipped (
    place_id TEXT PRIMARY KEY,
    name TEXT,
    reason TEXT NOT NULL,
    skipped_on TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS api_usage (
    month TEXT NOT NULL,
    api TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, api)
);
"""


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """Add columns introduced after a database was first created."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
    for column, kind in (("hunt_id", "INTEGER"), ("lat", "REAL"), ("lng", "REAL"), ("cluster", "TEXT")):
        if column not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {kind}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_hunt ON leads(hunt_id)")
    conn.commit()


def normalize_phone(phone):
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 7 else ""


def make_name_key(name, address):
    """Business name + postcode (or start of address), used to catch the same place listed twice."""
    clean_name = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not clean_name:
        return ""
    postcode = re.search(r"\b\d{5,6}\b", address or "")
    location = postcode.group(0) if postcode else re.sub(r"[^a-z0-9]", "", (address or "").lower())[:20]
    return f"{clean_name}|{location}"


def is_duplicate(conn, place_id, phone, name, address):
    if conn.execute("SELECT 1 FROM leads WHERE place_id = ?", (place_id,)).fetchone():
        return True
    phone_norm = normalize_phone(phone)
    if phone_norm and conn.execute("SELECT 1 FROM leads WHERE phone_norm = ?", (phone_norm,)).fetchone():
        return True
    name_key = make_name_key(name, address)
    return bool(name_key and conn.execute("SELECT 1 FROM leads WHERE name_key = ?", (name_key,)).fetchone())


def add_skipped(conn, place_id, name, reason):
    """Remember a business that had no contact details, so its website isn't checked again every run."""
    conn.execute(
        "INSERT OR REPLACE INTO skipped (place_id, name, reason, skipped_on) VALUES (?, ?, ?, ?)",
        (place_id, name, reason, date.today().isoformat()),
    )
    conn.commit()


def is_skipped(conn, place_id):
    """True if skipped within the last SKIP_RECHECK_DAYS (they may add an Instagram or email later)."""
    row = conn.execute("SELECT skipped_on FROM skipped WHERE place_id = ?", (place_id,)).fetchone()
    if row is None:
        return False
    if (date.today() - date.fromisoformat(row["skipped_on"])).days > SKIP_RECHECK_DAYS:
        conn.execute("DELETE FROM skipped WHERE place_id = ?", (place_id,))
        conn.commit()
        return False
    return True


def create_batch(conn, domain, target):
    row = conn.execute("SELECT COALESCE(MAX(batch_no), 0) FROM batches WHERE domain = ?", (domain,)).fetchone()
    batch_no = row[0] + 1
    cur = conn.execute(
        "INSERT INTO batches (domain, batch_no, created_at, target) VALUES (?, ?, ?, ?)",
        (domain, batch_no, datetime.now().isoformat(timespec="seconds"), target),
    )
    conn.commit()
    return cur.lastrowid, batch_no


def insert_lead(conn, lead):
    values = dict(lead)
    values["phone_norm"] = normalize_phone(lead.get("phone"))
    values["name_key"] = make_name_key(lead.get("name"), lead.get("address"))
    values["status"] = lead.get("status") or "New"
    placeholders = ", ".join(f":{c}" for c in LEAD_COLUMNS)
    conn.execute(
        f"INSERT OR IGNORE INTO leads ({', '.join(LEAD_COLUMNS)}) VALUES ({placeholders})",
        {c: values.get(c) for c in LEAD_COLUMNS},
    )


def finalize_batch(conn, batch_id):
    """Record the lead count; an empty batch is deleted so batch numbers stay continuous."""
    count = conn.execute("SELECT COUNT(*) FROM leads WHERE batch_id = ?", (batch_id,)).fetchone()[0]
    if count == 0:
        conn.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
    else:
        conn.execute("UPDATE batches SET lead_count = ? WHERE id = ?", (count, batch_id))
    conn.commit()
    return count


def pending_batches(conn):
    return conn.execute("SELECT * FROM batches WHERE exported = 0 AND lead_count > 0 ORDER BY id").fetchall()


def mark_exported(conn, batch_id):
    conn.execute("UPDATE batches SET exported = 1 WHERE id = ?", (batch_id,))
    conn.commit()


def leads_for_batch(conn, batch_id):
    return conn.execute(
        "SELECT * FROM leads WHERE batch_id = ? ORDER BY score DESC, name", (batch_id,)
    ).fetchall()


def all_batches(conn):
    return conn.execute("SELECT * FROM batches WHERE lead_count > 0 ORDER BY domain, batch_no").fetchall()


def update_tracking(conn, place_id, status, last_contacted, notes):
    """Copy the columns you edit by hand in Excel back into memory. Returns True if anything changed."""
    row = conn.execute(
        "SELECT status, last_contacted, notes FROM leads WHERE place_id = ?", (place_id,)
    ).fetchone()
    if row is None or (row["status"], row["last_contacted"], row["notes"]) == (status, last_contacted, notes):
        return False
    conn.execute(
        "UPDATE leads SET status = ?, last_contacted = ?, notes = ? WHERE place_id = ?",
        (status, last_contacted, notes, place_id),
    )
    return True


def summary_rows(conn):
    """Per sheet: total leads, count per status and number of Hot leads."""
    summary = {}
    for row in conn.execute("SELECT domain, status, priority, COUNT(*) AS n FROM leads GROUP BY domain, status, priority"):
        entry = summary.setdefault(row["domain"], {"total": 0, "hot": 0, "statuses": {}})
        entry["total"] += row["n"]
        entry["statuses"][row["status"] or "New"] = entry["statuses"].get(row["status"] or "New", 0) + row["n"]
        if row["priority"] == "Hot":
            entry["hot"] += row["n"]
    return dict(sorted(summary.items()))


def _month():
    return date.today().strftime("%Y-%m")


def usage_get(conn, api):
    row = conn.execute("SELECT requests FROM api_usage WHERE month = ? AND api = ?", (_month(), api)).fetchone()
    return row[0] if row else 0


def usage_add(conn, api, n=1):
    conn.execute(
        "INSERT INTO api_usage (month, api, requests) VALUES (?, ?, ?) "
        "ON CONFLICT(month, api) DO UPDATE SET requests = requests + excluded.requests",
        (_month(), api, n),
    )
    conn.commit()
