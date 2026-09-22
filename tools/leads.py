"""Non-interactive entry point so Claude can run Lead Hunter from chat: import places collected from Google Maps
(with Claude in Chrome), run the usual filters + website checks, and save to memory + Excel. Never prompts.

  .venv\\Scripts\\python.exe tools\\leads.py import data\\gmaps\\FILE.json --domain cafe --city Gurugram --country India --count 50
  .venv\\Scripts\\python.exe tools\\leads.py import FILE.json ... --dry-run     (counts only, no website checks, nothing saved)
  .venv\\Scripts\\python.exe tools\\leads.py export                           (retry writing to Excel after it was open)
  .venv\\Scripts\\python.exe tools\\leads.py status

FILE.json is a list of {"name", "address", "phone", "website", "rating", "reviews", "maps_link", "area"} objects;
only "name" is required. The last line of every command starts with RESULT: for Claude to read."""
import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lead_hunter import cli, excel, filters, store  # noqa: E402

PLACE_ID_RE = re.compile(r"!1s(0x[0-9a-f]+:0x[0-9a-f]+)")
COORDS_RE = re.compile(r"!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)")


def _number(value):
    try:
        return float(str(value).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def _count(value):
    digits = re.sub(r"\D", "", str(value or ""))
    return int(digits) if digits else None


def to_place(item):
    """One collected Google Maps listing -> the place dict the rest of the app uses."""
    link = (item.get("maps_link") or "").strip()
    name = (item.get("name") or "").strip()
    address = (item.get("address") or "").strip()
    found_id = PLACE_ID_RE.search(link)
    fallback = hashlib.md5(f"{name}|{address}".lower().encode()).hexdigest()[:16]
    coords = COORDS_RE.search(link)
    return {
        "place_id": item.get("place_id") or f"gm:{found_id.group(1) if found_id else fallback}",
        "name": name,
        "address": address,
        "phone": (item.get("phone") or "").strip(),
        "website": (item.get("website") or "").strip(),
        "email": (item.get("email") or "").strip(),
        "instagram": (item.get("instagram") or "").strip(),
        "facebook": (item.get("facebook") or "").strip(),
        "rating": _number(item.get("rating")),
        "reviews": _count(item.get("reviews")),
        "maps_link": link,
        "closed": bool(item.get("closed")),
        "area": (item.get("area") or "").strip(),
        "lat": float(coords.group(1)) if coords else item.get("lat"),
        "lng": float(coords.group(2)) if coords else item.get("lng"),
    }


def screen(conn, places):
    """Drop repeats, closed places, leads already saved or rejected, and chains. Returns (fresh, skipped)."""
    detector = filters.ChainDetector(filters.load_chain_names(cli.CHAINS_PATH))
    detector.observe(places)
    skipped = Counter()
    fresh, seen_ids, seen_phones, seen_names = [], set(), set(), set()
    for place in places:
        phone = store.normalize_phone(place["phone"])
        name_key = store.make_name_key(place["name"], place["address"])
        if place["place_id"] in seen_ids or (phone and phone in seen_phones) or (name_key and name_key in seen_names):
            skipped["listed twice in the file"] += 1
            continue
        seen_ids.add(place["place_id"])
        if place["closed"]:
            reason = "closed"
        elif store.is_duplicate(conn, place["place_id"], place["phone"], place["name"], place["address"]):
            reason = "already in your list"
        elif store.is_skipped(conn, place["place_id"]):
            reason = "no contact details (checked before)"
        else:
            reason = detector.reason(place, {})
        if reason:
            skipped[reason] += 1
            continue
        seen_phones.add(phone)
        seen_names.add(name_key)
        fresh.append(place)
    return fresh, skipped


def export(conn):
    """Write batches that aren't in Excel yet. Returns False if Excel has the file open."""
    for batch in store.pending_batches(conn):
        try:
            excel.sync_from_excel(conn, cli.XLSX_PATH)
            excel.append_batch(conn, cli.XLSX_PATH, batch)
        except excel.ExcelLocked:
            print("Excel has leads.xlsx open. The leads are safe in memory; close Excel, then run: tools\\leads.py export")
            return False
        store.mark_exported(conn, batch["id"])
        print(f"Saved Batch {batch['batch_no']} ({batch['lead_count']} leads) to sheet '{batch['domain']}' in {cli.XLSX_PATH}")
    return True


def import_cmd(args):
    conn = store.connect(cli.DB_PATH)
    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    places = [to_place(i) for i in items if (i.get("name") or "").strip()]
    fresh, skipped = screen(conn, places)
    print(f"{len(places)} listings in file, {len(fresh)} new candidates after filters.")
    if args.dry_run:
        print(f"RESULT: listings={len(places)} new_candidates={len(fresh)} skipped={dict(skipped)}")
        return

    owner_fn = cli.make_owner_fn(cli.load_config())
    kept, used = [], 0
    while len(kept) < args.count and used < len(fresh):
        chunk = fresh[used: used + args.count - len(kept)]
        used += len(chunk)
        kept += cli.enrich_and_filter(conn, owner_fn, chunk, skipped)
    cli.print_skipped(skipped)

    excel_ok = True
    if kept:
        for lead in kept:
            lead["area"] = lead["area"] or args.area or ""
        areas = list(dict.fromkeys(lead["area"] for lead in kept if lead["area"]))
        desc = f"{' / '.join(areas) or args.city}, {args.city}, {args.country} (Google Maps)"
        cli.save_leads(conn, args.domain, desc, kept, args.city, args.country, export=False)
        excel_ok = export(conn)
    print(
        f"RESULT: saved={len(kept)} wanted={args.count} unused_candidates={len(fresh) - used} "
        f"excel={'ok' if excel_ok else 'locked'} skipped={dict(skipped)}"
    )


def export_cmd(args):
    conn = store.connect(cli.DB_PATH)
    ok = export(conn)
    print(f"RESULT: excel={'ok' if ok else 'locked'}")


def status_cmd(args):
    conn = store.connect(cli.DB_PATH)
    try:
        cli.sync_excel(conn)
    except Exception:
        pass
    rows = store.summary_rows(conn)
    for domain, entry in rows.items():
        statuses = ", ".join(f"{s} {n}" for s, n in entry["statuses"].items())
        print(f"{domain}: {entry['total']} leads ({entry['hot']} Hot) - {statuses}")
    print("Recent batches:")
    for batch in conn.execute("SELECT * FROM batches WHERE lead_count > 0 ORDER BY id DESC LIMIT 10"):
        print(f"  {batch['created_at'][:10]}  {batch['domain']} batch {batch['batch_no']}: {batch['lead_count']} leads - {batch['target']}")
    pending = len(store.pending_batches(conn))
    print(f"RESULT: sheets={len(rows)} leads={sum(e['total'] for e in rows.values())} batches_not_in_excel={pending}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import", help="import collected Google Maps listings")
    imp.add_argument("file")
    imp.add_argument("--domain", required=True)
    imp.add_argument("--city", required=True)
    imp.add_argument("--country", required=True)
    imp.add_argument("--area", default="", help="used when a listing has no area")
    imp.add_argument("--count", type=int, default=50, help="how many NEW contactable leads to save")
    imp.add_argument("--dry-run", action="store_true")
    imp.set_defaults(func=import_cmd)
    sub.add_parser("export", help="write pending batches to Excel").set_defaults(func=export_cmd)
    sub.add_parser("status", help="leads per sheet and recent batches").set_defaults(func=status_cmd)
    args = parser.parse_args()
    args.func(args)


def _closing(command):
    """Run a command with its own database connection and always close it (Windows keeps open files locked)."""
    def run(args):
        original = store.connect
        opened = []

        def connect(path):
            conn = original(path)
            opened.append(conn)
            return conn
        store.connect = connect
        try:
            command(args)
        finally:
            store.connect = original
            for conn in opened:
                conn.close()
    return run


import_cmd, export_cmd, status_cmd = _closing(import_cmd), _closing(export_cmd), _closing(status_cmd)


if __name__ == "__main__":
    main()
