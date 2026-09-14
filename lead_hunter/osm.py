"""Free fallback when there is no Google key: OpenStreetMap (Nominatim to find the area, Overpass to list places).
Coverage is weaker than Google — many places have no phone/website and there are no ratings."""
import time

import requests

from .places import PlacesError

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "LeadHunter/1.0 (local business research)"
MIN_HALF_SIZE_DEG = 0.01  # ~1 km: small sectors often geocode to a single point

OSM_TAGS = {
    "cafe": ['"amenity"="cafe"'],
    "coffee": ['"amenity"="cafe"'],
    "restaurant": ['"amenity"="restaurant"'],
    "hospital": ['"amenity"="hospital"', '"amenity"="clinic"'],
    "clinic": ['"amenity"="clinic"', '"amenity"="doctors"'],
    "grocery": ['"shop"="supermarket"', '"shop"="convenience"', '"shop"="greengrocer"'],
    "supermarket": ['"shop"="supermarket"'],
    "gym": ['"leisure"="fitness_centre"'],
    "salon": ['"shop"="hairdresser"', '"shop"="beauty"'],
    "dentist": ['"amenity"="dentist"'],
    "pharmacy": ['"amenity"="pharmacy"'],
    "bakery": ['"shop"="bakery"'],
    "hotel": ['"tourism"="hotel"'],
    "school": ['"amenity"="school"'],
    "startup": ['"office"="company"', '"office"="it"'],
}


# Public servers with the same OpenStreetMap data; when one is overloaded the next is tried.
OVERPASS_MIRRORS = [
    OVERPASS_URL,
    "https://lz4.overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
]
RETRY_WAITS = (0, 3, 3, 3, 15, 15, 15, 15)  # seconds before each attempt; cycles through the mirrors twice
_preferred_mirror = 0  # start with whichever server answered last time


def _run_overpass(query):
    """Run an Overpass query. A busy/timed-out server can answer 200 with no elements and an error 'remark';
    that must never be mistaken for 'no businesses here', so it's retried on other mirrors and then raised."""
    global _preferred_mirror
    problems = []
    for attempt, wait in enumerate(RETRY_WAITS):
        time.sleep(wait)
        index = (_preferred_mirror + attempt) % len(OVERPASS_MIRRORS)
        url = OVERPASS_MIRRORS[index]
        host = url.split("/")[2]
        try:
            resp = requests.post(url, data={"data": query}, headers={"User-Agent": USER_AGENT}, timeout=60)
        except requests.RequestException as exc:
            problems.append(f"{host}: {type(exc).__name__}")
            continue
        if resp.status_code in (429, 502, 503, 504):
            problems.append(f"{host}: busy (HTTP {resp.status_code})")
            continue
        try:
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            problems.append(f"{host}: bad response ({type(exc).__name__})")
            continue
        remark = data.get("remark") or ""
        if "error" in remark.lower() or "timed out" in remark.lower():
            problems.append(f"{host}: {remark[:80]}")
            continue
        _preferred_mirror = index
        return data.get("elements", [])
    raise PlacesError("All OpenStreetMap servers are busy right now: " + "; ".join(problems[-4:]))


class OSMProvider:
    name = "OpenStreetMap (free)"

    def search(self, domain, area, city, country):
        yield from self._query(self._tags(domain), self._bbox(f"{area}, {city}, {country}"))

    def search_box(self, domain, box, can_split):
        """OpenStreetMap has no result cap, so a square is never 'saturated'."""
        time.sleep(1)  # be polite to the free Overpass server
        return list(self._query(self._tags(domain), box)), False

    @staticmethod
    def _tags(domain):
        base = domain.strip().lower()
        tags = next((t for key, t in OSM_TAGS.items() if key in base), None)
        if not tags:
            raise PlacesError(f"OpenStreetMap has no category for '{domain}'. Supported: {', '.join(OSM_TAGS)}")
        return tags

    @staticmethod
    def _query(tags, box):
        south, west, north, east = box
        parts = "".join(f"nwr[{tag}]({south},{west},{north},{east});" for tag in tags)
        query = f"[out:json][timeout:60];({parts});out center tags;"
        elements = _run_overpass(query)

        for el in elements:
            t = el.get("tags", {})
            if not t.get("name"):
                continue
            point = el if "lat" in el else el.get("center", {})
            address = ", ".join(
                p for p in (t.get("addr:housenumber"), t.get("addr:street"), t.get("addr:suburb"),
                            t.get("addr:city"), t.get("addr:postcode")) if p
            )
            yield {
                "place_id": f"osm:{el['type']}/{el['id']}",
                "name": t["name"],
                "address": address,
                "phone": t.get("phone") or t.get("contact:phone") or "",
                "website": t.get("website") or t.get("contact:website") or "",
                "email": t.get("email") or t.get("contact:email") or "",
                "instagram": t.get("contact:instagram") or "",
                "facebook": t.get("contact:facebook") or "",
                "rating": None,
                "reviews": None,
                "maps_link": f"https://www.openstreetmap.org/{el['type']}/{el['id']}",
                "closed": False,
                "brand": bool(t.get("brand") or t.get("brand:wikidata")),  # OSM tags chain stores with a brand
                "lat": point.get("lat"),
                "lng": point.get("lon"),
                "postcode": t.get("addr:postcode", ""),
                "suburb": t.get("addr:suburb") or t.get("addr:district") or "",
            }

    @staticmethod
    def _bbox(place):
        try:
            resp = requests.get(
                NOMINATIM_URL, params={"q": place, "format": "json", "limit": 1},
                headers={"User-Agent": USER_AGENT}, timeout=20,
            )
            resp.raise_for_status()
            results = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise PlacesError(f"Couldn't look up '{place}' on OpenStreetMap: {exc}") from exc
        if not results:
            raise PlacesError(f"OpenStreetMap couldn't find '{place}'. Try a different spelling.")
        south, north, west, east = (float(v) for v in results[0]["boundingbox"])
        lat, lng = (south + north) / 2, (west + east) / 2
        half_lat = max((north - south) / 2, MIN_HALF_SIZE_DEG)
        half_lng = max((east - west) / 2, MIN_HALF_SIZE_DEG)
        return lat - half_lat, lng - half_lng, lat + half_lat, lng + half_lng
