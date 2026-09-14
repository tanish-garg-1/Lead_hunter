"""Google Places API (New) Text Search, with a hard monthly request limit so the free tier is never exceeded."""
import requests

from . import store

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = ",".join([
    "places.id", "places.displayName", "places.formattedAddress", "places.nationalPhoneNumber",
    "places.internationalPhoneNumber", "places.websiteUri", "places.rating", "places.userRatingCount",
    "places.googleMapsUri", "places.businessStatus", "places.location", "places.addressComponents",
    "nextPageToken",
])
USAGE_KEY = "google_places"
MAX_PAGES_PER_QUERY = 3  # Google returns at most 60 results (3 x 20) per query
SUBURB_TYPES = ("neighborhood", "sublocality_level_2", "sublocality_level_1", "sublocality", "administrative_area_level_3")

SYNONYMS = {
    "cafe": ["coffee shop"],
    "coffee": ["cafe"],
    "grocery": ["supermarket"],
    "hospital": ["clinic"],
    "gym": ["fitness centre"],
    "salon": ["beauty parlour"],
    "startup": ["software company"],
    "dentist": ["dental clinic"],
    "bakery": ["cake shop"],
    "restaurant": ["family restaurant"],
}


class QuotaExceeded(Exception):
    pass


class PlacesError(Exception):
    pass


def search_terms(domain):
    """The domain itself first, then close synonyms to find more businesses when the first query runs out."""
    base = domain.strip().lower()
    terms = [base]
    for key, extra in SYNONYMS.items():
        if key in base:
            terms += [t for t in extra if t.rstrip("s") != base.rstrip("s") and t not in terms]
    return terms


class GooglePlacesProvider:
    name = "Google Places"

    def __init__(self, api_key, conn, monthly_limit):
        self.api_key = api_key
        self.conn = conn
        self.monthly_limit = monthly_limit

    def search(self, domain, area, city, country):
        """Quick search by area name (max 60 results per term)."""
        for term in search_terms(domain):
            body = {"textQuery": f"{term} in {area}, {city}, {country}", "pageSize": 20}
            token = None
            for _ in range(MAX_PAGES_PER_QUERY):
                data = self._post({**body, "pageToken": token} if token else body)
                for place in data.get("places", []):
                    yield self._normalize(place)
                token = data.get("nextPageToken")
                if not token:
                    break

    def search_box(self, domain, box, can_split):
        """Search strictly inside one map square. Returns (places, saturated). When the first page is already
        full and the square can still be split, stop there: 4 smaller squares will find everything."""
        south, west, north, east = box
        body = {
            "textQuery": domain.strip(),
            "pageSize": 20,
            "locationRestriction": {"rectangle": {
                "low": {"latitude": south, "longitude": west},
                "high": {"latitude": north, "longitude": east},
            }},
        }
        data = self._post(body)
        places = [self._normalize(p) for p in data.get("places", [])]
        token = data.get("nextPageToken")
        if token and can_split:
            return places, True
        for _ in range(MAX_PAGES_PER_QUERY - 1):
            if not token:
                break
            data = self._post({**body, "pageToken": token})
            places += [self._normalize(p) for p in data.get("places", [])]
            token = data.get("nextPageToken")
        return places, False

    def _post(self, body):
        used = store.usage_get(self.conn, USAGE_KEY)
        if used >= self.monthly_limit:
            raise QuotaExceeded(
                f"Monthly Google request limit reached ({used}/{self.monthly_limit}). It resets next month. "
                "Google's free cap is 1,000; going above that costs money."
            )
        store.usage_add(self.conn, USAGE_KEY)
        headers = {"X-Goog-Api-Key": self.api_key, "X-Goog-FieldMask": FIELD_MASK}
        try:
            resp = requests.post(SEARCH_URL, json=body, headers=headers, timeout=20)
        except requests.RequestException as exc:
            raise PlacesError(f"Network error talking to Google: {exc}") from exc
        if resp.status_code != 200:
            try:
                message = resp.json()["error"]["message"]
            except (ValueError, KeyError):
                message = resp.text[:300]
            raise PlacesError(f"Google Places error {resp.status_code}: {message}")
        return resp.json()

    @staticmethod
    def _normalize(place):
        components = {}
        for component in place.get("addressComponents", []):
            for kind in component.get("types", []):
                components.setdefault(kind, component.get("longText", ""))
        location = place.get("location", {})
        return {
            "place_id": place["id"],
            "name": place.get("displayName", {}).get("text", ""),
            "address": place.get("formattedAddress", ""),
            "phone": place.get("internationalPhoneNumber") or place.get("nationalPhoneNumber") or "",
            "website": place.get("websiteUri", ""),
            "rating": place.get("rating"),
            "reviews": place.get("userRatingCount"),
            "maps_link": place.get("googleMapsUri", ""),
            "closed": place.get("businessStatus") in ("CLOSED_PERMANENTLY", "CLOSED_TEMPORARILY"),
            "lat": location.get("latitude"),
            "lng": location.get("longitude"),
            "postcode": components.get("postal_code", ""),
            "suburb": next((components[t] for t in SUBURB_TYPES if components.get(t)), ""),
        }
