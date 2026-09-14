"""City hunt: cover a whole city map square by map square, busiest (central) squares first.
A square whose results hit the cap is split into 4 until nothing is hidden. Everything found goes into a
queue grouped by neighbourhood cluster (postcode / suburb), so each day's leads come in tight clusters and
tomorrow picks up where today stopped without spending searches again."""
import json
from collections import Counter
from datetime import datetime

from . import geo, store
from .places import PlacesError, QuotaExceeded

START_CELL_KM = 4.0
MIN_CELL_KM = 0.25
NAME_BIN_DEG = 0.01  # neighbourhood names are looked up once per ~1 km zone
FOCUS_RADIUS_KM = 2.0
POSITIVE_STATUSES = ("Replied", "Meeting", "Won")


def get_hunt(conn, hunt_id):
    return conn.execute("SELECT * FROM hunts WHERE id = ?", (hunt_id,)).fetchone()


def list_hunts(conn):
    return conn.execute("SELECT * FROM hunts ORDER BY id DESC").fetchall()


def find_hunt(conn, domain, city, country):
    return conn.execute(
        "SELECT * FROM hunts WHERE lower(domain) = lower(?) AND lower(city) = lower(?) AND lower(country) = lower(?)",
        (domain.strip(), city.strip(), country.strip()),
    ).fetchone()


def add_cell(conn, hunt_id, box, depth, center, polygons, boost=0):
    status = "pending" if geo.box_touches_city(box, polygons) else "outside"
    dist = geo.distance_km(*center, *geo.box_center(box))
    conn.execute(
        "INSERT INTO hunt_cells (hunt_id, south, west, north, east, depth, status, dist_km, boost) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (hunt_id, *box, depth, status, dist, boost),
    )


def create_hunt(conn, domain, city, country, city_geo):
    cur = conn.execute(
        "INSERT INTO hunts (domain, city, country, center_lat, center_lng, polygons, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (domain.strip(), city.strip(), country.strip(), city_geo["lat"], city_geo["lng"],
         json.dumps(city_geo["polygons"]), datetime.now().isoformat(timespec="seconds")),
    )
    hunt_id = cur.lastrowid
    center = (city_geo["lat"], city_geo["lng"])
    for box in geo.initial_grid(city_geo["bbox"], START_CELL_KM):
        add_cell(conn, hunt_id, box, 0, center, city_geo["polygons"])
    conn.commit()
    return hunt_id


def cluster_of(place, cell_id, namer):
    """Neighbourhood cluster for a place: its own suburb, else the looked-up neighbourhood, else postcode."""
    suburb = (place.get("suburb") or "").strip()
    if not suburb and place.get("lat") is not None:
        suburb = namer(place["lat"], place["lng"])
    postcode = (place.get("postcode") or "").strip()
    key = suburb or postcode or f"square-{cell_id}"
    return key, suburb or postcode or f"Map square {cell_id}"


def progress(conn, hunt_id):
    done_area = pending_area = 0.0
    done_cells = pending_cells = 0
    for c in conn.execute(
        "SELECT south, west, north, east, status FROM hunt_cells WHERE hunt_id = ? AND status IN ('done', 'pending')",
        (hunt_id,),
    ):
        area = geo.box_area_km2((c["south"], c["west"], c["north"], c["east"]))
        if c["status"] == "done":
            done_area += area
            done_cells += 1
        else:
            pending_area += area
            pending_cells += 1
    total = done_area + pending_area
    return {
        "covered_pct": 100 * done_area / total if total else 100.0,
        "done_cells": done_cells,
        "pending_cells": pending_cells,
        "total_km2": total,
        "leads": conn.execute("SELECT COUNT(*) FROM leads WHERE hunt_id = ?", (hunt_id,)).fetchone()[0],
        "queued": conn.execute("SELECT COUNT(*) FROM hunt_queue WHERE hunt_id = ?", (hunt_id,)).fetchone()[0],
    }


def clusters(conn, hunt_id):
    """Neighbourhoods seen so far, best responses first."""
    data = {}
    for r in conn.execute("SELECT cluster, area, status FROM leads WHERE hunt_id = ?", (hunt_id,)):
        c = data.setdefault(r["cluster"], {"cluster": r["cluster"], "name": r["area"], "leads": 0, "positive": 0, "queued": 0})
        c["leads"] += 1
        c["positive"] += r["status"] in POSITIVE_STATUSES
    for r in conn.execute(
        "SELECT cluster, MAX(cluster_name) AS name, COUNT(*) AS n FROM hunt_queue WHERE hunt_id = ? GROUP BY cluster",
        (hunt_id,),
    ):
        c = data.setdefault(r["cluster"], {"cluster": r["cluster"], "name": r["name"], "leads": 0, "positive": 0, "queued": 0})
        c["queued"] = r["n"]
    return sorted(data.values(), key=lambda c: (-c["positive"], -c["leads"], -c["queued"]))


def set_focus(conn, hunt_id, cluster):
    """Put one neighbourhood first: its queued places are used first and nearby map squares are searched next."""
    conn.execute("UPDATE hunts SET focus_cluster = ? WHERE id = ?", (cluster, hunt_id))
    conn.execute("UPDATE hunt_cells SET boost = 0 WHERE hunt_id = ?", (hunt_id,))
    if cluster:
        points = [
            (r["lat"], r["lng"]) for r in conn.execute(
                "SELECT lat, lng FROM leads WHERE hunt_id = ? AND cluster = ? AND lat IS NOT NULL", (hunt_id, cluster))
        ]
        for r in conn.execute("SELECT data FROM hunt_queue WHERE hunt_id = ? AND cluster = ?", (hunt_id, cluster)):
            place = json.loads(r["data"])
            if place.get("lat") is not None:
                points.append((place["lat"], place["lng"]))
        if points:
            lat = sum(p[0] for p in points) / len(points)
            lng = sum(p[1] for p in points) / len(points)
            for cell in conn.execute(
                "SELECT id, south, west, north, east FROM hunt_cells WHERE hunt_id = ? AND status = 'pending'", (hunt_id,)
            ).fetchall():
                box = (cell["south"], cell["west"], cell["north"], cell["east"])
                reach = FOCUS_RADIUS_KM + max(geo.box_size_km(box)) / 2
                if geo.distance_km(lat, lng, *geo.box_center(box)) <= reach:
                    conn.execute("UPDATE hunt_cells SET boost = 1 WHERE id = ?", (cell["id"],))
    conn.commit()


class Hunt:
    def __init__(self, conn, hunt_id, provider, detector, log=print, namer=geo.neighbourhood_name):
        self.namer = namer
        self.name_lookups = 0
        self.conn = conn
        self.id = hunt_id
        self.provider = provider
        self.detector = detector
        self.log = log
        self.row = get_hunt(conn, hunt_id)
        self.center = (self.row["center_lat"], self.row["center_lng"])
        self.polygons = json.loads(self.row["polygons"] or "[]")
        self.skipped = Counter()
        self.taken = set()
        self.claimed_phones, self.claimed_names = set(), set()
        self.stop_reason = ""
        # Everything seen in earlier sessions counts towards spotting chains with many branches in this city.
        for r in conn.execute("SELECT place_id, keys FROM hunt_seen WHERE hunt_id = ?", (hunt_id,)):
            for key in filter(None, (r["keys"] or "").split("\n")):
                detector.places_by_key[key].add(r["place_id"])

    def queue_size(self):
        total = self.conn.execute("SELECT COUNT(*) FROM hunt_queue WHERE hunt_id = ?", (self.id,)).fetchone()[0]
        return total - len(self.taken)

    def is_complete(self):
        pending = self.conn.execute(
            "SELECT 1 FROM hunt_cells WHERE hunt_id = ? AND status = 'pending' LIMIT 1", (self.id,)
        ).fetchone()
        return pending is None and self.queue_size() <= 0

    def fill_queue(self, target_size):
        """Search more map squares until the queue holds at least target_size places (or the city is done)."""
        while not self.stop_reason and self.queue_size() < target_size:
            cell = self.conn.execute(
                "SELECT * FROM hunt_cells WHERE hunt_id = ? AND status = 'pending' ORDER BY boost DESC, dist_km, id LIMIT 1",
                (self.id,),
            ).fetchone()
            if cell is None:
                return
            try:
                self._process_cell(cell)
            except QuotaExceeded as exc:
                self.stop_reason = str(exc)
            except PlacesError as exc:
                self.stop_reason = f"{exc} (progress is saved; try again later)"

    def _process_cell(self, cell):
        box = (cell["south"], cell["west"], cell["north"], cell["east"])
        can_split = min(geo.box_size_km(box)) >= 2 * MIN_CELL_KM
        places, saturated = self.provider.search_box(self.row["domain"], box, can_split)
        page_keys = self.detector.observe(places)

        queued = 0
        for place in places:
            keys = "\n".join(self.detector.keys_for(place))
            is_new = self.conn.execute(
                "INSERT OR IGNORE INTO hunt_seen (hunt_id, place_id, keys) VALUES (?, ?, ?)",
                (self.id, place["place_id"], keys),
            ).rowcount
            if not is_new:
                continue  # already handled via an overlapping (parent) square
            reason = self._queue_skip_reason(place, page_keys)
            if reason:
                self.skipped[reason] += 1
                continue
            cluster, name = cluster_of(place, cell["id"], self._neighbourhood)
            self.conn.execute(
                "INSERT OR IGNORE INTO hunt_queue (hunt_id, place_id, cluster, cluster_name, data) VALUES (?, ?, ?, ?, ?)",
                (self.id, place["place_id"], cluster, name, json.dumps(place)),
            )
            queued += 1

        if saturated:
            for child in geo.split_box(box):
                add_cell(self.conn, self.id, child, cell["depth"] + 1, self.center, self.polygons, cell["boost"])
            status = "split"
        else:
            status = "done"
        self.conn.execute("UPDATE hunt_cells SET status = ?, found = ? WHERE id = ?", (status, len(places), cell["id"]))
        self.conn.commit()
        note = "busy, splitting into 4 smaller squares" if saturated else "done"
        self.log(f"  [dim]map square {cell['id']}: {len(places)} found, {queued} new ({note})[/]")

    def _neighbourhood(self, lat, lng):
        zone = f"{round(lat / NAME_BIN_DEG)}:{round(lng / NAME_BIN_DEG)}"
        row = self.conn.execute("SELECT name FROM neighbourhood_names WHERE zone = ?", (zone,)).fetchone()
        if row:
            return row["name"]
        if self.name_lookups == 0:
            self.log("  [dim]looking up neighbourhood names (free, about 1 per second, only once per area)...[/]")
        self.name_lookups += 1
        name = self.namer(lat, lng)
        if name:
            self.conn.execute("INSERT OR REPLACE INTO neighbourhood_names (zone, name) VALUES (?, ?)", (zone, name))
        return name

    def _queue_skip_reason(self, place, page_keys):
        if place.get("closed"):
            return "closed"
        if place.get("lat") is not None and not geo.inside_city(self.polygons, place["lng"], place["lat"]):
            return "outside the city"
        return self.recheck(place, page_keys)

    def recheck(self, place, page_keys=None, claim=False):
        """Checks worth repeating right before use: the list or chain knowledge may have changed since queueing."""
        phone = store.normalize_phone(place.get("phone"))
        name_key = store.make_name_key(place.get("name"), place.get("address"))
        if (store.is_duplicate(self.conn, place["place_id"], place.get("phone"), place.get("name"), place.get("address"))
                or (phone and phone in self.claimed_phones) or (name_key and name_key in self.claimed_names)):
            return "already in your list"
        if store.is_skipped(self.conn, place["place_id"]):
            return "no contact details (checked before)"
        reason = self.detector.reason(place, page_keys or {})
        if not reason and claim:
            self.claimed_phones.add(phone)
            self.claimed_names.add(name_key)
        return reason

    def take(self, n):
        """Up to n queued places, whole neighbourhood clusters at a time: focus cluster first, then biggest."""
        order = self.conn.execute(
            "SELECT cluster, COUNT(*) AS n FROM hunt_queue WHERE hunt_id = ? GROUP BY cluster "
            "ORDER BY (cluster = ?) DESC, n DESC, MIN(rowid)",
            (self.id, self.row["focus_cluster"] or ""),
        ).fetchall()
        picked = []
        for group in order:
            rows = self.conn.execute(
                "SELECT place_id, cluster, cluster_name, data FROM hunt_queue WHERE hunt_id = ? AND cluster = ? ORDER BY rowid",
                (self.id, group["cluster"]),
            ).fetchall()
            for row in rows:
                if row["place_id"] in self.taken:
                    continue
                place = json.loads(row["data"])
                place.update(area=row["cluster_name"], cluster=row["cluster"])
                picked.append(place)
                self.taken.add(row["place_id"])
                if len(picked) >= n:
                    return picked
        return picked

    def done(self, place_ids):
        """Remove places from the queue once they're saved or rejected."""
        self.conn.executemany(
            "DELETE FROM hunt_queue WHERE hunt_id = ? AND place_id = ?", [(self.id, pid) for pid in place_ids]
        )
        self.conn.commit()
        self.taken.difference_update(place_ids)
