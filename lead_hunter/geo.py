"""City outline lookup (OpenStreetMap Nominatim, free) and map-square helpers for city hunts.
Boxes are (south, west, north, east) in degrees; polygon rings are lists of (lng, lat)."""
import math
import time

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
NEIGHBOURHOOD_KEYS = ("suburb", "city_district", "quarter", "borough", "neighbourhood", "town", "village")
USER_AGENT = "LeadHunter/1.0 (local business research)"
KM_PER_DEG = 111.32
POINT_CITY_HALF_DEG = 0.05  # a city without an outline gets a ~11 km box around its centre


class GeoError(Exception):
    pass


def locate_city(city, country):
    params = {
        "city": city, "country": country, "format": "json", "limit": 1,
        "polygon_geojson": 1, "polygon_threshold": 0.0005,
    }
    try:
        resp = requests.get(NOMINATIM_URL, params=params, headers={"User-Agent": USER_AGENT}, timeout=30)
        resp.raise_for_status()
        results = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeoError(f"Couldn't look up {city}, {country} on OpenStreetMap: {exc}") from exc
    if not results:
        raise GeoError(f"OpenStreetMap couldn't find {city}, {country}. Check the spelling.")

    result = results[0]
    lat, lng = float(result["lat"]), float(result["lon"])
    south, north, west, east = (float(v) for v in result["boundingbox"])
    polygons = outer_rings(result.get("geojson"))
    if not polygons and (north - south) < 2 * POINT_CITY_HALF_DEG:
        south, north = lat - POINT_CITY_HALF_DEG, lat + POINT_CITY_HALF_DEG
        west, east = lng - POINT_CITY_HALF_DEG, lng + POINT_CITY_HALF_DEG
    return {
        "name": result.get("display_name", f"{city}, {country}"),
        "lat": lat, "lng": lng, "bbox": (south, west, north, east), "polygons": polygons,
    }


def neighbourhood_name(lat, lng):
    """Neighbourhood at a point via free Nominatim reverse lookup (their limit: 1 request/second). '' on failure."""
    time.sleep(1)
    try:
        resp = requests.get(
            REVERSE_URL, params={"lat": lat, "lon": lng, "format": "json", "zoom": 14, "addressdetails": 1},
            headers={"User-Agent": USER_AGENT}, timeout=20,
        )
        resp.raise_for_status()
        address = resp.json().get("address", {})
    except (requests.RequestException, ValueError):
        return ""
    return next((address[k] for k in NEIGHBOURHOOD_KEYS if address.get(k)), "")


def outer_rings(geojson):
    if not geojson:
        return []
    if geojson.get("type") == "Polygon":
        return [geojson["coordinates"][0]]
    if geojson.get("type") == "MultiPolygon":
        return [poly[0] for poly in geojson["coordinates"]]
    return []


def _point_in_ring(lng, lat, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat) and lng < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def inside_city(polygons, lng, lat, tolerance_deg=0.003):
    """True if the point (or a spot ~300 m away, to forgive the simplified outline) is inside the city."""
    if not polygons:
        return True
    offsets = [(0, 0), (tolerance_deg, 0), (-tolerance_deg, 0), (0, tolerance_deg), (0, -tolerance_deg)]
    return any(_point_in_ring(lng + dx, lat + dy, ring) for dx, dy in offsets for ring in polygons)


def box_touches_city(box, polygons):
    if not polygons:
        return True
    south, west, north, east = box
    steps = 5
    for i in range(steps):
        for j in range(steps):
            lat = south + (north - south) * (i + 0.5) / steps
            lng = west + (east - west) * (j + 0.5) / steps
            if inside_city(polygons, lng, lat, tolerance_deg=0):
                return True
    return any(south <= lat <= north and west <= lng <= east for ring in polygons for lng, lat in ring)


def box_size_km(box):
    south, west, north, east = box
    height = (north - south) * KM_PER_DEG
    width = (east - west) * KM_PER_DEG * math.cos(math.radians((south + north) / 2))
    return height, width


def box_area_km2(box):
    height, width = box_size_km(box)
    return height * width


def box_center(box):
    south, west, north, east = box
    return (south + north) / 2, (west + east) / 2


def split_box(box):
    south, west, north, east = box
    mid_lat, mid_lng = (south + north) / 2, (west + east) / 2
    return [
        (south, west, mid_lat, mid_lng), (south, mid_lng, mid_lat, east),
        (mid_lat, west, north, mid_lng), (mid_lat, mid_lng, north, east),
    ]


def initial_grid(bbox, cell_km):
    south, west, north, east = bbox
    height, width = box_size_km(bbox)
    rows, cols = max(1, math.ceil(height / cell_km)), max(1, math.ceil(width / cell_km))
    lat_edges = [south + (north - south) * r / rows for r in range(rows)] + [north]
    lng_edges = [west + (east - west) * c / cols for c in range(cols)] + [east]
    return [
        (lat_edges[r], lng_edges[c], lat_edges[r + 1], lng_edges[c + 1])
        for r in range(rows) for c in range(cols)
    ]


def distance_km(lat1, lng1, lat2, lng2):
    x = (lng2 - lng1) * KM_PER_DEG * math.cos(math.radians((lat1 + lat2) / 2))
    y = (lat2 - lat1) * KM_PER_DEG
    return math.hypot(x, y)
