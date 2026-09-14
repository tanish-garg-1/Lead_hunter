"""Terminal interface: quick searches and whole-city hunts; check websites, filter, score, save to memory + Excel."""
import itertools
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import questionary
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn
from rich.table import Table

from . import discuss, enrich, excel, filters, geo, hunt, score, store
from .osm import OSMProvider
from .places import USAGE_KEY, GooglePlacesProvider, PlacesError, QuotaExceeded

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "leads.db"
XLSX_PATH = ROOT / "data" / "leads.xlsx"
CHAINS_PATH = ROOT / "chains.txt"
ENRICH_WORKERS = 6
PAGE_SIZE = 20  # one Google request; branches of a chain are compared within a page

console = Console()
Choice = questionary.Choice


def load_config():
    load_dotenv(ROOT / ".env")
    try:
        limit = int(os.getenv("MONTHLY_REQUEST_LIMIT", "900"))
    except ValueError:
        limit = 900
    return {
        "google_key": os.getenv("GOOGLE_PLACES_API_KEY", "").strip(),
        "groq_key": os.getenv("GROQ_API_KEY", "").strip(),
        "groq_model": os.getenv("GROQ_MODEL", "").strip() or discuss.DEFAULT_MODEL,
        "monthly_limit": limit,
    }


# ---------- Excel sync / export ----------

def sync_excel(conn, quiet=True):
    try:
        changed = excel.sync_from_excel(conn, XLSX_PATH)
    except excel.ExcelLocked:
        console.print("[yellow]Couldn't read leads.xlsx right now; statuses will sync next time.[/]")
        return 0
    if changed and not quiet:
        console.print(f"[green]Synced {changed} status/notes change(s) from Excel into memory.[/]")
    return changed


def export_pending(conn):
    for batch in store.pending_batches(conn):
        while True:
            sync_excel(conn)
            try:
                excel.append_batch(conn, XLSX_PATH, batch)
            except excel.ExcelLocked:
                retry = questionary.confirm(
                    "leads.xlsx is open in Excel. Save & close it, then choose Yes to retry "
                    "(No = it will be written next time you run)", default=True,
                ).ask()
                if not retry:
                    console.print("[yellow]Leads are safe in memory and will be added to Excel on the next run.[/]")
                    return
                continue
            store.mark_exported(conn, batch["id"])
            console.print(
                f"[green]Saved[/] Batch {batch['batch_no']} ({batch['lead_count']} leads) "
                f"→ sheet '{batch['domain']}' in {XLSX_PATH}"
            )
            break


# ---------- asking for targets ----------

def _required(value):
    return True if value and value.strip() else "Required"


def _positive_int(value):
    return True if value.strip().isdigit() and int(value) > 0 else "Enter a number above 0"


def ask_count():
    count = questionary.text("How many NEW leads do you want?", default="50", validate=_positive_int).ask()
    return int(count) if count else None


def _place_name(value):
    if not value or not value.strip():
        return "Required"
    if len(value.split()) > 4 or "?" in value or "help" in value.lower():
        return "Just the name here (e.g. Germany). For suggestions press Ctrl+C and choose 'help me pick a city'."
    return True


def _ask_text(label, default="", validate=_required):
    value = questionary.text(label, default=default, validate=validate).ask()
    return value.strip() if value else None


def ask_place(defaults=None):
    d = defaults or {}
    country = _ask_text("Country:", d.get("country", ""), _place_name)
    city = country and _ask_text("City:", d.get("city", ""), _place_name)
    domain = city and _ask_text("Business type (cafe, dentist, grocery store...):", d.get("domain", ""))
    return {"country": country, "city": city, "domain": domain} if domain else None


def ask_target(defaults=None):
    place = ask_place(defaults)
    if not place:
        return None
    areas = _ask_text("Area(s) / sector(s), comma-separated:", ", ".join((defaults or {}).get("areas", [])))
    count = areas and ask_count()
    if not count:
        return None
    return {**place, "areas": [a.strip() for a in areas.split(",") if a.strip()], "count": count}


def discuss_target(cfg, force_mode=None):
    """Returns ("quick", target) or ("hunt", place) once agreed, else None. force_mode skips the mode question."""
    if not cfg["groq_key"]:
        console.print("[red]Add GROQ_API_KEY to .env first (free at https://console.groq.com/keys).[/]")
        return None
    console.print(Panel(
        "Tell me what you sell and who you'd like to work with. I'll suggest where to look.\n"
        "Type [bold]/done[/] to wrap up with the best options so far, [bold]/quit[/] to go back.",
        title="Help me decide", border_style="magenta",
    ))
    conversation = discuss.Discussion(cfg["groq_key"], cfg["groq_model"])
    while True:
        text = questionary.text("You:").ask()
        if text is None or text.strip().lower() == "/quit":
            return None
        if not text.strip():
            continue
        if text.strip().lower() == "/done":
            text = "/done — please finalise now and output the json block."
        try:
            with console.status("Thinking..."):
                reply = conversation.send(text)
        except discuss.GroqError as exc:
            console.print(f"[red]{exc}[/]")
            continue
        console.print(Markdown(reply))
        target = discuss.extract_target(reply)
        if not target:
            continue
        console.print(Panel(target["rationale"] or "Target agreed.", title="Proposed target", border_style="green"))
        mode = force_mode or questionary.select("How do you want to search?", choices=[
            Choice(f"Hunt all of {target['city']}, cluster by cluster (nothing missed)", "hunt"),
            Choice(f"Quick search only in: {', '.join(target['areas'])}", "quick"),
        ]).ask()
        if mode is None:
            return None
        console.print("Check or edit the details:")
        chosen = ask_place(target) if mode == "hunt" else ask_target(target)
        return (mode, chosen) if chosen else None


# ---------- shared pipeline ----------

def get_provider(cfg, conn):
    if cfg["google_key"]:
        return GooglePlacesProvider(cfg["google_key"], conn, cfg["monthly_limit"])
    console.print("[yellow]No GOOGLE_PLACES_API_KEY in .env: using free OpenStreetMap data (fewer phones/websites, no ratings).[/]")
    return OSMProvider()


def make_owner_fn(cfg):
    if not cfg["groq_key"]:
        return None

    def owner_fn(business, snippets):
        return discuss.extract_owner_name(cfg["groq_key"], business, snippets)
    return owner_fn


def enrich_all(owner_fn, candidates):
    columns = (TextColumn("{task.description}"), BarColumn(), MofNCompleteColumn())
    with Progress(*columns, console=console) as progress:
        task = progress.add_task("Checking websites, emails & socials", total=len(candidates))
        with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
            for future in as_completed([pool.submit(enrich.enrich_lead, c, owner_fn) for c in candidates]):
                future.result()
                progress.advance(task)


def enrich_and_filter(conn, owner_fn, places, skipped):
    """Check websites, then keep only leads with at least one way to contact them."""
    if not places:
        return []
    enrich_all(owner_fn, places)
    kept = []
    for lead in places:
        if filters.has_contact(lead):
            kept.append(lead)
        else:
            store.add_skipped(conn, lead["place_id"], lead["name"], "no contact details")
            skipped["no contact details"] += 1
    return kept


def print_skipped(skipped):
    if skipped:
        console.print("[dim]Skipped: " + ", ".join(f"{n} {reason}" for reason, n in skipped.most_common()) + "[/]")


def save_leads(conn, domain, target_desc, leads, city, country, hunt_id=None):
    sheet = excel.sheet_name_for(domain)
    batch_id, _ = store.create_batch(conn, sheet, target_desc)
    today = date.today().isoformat()
    for lead in leads:
        lead.update(domain=sheet, batch_id=batch_id, date_added=today, city=city, country=country, hunt_id=hunt_id)
        lead["score"], lead["priority"] = score.score_lead(lead)
        store.insert_lead(conn, lead)
    store.finalize_batch(conn, batch_id)
    show_top_leads(leads)
    export_pending(conn)


def show_top_leads(leads, limit=15):
    table = Table(title=f"New leads ({len(leads)}), best first")
    for header in ("Business", "Area", "Priority", "Website", "Email", "Instagram", "Phone"):
        table.add_column(header, overflow="fold")
    colours = {"Hot": "red", "Warm": "yellow", "Cold": "dim"}
    for lead in sorted(leads, key=lambda l: -l["score"])[:limit]:
        table.add_row(
            lead["name"], lead.get("area") or "-", f"[{colours[lead['priority']]}]{lead['priority']} ({lead['score']})[/]",
            lead["has_website"], lead["email"] or "-", "yes" if lead["instagram"] else "-", lead.get("phone") or "-",
        )
    console.print(table)


# ---------- quick search (named areas) ----------

class Search:
    """Streams new, independent businesses across named areas; remembers what it has seen for the whole run."""

    def __init__(self, conn, provider, target, detector):
        self.conn = conn
        self.provider = provider
        self.target = target
        self.detector = detector
        self.seen_ids, self.seen_phones, self.seen_names = set(), set(), set()
        self.searched = []
        self.skipped = Counter()
        self.quota_hit = False

    def candidates(self, areas):
        for area in areas:
            if area in self.searched or self.quota_hit:
                continue
            self.searched.append(area)
            t = self.target
            console.print(f"[cyan]Searching[/] {t['domain']} in {area}, {t['city']} via {self.provider.name}...")
            try:
                results = self.provider.search(t["domain"], area, t["city"], t["country"])
                while page := list(itertools.islice(results, PAGE_SIZE)):
                    page_keys = self.detector.observe(page)
                    for place in page:
                        if self._should_skip(place, page_keys):
                            continue
                        place["area"] = area
                        yield place
            except QuotaExceeded as exc:
                self.quota_hit = True
                console.print(f"[red]{exc}[/]")
                return
            except PlacesError as exc:
                console.print(f"[red]{exc}[/]")

    def _should_skip(self, place, page_keys):
        phone = store.normalize_phone(place.get("phone"))
        name_key = store.make_name_key(place["name"], place.get("address"))
        if (place["place_id"] in self.seen_ids or (phone and phone in self.seen_phones)
                or (name_key and name_key in self.seen_names)):
            return True  # already seen earlier in this run (e.g. by a synonym query)
        self.seen_ids.add(place["place_id"])

        if place.get("closed"):
            reason = "closed"
        elif store.is_duplicate(self.conn, place["place_id"], place.get("phone"), place["name"], place.get("address")):
            reason = "already in your list"
        elif store.is_skipped(self.conn, place["place_id"]):
            reason = "no contact details (checked before)"
        else:
            reason = self.detector.reason(place, page_keys)
        if reason:
            self.skipped[reason] += 1
            return True
        self.seen_phones.add(phone)
        self.seen_names.add(name_key)
        return False


def run_search(conn, cfg, target):
    provider = get_provider(cfg, conn)
    export_pending(conn)

    wanted = target["count"]
    detector = filters.ChainDetector(filters.load_chain_names(CHAINS_PATH))
    search = Search(conn, provider, target, detector)
    owner_fn = make_owner_fn(cfg)
    candidates = []
    areas = list(target["areas"])

    # Keep pulling businesses until we have enough that can actually be contacted.
    while True:
        stream = search.candidates(areas)
        while len(candidates) < wanted and (chunk := list(itertools.islice(stream, wanted - len(candidates)))):
            candidates += enrich_and_filter(conn, owner_fn, chunk, search.skipped)
        if len(candidates) >= wanted or search.quota_hit:
            break
        console.print(
            f"[yellow]Found {len(candidates)}/{wanted} contactable leads in: {', '.join(search.searched)}.[/]"
        )
        more = questionary.text("Add nearby areas to search (comma-separated), or leave blank to finish:").ask()
        areas = [a.strip() for a in (more or "").split(",") if a.strip() and a.strip() not in search.searched]
        if not areas:
            break

    # Branches that only became obvious after all results were seen.
    late_chains = [c for c in candidates if detector.has_many_branches(c)]
    if late_chains:
        candidates = [c for c in candidates if c not in late_chains]
        search.skipped["chain / franchise"] += len(late_chains)

    print_skipped(search.skipped)
    if not candidates:
        console.print("[yellow]No new contactable leads found. Try other areas or a different business type.[/]")
        return
    used_areas = list(dict.fromkeys(c["area"] for c in candidates))
    save_leads(conn, target["domain"], f"{' / '.join(used_areas)}, {target['city']}, {target['country']}",
               candidates, target["city"], target["country"])


# ---------- city hunts ----------

def start_hunt(conn, cfg, place):
    existing = hunt.find_hunt(conn, place["domain"], place["city"], place["country"])
    if existing:
        console.print("[cyan]You already have this hunt, continuing where you left off.[/]")
        return existing["id"]
    with console.status(f"Looking up {place['city']} on the map..."):
        try:
            city_geo = geo.locate_city(place["city"], place["country"])
        except geo.GeoError as exc:
            console.print(f"[red]{exc}[/]")
            return None
    hunt_id = hunt.create_hunt(conn, place["domain"], place["city"], place["country"], city_geo)
    p = hunt.progress(conn, hunt_id)
    source = "Google" if cfg["google_key"] else "OpenStreetMap (free, no Google key set)"
    console.print(Panel(
        f"Found [bold]{city_geo['name']}[/]\n"
        f"The city is split into {p['pending_cells']} map squares (~{p['total_km2']:.0f} km²). Central squares go first; "
        "a busy square is split into 4 smaller ones until nothing is hidden.\n"
        f"Squares are only searched when you ask for more leads. Data source: {source}.",
        title="City hunt created", border_style="green",
    ))
    return hunt_id


def show_hunt_progress(conn, hunt_id):
    row = hunt.get_hunt(conn, hunt_id)
    p = hunt.progress(conn, hunt_id)
    filled = int(p["covered_pct"] / 5)
    lines = [
        f"Map covered   {'█' * filled}{'░' * (20 - filled)} {p['covered_pct']:.0f}%"
        f"  ({p['done_cells']} squares done, {p['pending_cells']} to go)",
        f"Leads saved   {p['leads']}     Waiting in queue   {p['queued']}",
    ]
    if row["focus_cluster"]:
        lines.append(f"Focus         {row['focus_cluster']}")
    console.print(Panel("\n".join(lines), title=f"{row['city']}, {row['country']} · {row['domain']}", border_style="cyan"))

    top = hunt.clusters(conn, hunt_id)[:10]
    if top:
        table = Table(title="Neighbourhoods")
        for header in ("Neighbourhood", "Leads", "Good responses", "Queued"):
            table.add_column(header)
        for c in top:
            table.add_row(c["name"] or c["cluster"], str(c["leads"]), str(c["positive"]), str(c["queued"]))
        console.print(table)


def run_hunt(conn, cfg, hunt_id, count):
    export_pending(conn)
    detector = filters.ChainDetector(filters.load_chain_names(CHAINS_PATH))
    h = hunt.Hunt(conn, hunt_id, get_provider(cfg, conn), detector, log=console.print)
    owner_fn = make_owner_fn(cfg)
    kept = []

    while len(kept) < count:
        need = count - len(kept)
        with console.status("Searching the map..."):
            h.fill_queue(need)
        chunk = h.take(need)
        if not chunk:
            break
        fresh = []
        for place in chunk:
            reason = h.recheck(place, claim=True)
            if reason:
                h.skipped[reason] += 1
            else:
                fresh.append(place)
        new_leads = enrich_and_filter(conn, owner_fn, fresh, h.skipped)
        kept_ids = {lead["place_id"] for lead in new_leads}
        h.done([p["place_id"] for p in chunk if p["place_id"] not in kept_ids])  # rejected: out of the queue now
        kept += new_leads

    if h.stop_reason:
        console.print(f"[red]{h.stop_reason}[/]")
    print_skipped(h.skipped)
    if kept:
        clusters = list(dict.fromkeys(lead["area"] for lead in kept))
        more = f" +{len(clusters) - 4} more" if len(clusters) > 4 else ""
        desc = f"{h.row['city']}, {h.row['country']} · {', '.join(clusters[:4])}{more}"
        save_leads(conn, h.row["domain"], desc, kept, h.row["city"], h.row["country"], hunt_id=hunt_id)
        h.done([lead["place_id"] for lead in kept])  # only leave the queue once safely saved
    else:
        console.print("[yellow]No new contactable leads this time.[/]")
    if h.is_complete():
        console.print(f"[green]{h.row['city']} is fully covered for {h.row['domain']}![/]")


def focus_menu(conn, hunt_id):
    sync_excel(conn)
    options = hunt.clusters(conn, hunt_id)
    if not options:
        console.print("No neighbourhoods yet. Get some leads first.")
        return
    choices = [
        Choice(f"{c['name'] or c['cluster']}  ({c['leads']} leads, {c['positive']} good responses, {c['queued']} queued)",
               c["cluster"])
        for c in options
    ] + [Choice("Clear focus (back to busiest-first)", "")]
    choice = questionary.select("Which neighbourhood should come first?", choices=choices).ask()
    if choice is None:
        return
    hunt.set_focus(conn, hunt_id, choice or None)
    console.print("[green]Focus cleared.[/]" if not choice else "[green]Next leads will come from there and nearby squares.[/]")


def hunt_actions(conn, cfg, hunt_id):
    while True:
        sync_excel(conn)
        show_hunt_progress(conn, hunt_id)
        action = questionary.select("What next?", choices=[
            Choice("Get more leads", "more"),
            Choice("Focus on a neighbourhood next (e.g. one that responded well)", "focus"),
            Choice("Back", "back"),
        ]).ask()
        if action in (None, "back"):
            return
        if action == "more":
            count = ask_count()
            if count:
                run_hunt(conn, cfg, hunt_id, count)
        else:
            focus_menu(conn, hunt_id)


def hunts_menu(conn, cfg):
    choices = []
    for row in hunt.list_hunts(conn):
        p = hunt.progress(conn, row["id"])
        choices.append(Choice(
            f"{row['city']}, {row['country']} · {row['domain']}  ({p['covered_pct']:.0f}% covered, {p['leads']} leads)",
            row["id"],
        ))
    choices += [Choice("Start a new city hunt", "new"), Choice("Back", "back")]
    choice = questionary.select("City hunts", choices=choices).ask()
    if choice in (None, "back"):
        return
    if choice == "new":
        how = questionary.select("Do you already know the city?", choices=[
            Choice("Yes, I'll type it", "type"),
            Choice("No, help me pick a city (AI discussion)", "ai"),
        ]).ask()
        if how is None:
            return
        if how == "ai":
            result = discuss_target(cfg, force_mode="hunt")
            place = result and result[1]
        else:
            place = ask_place()
        choice = place and start_hunt(conn, cfg, place)
        if not choice:
            return
    hunt_actions(conn, cfg, choice)


# ---------- other menu items ----------

def show_stats(conn, cfg):
    used = store.usage_get(conn, USAGE_KEY)
    console.print(Panel(
        f"Google requests this month: [bold]{used}[/] / {cfg['monthly_limit']} (Google's free cap: 1,000)",
        title="Usage", border_style="cyan",
    ))
    rows = store.summary_rows(conn)
    if not rows:
        console.print("No leads saved yet.")
        return
    table = Table(title="Leads in memory")
    for header in ["Sheet", "Total", "Hot"] + excel.STATUSES:
        table.add_column(header)
    for domain, entry in rows.items():
        table.add_row(domain, str(entry["total"]), str(entry["hot"]),
                      *[str(entry["statuses"].get(s, 0)) for s in excel.STATUSES])
    console.print(table)


def sync_menu(conn):
    if not XLSX_PATH.exists():
        console.print("No leads.xlsx yet.")
        return
    changed = sync_excel(conn, quiet=False)
    if not changed:
        console.print("Memory already matches Excel.")
        return
    try:
        excel.refresh_summary(conn, XLSX_PATH)
        console.print("[green]Summary sheet updated.[/]")
    except excel.ExcelLocked:
        console.print("[yellow]Close Excel to refresh the Summary sheet (memory is already updated).[/]")


def main():
    cfg = load_config()
    conn = store.connect(DB_PATH)
    console.print(Panel.fit("[bold]Lead Hunter[/]\nFind local businesses that need a website", border_style="cyan"))
    if not cfg["groq_key"]:
        console.print("[dim]Tip: add GROQ_API_KEY to .env to enable 'help me decide' and owner-name lookup.[/]")
    export_pending(conn)

    choices = [
        Choice("City hunt: cover a whole city, cluster by cluster (start / continue)", "hunts"),
        Choice("Quick search: specific areas I choose", "manual"),
        Choice("Help me decide (AI discussion)", "discuss"),
        Choice("Sync statuses/notes from Excel", "sync"),
        Choice("Usage & stats", "stats"),
        Choice("Exit", "exit"),
    ]
    try:
        while True:
            choice = questionary.select("What do you want to do?", choices=choices).ask()
            if choice in (None, "exit"):
                break
            if choice == "hunts":
                hunts_menu(conn, cfg)
            elif choice == "manual":
                target = ask_target()
                if target:
                    run_search(conn, cfg, target)
            elif choice == "discuss":
                result = discuss_target(cfg)
                if result and result[0] == "quick":
                    run_search(conn, cfg, result[1])
                elif result:
                    hunt_id = start_hunt(conn, cfg, result[1])
                    if hunt_id:
                        hunt_actions(conn, cfg, hunt_id)
            elif choice == "sync":
                sync_menu(conn)
            elif choice == "stats":
                show_stats(conn, cfg)
    except KeyboardInterrupt:
        console.print("\nStopped. Anything already saved stays in memory.")
    finally:
        conn.close()
