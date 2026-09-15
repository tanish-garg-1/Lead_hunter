"""Compare the free data sources on real areas: how many businesses each finds, how many can be contacted,
how much they overlap, and (optionally) how many of the places you noted from Google Maps each one finds.

Run from the project folder:
  .venv\\Scripts\\python.exe tools\\coverage_check.py
  .venv\\Scripts\\python.exe tools\\coverage_check.py --domain cafe --area "Sector 29, Gurugram, India" --enrich 20

Google Maps sample (optional): fill data/coverage_truth.csv with columns area,name — about 20 places per area,
copied by hand from Google Maps. The area text must match the --area text."""
import argparse
import csv
import difflib
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from lead_hunter import enrich, filters, geo, store  # noqa: E402
from lead_hunter.osm import OSMProvider  # noqa: E402
from lead_hunter.overture import OvertureProvider  # noqa: E402
from lead_hunter.places import GooglePlacesProvider, PlacesError  # noqa: E402

DEFAULT_AREAS = [
    "Sector 29, Gurugram, India",
    "Connaught Place, New Delhi, India",
    "Baixa, Lisbon, Portugal",
    "Pearl District, Portland, United States",
]
TRUTH_PATH = ROOT / "data" / "coverage_truth.csv"
REPORT_PATH = ROOT / "data" / "coverage_report.md"
MATCH_METERS = 100
console = Console()


def same_place(a, b):
    name_a, name_b = filters.normalize_name(a["name"]), filters.normalize_name(b["name"])
    if not name_a or not name_b:
        return False
    similar = name_a == name_b or (min(len(name_a), len(name_b)) >= 4 and (name_a in name_b or name_b in name_a)) \
        or difflib.SequenceMatcher(None, name_a, name_b).ratio() >= 0.85
    if not similar:
        return False
    if None in (a.get("lat"), b.get("lat")):
        return True
    return geo.distance_km(a["lat"], a["lng"], b["lat"], b["lng"]) * 1000 <= MATCH_METERS


def name_found(name, places):
    target = filters.normalize_name(name)
    return any(
        target and (target == (n := filters.normalize_name(p["name"])) or (len(target) >= 4 and (target in n or n in target))
                    or difflib.SequenceMatcher(None, target, n).ratio() >= 0.8)
        for p in places
    )


def stats(places, chains):
    detector = filters.ChainDetector(chains)
    detector.observe(places)
    return {
        "places": len(places),
        "phone": sum(bool(p.get("phone")) for p in places),
        "website": sum(bool(p.get("website")) for p in places),
        "any contact": sum(filters.has_contact(p) for p in places),
        "chains": sum(bool(detector.reason(p, {})) for p in places),
    }


def contactable_after_checks(places, sample_size):
    """Run the real website check on a random sample; returns (contactable, checked)."""
    sample = [dict(p) for p in random.Random(1).sample(places, min(sample_size, len(places)))]
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(enrich.enrich_lead, sample))
    return sum(filters.has_contact(p) for p in sample), len(sample)


def load_truth():
    if not TRUTH_PATH.exists():
        TRUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
        TRUTH_PATH.write_text("area,name\n", encoding="utf-8")
        return {}
    truth = {}
    with TRUTH_PATH.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            if row.get("area") and row.get("name"):
                truth.setdefault(row["area"].strip().lower(), []).append(row["name"].strip())
    return truth


def fetch(label, provider, domain, box):
    try:
        with console.status(f"{label}: fetching..."):
            return provider.search_box(domain, box, False)[0]
    except PlacesError as exc:
        console.print(f"[red]{label}: {exc}[/]")
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", default="cafe")
    parser.add_argument("--area", action="append", help="repeatable; defaults to 2 Indian + 2 foreign areas")
    parser.add_argument("--enrich", type=int, default=0, metavar="N", help="website-check N random places per source")
    args = parser.parse_args()

    chains = filters.load_chain_names(ROOT / "chains.txt")
    truth = load_truth()
    overture = OvertureProvider(ROOT / "data" / "overture", log=console.print)
    osm = OSMProvider()
    load_dotenv(ROOT / ".env")
    google = None
    if key := os.getenv("GOOGLE_PLACES_API_KEY", "").strip():
        # Up to 3 requests per area (Google's top 60), counted against the app's monthly limit.
        google = GooglePlacesProvider(key, store.connect(ROOT / "data" / "leads.db"),
                                      int(os.getenv("MONTHLY_REQUEST_LIMIT", "900")))
    else:
        console.print("[dim]No GOOGLE_PLACES_API_KEY in .env: comparing Overture and OpenStreetMap only.[/]")
    report = [f"# Coverage check: {args.domain} ({date.today().isoformat()})", ""]

    for area in args.area or DEFAULT_AREAS:
        console.rule(area)
        try:
            box = geo.area_bbox(area)
        except geo.GeoError as exc:
            console.print(f"[red]{exc}[/]")
            continue
        results = {"Overture": fetch("Overture", overture, args.domain, box), "OSM": fetch("OpenStreetMap", osm, args.domain, box)}
        if google:
            results["Google"] = fetch("Google", google, args.domain, box)
        available = {k: v for k, v in results.items() if v is not None}
        rows = {k: stats(v, chains) for k, v in available.items()}

        if available.get("Google"):
            reference = available["Google"]
            for source in ("Overture", "OSM"):
                if source in available:
                    found = sum(any(same_place(g, p) for p in available[source]) for g in reference)
                    rows[source]["has Google's places"] = f"{found}/{len(reference)}"

        if "Overture" in available and "OSM" in available:
            ovt, osm_places = available["Overture"], available["OSM"]
            both = sum(any(same_place(a, b) for b in osm_places) for a in ovt)
            rows["Overture"]["only here"] = len(ovt) - both
            rows["OSM"]["only here"] = len(osm_places) - sum(any(same_place(b, a) for a in ovt) for b in osm_places)
            rows["Overture"]["in both"] = rows["OSM"]["in both"] = both

        names = truth.get(area.strip().lower(), [])
        for source, places in available.items():
            if names:
                rows[source]["Google sample found"] = f"{sum(name_found(n, places) for n in names)}/{len(names)}"
            if args.enrich and places:
                with console.status(f"{source}: checking websites on {min(args.enrich, len(places))} places..."):
                    ok, checked = contactable_after_checks(places, args.enrich)
                rows[source][f"contactable after checks (sample)"] = f"{ok}/{checked}"
        if names and "Overture" in available and "OSM" in available:
            merged = available["Overture"] + available["OSM"]
            rows.setdefault("Both combined", {})["Google sample found"] = f"{sum(name_found(n, merged) for n in names)}/{len(names)}"

        metrics = list(dict.fromkeys(m for r in rows.values() for m in r))
        table = Table(title=f"{args.domain} in {area}")
        table.add_column("")
        for source in rows:
            table.add_column(source, justify="right")
        report += [f"## {area}", "", "| | " + " | ".join(rows) + " |", "|---|" + "---|" * len(rows)]
        for metric in metrics:
            values = [str(rows[s].get(metric, "")) for s in rows]
            table.add_row(metric, *values)
            report.append(f"| {metric} | " + " | ".join(values) + " |")
        report.append("")
        console.print(table)
        if not names:
            console.print(f"[dim]Tip: add ~20 places you see on Google Maps for this area to {TRUTH_PATH} (area,name) to measure how complete each source is.[/]")

    REPORT_PATH.write_text("\n".join(report), encoding="utf-8")
    console.print(f"\nReport saved to {REPORT_PATH}")


if __name__ == "__main__":
    main()
