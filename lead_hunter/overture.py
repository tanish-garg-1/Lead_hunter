"""Overture Maps places (free, no account, no card), read by map area straight from Overture's public cloud
storage with DuckDB. Most records come from Meta business pages, so phones and social links are common.
Downloaded map tiles are cached under data/overture/ so an area is only fetched once per monthly release."""
import hashlib
import json
import math
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from . import enrich, geo
from .places import PlacesError

BUCKET = "s3://overturemaps-us-west-2/release"
RELEASE_CHECK_DAYS = 7
TILE_DEG = 0.05        # ~5 km cache tiles
MIN_CONFIDENCE = 0.5   # "balanced": Overture's own score for how likely the place exists

# Overture category names (primary or alternate) per business type, as regex alternatives.
CATEGORY_GROUPS = {
    "cafe": "cafe|coffee_shop|coffee_roastery|tea_room|bubble_tea_shop",
    "coffee": "cafe|coffee_shop|coffee_roastery",
    "tea": "tea_room|bubble_tea_shop",
    "bakery": "bakery|patisserie|cake_shop",
    "restaurant": "[a-z_]*restaurant",
    "hospital": "hospital|medical_center|emergency_room",
    "clinic": "[a-z_]*clinic|medical_center|doctor",
    "dentist": "dentist|[a-z_]*dental[a-z_]*|orthodontist",
    "grocery": "grocery_store|supermarket|convenience_store|[a-z_]*grocery[a-z_]*",
    "supermarket": "supermarket|grocery_store",
    "gym": "gym|fitness_center|[a-z_]*fitness[a-z_]*|yoga_studio",
    "salon": "[a-z_]*salon|barber|spa",
    "pharmacy": "pharmacy|drugstore",
    "hotel": "hotel|motel|guest_house|bed_and_breakfast|hostel",
    "school": "[a-z_]*school",
    "startup": "software_development|[a-z_]*software[a-z_]*|information_technology_company",
}

QUERY = """
SELECT id, names.primary AS name, websites, emails, socials, phones, brand.names.primary AS brand,
       addresses, operating_status, (bbox.xmin + bbox.xmax) / 2 AS lng, (bbox.ymin + bbox.ymax) / 2 AS lat
FROM read_parquet('{path}', hive_partitioning = 1)
WHERE bbox.xmin >= ? AND bbox.xmax <= ? AND bbox.ymin >= ? AND bbox.ymax <= ?
  AND confidence >= ?
  AND coalesce(operating_status, 'open') <> 'permanently_closed'
  AND regexp_matches(coalesce(categories.primary, '') || '|' || coalesce(array_to_string(categories.alternate, '|'), ''), ?)
"""


def category_regex(domain):
    base = domain.strip().lower()
    group = next((g for key, g in CATEGORY_GROUPS.items() if key in base), None)
    if group is None:  # unknown type: match categories containing its words, e.g. "florist"
        words = [re.escape(w.rstrip("s")) for w in re.findall(r"[a-z]+", base) if len(w) > 2]
        group = "|".join(f"[a-z_]*{w}[a-z_]*" for w in words) or re.escape(base)
    return rf"(^|\|)({group})(\||$)"


def tiles_for(box):
    south, west, north, east = box
    eps = 1e-9
    rows = range(math.floor(south / TILE_DEG + eps), math.ceil(north / TILE_DEG - eps))
    cols = range(math.floor(west / TILE_DEG + eps), math.ceil(east / TILE_DEG - eps))
    return [(r, c) for r in rows for c in cols]


def tile_box(tile):
    r, c = tile
    return r * TILE_DEG, c * TILE_DEG, (r + 1) * TILE_DEG, (c + 1) * TILE_DEG


def normalize_row(row):
    address = (row.get("addresses") or [None])[0] or {}
    links = [u for u in (row.get("socials") or []) + (row.get("websites") or []) if u]
    socials = {}
    for url in links:
        host = enrich._host(url)
        for key, hosts in enrich.SOCIAL_HOSTS.items():
            if key not in socials and enrich._host_matches(host, hosts):
                socials[key] = url
    website = next(
        (w for w in row.get("websites") or [] if w and not enrich._host_matches(enrich._host(w), enrich.SOCIAL_ONLY_HOSTS)),
        "",
    )
    name = row.get("name") or ""
    full_address = ", ".join(p for p in (address.get("freeform"), address.get("postcode"), address.get("locality")) if p)
    search = quote_plus(", ".join(p for p in (name, full_address) if p))
    return {
        "place_id": f"ovt:{row['id']}",
        "name": name,
        "address": full_address,
        "phone": next((p for p in row.get("phones") or [] if p), ""),
        "website": website,
        "email": ", ".join([e for e in row.get("emails") or [] if e][:3]),
        "instagram": socials.get("instagram", ""),
        "facebook": socials.get("facebook", ""),
        "whatsapp": socials.get("whatsapp", ""),
        "linkedin": socials.get("linkedin", ""),
        "rating": None,
        "reviews": None,
        "maps_link": f"https://www.google.com/maps/search/?api=1&query={search}",  # to check the listing by hand
        "closed": row.get("operating_status") == "permanently_closed",
        "brand": bool(row.get("brand")),
        "lat": row.get("lat"),
        "lng": row.get("lng"),
        "postcode": address.get("postcode") or "",
        "suburb": "",  # Overture's locality is the city; the hunt looks the neighbourhood up
    }


class OvertureProvider:
    name = "Overture Maps (free)"

    def __init__(self, cache_dir, log=None):
        self.cache_dir = Path(cache_dir)
        self.log = log or (lambda *_: None)
        self._db = None

    def search(self, domain, area, city, country):
        try:
            box = geo.area_bbox(f"{area}, {city}, {country}")
        except geo.GeoError as exc:
            raise PlacesError(str(exc)) from exc
        yield from self.search_box(domain, box, False)[0]

    def search_box(self, domain, box, can_split):
        """Everything of this type inside the square. Overture has no result cap, so never 'saturated'."""
        regex = category_regex(domain)
        south, west, north, east = box
        found = {}
        for tile in tiles_for(box):
            for place in self._tile(regex, tile):
                if place["lat"] is not None and south <= place["lat"] < north and west <= place["lng"] < east:
                    found[place["place_id"]] = place
        return [dict(p) for p in found.values()], False

    def _tile(self, regex, tile):
        folder = self.cache_dir / self._release() / hashlib.md5(regex.encode()).hexdigest()[:10]
        path = folder / f"{tile[0]}_{tile[1]}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))

        south, west, north, east = tile_box(tile)
        self.log(f"  [dim]downloading Overture places for a ~5 km map tile (first time only, can take a minute)...[/]")
        con = self._connect()
        glob = f"{BUCKET}/{self._release()}/theme=places/type=place/*"
        try:
            cur = con.execute(QUERY.format(path=glob), [west, east, south, north, MIN_CONFIDENCE, regex])
            columns = [d[0] for d in cur.description]
            places = [normalize_row(dict(zip(columns, row))) for row in cur.fetchall()]
        except Exception as exc:  # duckdb raises its own error types (network, S3)
            raise PlacesError(f"Overture download failed: {exc}") from exc
        folder.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(places, ensure_ascii=False), encoding="utf-8")
        return places

    def _connect(self):
        if self._db is None:
            try:
                import duckdb
            except ImportError as exc:
                raise PlacesError("Overture needs duckdb: run .venv\\Scripts\\python.exe -m pip install duckdb") from exc
            try:
                self._db = duckdb.connect()
                self._db.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
            except Exception as exc:
                self._db = None
                raise PlacesError(f"Couldn't set up Overture access: {exc}") from exc
        return self._db

    def _release(self):
        """Latest monthly release name, checked at most once a week."""
        marker = self.cache_dir / "release.json"
        cached = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else None
        if cached and time.time() - cached["checked"] < RELEASE_CHECK_DAYS * 86400:
            return cached["release"]
        try:
            files = self._connect().execute(f"SELECT file FROM glob('{BUCKET}/*/theme=places/type=place/*')").fetchall()
            releases = sorted({f[0].split("/release/")[1].split("/")[0] for f in files})
        except Exception as exc:
            if cached:
                return cached["release"]
            raise PlacesError(f"Couldn't reach Overture: {exc}") from exc
        if not releases:
            raise PlacesError("Couldn't find an Overture release.")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"release": releases[-1], "checked": time.time()}), encoding="utf-8")
        return releases[-1]
