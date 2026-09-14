"""Keep only leads worth pitching: independent businesses you can actually contact."""
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

from .enrich import SOCIAL_ONLY_HOSTS, _host, _host_matches

CONTACT_KEYS = ("phone", "email", "instagram", "facebook", "whatsapp", "linkedin")
SAME_PAGE_BRANCHES = 2  # same brand twice in one page of results = several branches nearby
SAME_PAGE_MAX_RESULTS = 20  # ...but only for a small page; a whole-square OSM result can hold two unrelated "Café Mocca"s
RUN_BRANCHES = 3        # or at three different places anywhere in this run


def normalize_name(name):
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    text = re.sub(r"['’`]", "", text.lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def brand_key(name):
    """'Starbucks Coffee - Sector 29' and 'Starbucks Coffee (DLF)' -> 'starbucks coffee'."""
    return normalize_name(re.split(r"\s[-–|@]\s|\(|,", name or "")[0])


def load_chain_names(path):
    path = Path(path)
    if not path.exists():
        return set()
    names = {normalize_name(line.split("#")[0]) for line in path.read_text(encoding="utf-8").splitlines()}
    names.discard("")
    return names


def is_known_chain(name, chains):
    """Chain listings start with the brand ('Starbucks Coffee - Mitte'); matching only at the start avoids
    flagging an independent like 'Calli. The coffee club'."""
    padded = f"{normalize_name(name)} "
    return any(padded.startswith(f"{chain} ") for chain in chains)


def website_key(place):
    url = (place.get("website") or "").strip()
    if not url:
        return ""
    host = _host(url if "//" in url else "http://" + url)
    return "" if _host_matches(host, SOCIAL_ONLY_HOSTS) else host


def has_contact(lead):
    return any((lead.get(key) or "").strip() for key in CONTACT_KEYS)


class ChainDetector:
    """Spots chains from the known-brands list, brand tags, and the same name/website at several locations."""

    def __init__(self, chains):
        self.chains = chains
        self.places_by_key = defaultdict(set)

    @staticmethod
    def keys_for(place):
        keys = []
        if brand := brand_key(place.get("name")):
            keys.append("name:" + brand)
        if site := website_key(place):
            keys.append("site:" + site)
        return keys

    def observe(self, page):
        """Register one page of results; returns which distinct places share each name/website on this page.
        Counting place IDs (not appearances) means the same café found by two queries isn't a 'branch'."""
        page_keys = defaultdict(set)
        for place in page:
            for key in self.keys_for(place):
                page_keys[key].add(place["place_id"])
                self.places_by_key[key].add(place["place_id"])
        return page_keys if len(page) <= SAME_PAGE_MAX_RESULTS else {}

    def reason(self, place, page_keys):
        if place.get("brand"):
            return "chain / franchise"
        if is_known_chain(place.get("name"), self.chains):
            return "chain / franchise"
        for key in self.keys_for(place):
            if len(page_keys.get(key, ())) >= SAME_PAGE_BRANCHES or len(self.places_by_key[key]) >= RUN_BRANCHES:
                return "chain / franchise"
        return ""

    def has_many_branches(self, place):
        """Final check at the end of a run, once every result has been seen."""
        return any(len(self.places_by_key[key]) >= RUN_BRANCHES for key in self.keys_for(place))
