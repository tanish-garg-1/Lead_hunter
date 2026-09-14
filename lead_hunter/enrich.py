"""Visit a business's website to find emails, social links, an owner name, and website problems worth pitching."""
import re
import time
from datetime import date
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (compatible; LeadHunter/1.0; local business research)"
TIMEOUT = 8
SLOW_SECONDS = 4.0
MAX_PAGES = 3
DELAY_SECONDS = 1.0

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
JUNK_EMAIL_RE = re.compile(
    r"\.(png|jpe?g|gif|svg|webp|css|js)$|@(example|domain|email|sentry|wixpress|sentry-next\.wixpress)\.", re.I
)
SOCIAL_HOSTS = {
    "instagram": ("instagram.com",),
    "facebook": ("facebook.com", "fb.com"),
    "whatsapp": ("wa.me", "api.whatsapp.com", "whatsapp.com"),
    "linkedin": ("linkedin.com",),
}
SOCIAL_IGNORE = ("sharer", "share.php", "/p/", "/reel/", "/explore/", "/plugins/", "/dialog/", "/intent/", "/shareArticle")
SOCIAL_ONLY_HOSTS = ("instagram.com", "facebook.com", "fb.com", "linktr.ee", "wa.me", "api.whatsapp.com", "linkedin.com")
BUILDERS = {
    "wixsite.com": "Wix", "wixstatic.com": "Wix", "blogspot.": "Blogger", "weebly.com": "Weebly",
    "godaddysites.com": "GoDaddy Builder", "sites.google.com": "Google Sites", "business.site": "Google Business Site",
    "wordpress.com": "WordPress.com",
}
PAGE_KEYWORDS = ("contact", "about", "team", "story", "founder")
OWNER_HINT_RE = re.compile(r"(?i)founder|owner|proprietor|founded by|managing director|our story")
OWNER_NAME_RE = re.compile(
    r"(?i:founded by|owned by|co-founder|founder|owner|proprietor|managing director)\s*[:,\-–]?\s*"
    r"((?:(?:Dr|Mr|Mrs|Ms|Chef)\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})"
)


def _host(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def _host_matches(host, domains):
    return any(host == d or host.endswith("." + d) for d in domains)


def _clean_url(url):
    return url.split("?")[0].split("#")[0].rstrip("/")


def find_socials(soup, base_url):
    socials = {}
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"].strip())
        host = _host(url)
        if any(marker in url for marker in SOCIAL_IGNORE):
            continue
        for key, hosts in SOCIAL_HOSTS.items():
            if key in socials or not _host_matches(host, hosts):
                continue
            if key == "whatsapp":
                socials[key] = url  # the number lives in the query string
            elif urlparse(url).path.strip("/"):  # a profile, not the platform homepage
                socials[key] = _clean_url(url)
    return socials


def find_emails(soup, text):
    emails = set()
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            emails.add(a["href"][7:].split("?")[0].strip())
    emails.update(EMAIL_RE.findall(text))
    return {e.lower() for e in emails if EMAIL_RE.fullmatch(e) and not JUNK_EMAIL_RE.search(e)}


def quality_issues(soup, html, text, final_url, elapsed):
    issues = []
    if urlparse(final_url).scheme != "https":
        issues.append("No HTTPS")
    if not soup.find("meta", attrs={"name": re.compile(r"^viewport$", re.I)}):
        issues.append("Not mobile-friendly (no viewport)")
    years = [
        int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?(\d{4})", text, re.I)
        if int(y) <= date.today().year
    ]
    if years and max(years) <= date.today().year - 3:
        issues.append(f"Outdated (© {max(years)})")
    if elapsed > SLOW_SECONDS:
        issues.append(f"Slow ({elapsed:.1f}s)")
    lowered = _host(final_url) + " " + html[:200000].lower()
    for marker, builder in BUILDERS.items():
        if marker in lowered:
            issues.append(f"Built on {builder}")
            break
    return issues


def find_owner_name(texts):
    """Returns (name, snippets). snippets is text around owner/founder mentions, for an AI fallback."""
    snippets = []
    for text in texts:
        for match in OWNER_HINT_RE.finditer(text):
            snippets.append(text[max(0, match.start() - 150): match.end() + 150])
    joined = " … ".join(snippets)[:1500]
    found = OWNER_NAME_RE.search(joined)
    return (found.group(1).strip() if found else ""), joined


def _page_links(soup, base_url, already):
    links = []
    for a in soup.find_all("a", href=True):
        url = _clean_url(urljoin(base_url, a["href"]))
        label = (a["href"] + " " + a.get_text(" ")).lower()
        if (_host(url) == _host(base_url) and url not in already and url not in links
                and any(k in label for k in PAGE_KEYWORDS)):
            links.append(url)
    return links


def _robots(session, base_url):
    parser = RobotFileParser()
    try:
        resp = session.get(urljoin(base_url, "/robots.txt"), timeout=TIMEOUT)
        parser.parse(resp.text.splitlines() if resp.status_code == 200 else [])
    except requests.RequestException:
        parser.parse([])
    return parser


def enrich_lead(lead, owner_extractor=None):
    """Fill email, socials, owner_name, has_website and website_issues on the lead dict (in place)."""
    for key in ("email", "instagram", "facebook", "whatsapp", "linkedin", "owner_name", "website_issues"):
        lead[key] = lead.get(key) or ""
    website = (lead.get("website") or "").strip()

    if not website:
        lead["has_website"] = "No"
        lead["website_issues"] = "No website"
        return lead

    if _host_matches(_host(website), SOCIAL_ONLY_HOSTS):
        lead["has_website"] = "Social only"
        lead["website_issues"] = "No real website (social/link page only)"
        for key, hosts in SOCIAL_HOSTS.items():
            if _host_matches(_host(website), hosts) and not lead[key]:
                lead[key] = website
        return lead

    try:
        _crawl(lead, website, owner_extractor)
    except Exception as exc:  # one bad site must never stop the batch
        lead["has_website"] = lead.get("has_website") or "Yes"
        lead["website_issues"] = f"Not checked ({type(exc).__name__})"
    return lead


def _crawl(lead, website, owner_extractor):
    if not website.startswith("http"):
        website = "http://" + website
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    robots = _robots(session, website)

    start = time.monotonic()
    try:
        resp = session.get(website, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        lead["has_website"] = "Down"
        lead["website_issues"] = "Website down / unreachable"
        return
    elapsed = time.monotonic() - start
    if resp.status_code >= 400:
        lead["has_website"] = "Down"
        lead["website_issues"] = f"Website down (HTTP {resp.status_code})"
        return

    lead["has_website"] = "Yes"
    soup = BeautifulSoup(resp.text, "html.parser")
    text = soup.get_text(" ", strip=True)
    issues = quality_issues(soup, resp.text, text, resp.url, elapsed)

    emails = find_emails(soup, text)
    socials = find_socials(soup, resp.url)
    texts = [text]

    visited = {_clean_url(resp.url)}
    extra = _page_links(soup, resp.url, visited) or [urljoin(resp.url, "/contact"), urljoin(resp.url, "/about")]
    for url in extra[: MAX_PAGES - 1]:
        if not robots.can_fetch(USER_AGENT, url):
            continue
        time.sleep(DELAY_SECONDS)
        try:
            page = session.get(url, timeout=TIMEOUT)
        except requests.RequestException:
            continue
        if page.status_code >= 400 or "html" not in page.headers.get("Content-Type", "html"):
            continue
        page_soup = BeautifulSoup(page.text, "html.parser")
        page_text = page_soup.get_text(" ", strip=True)
        emails |= find_emails(page_soup, page_text)
        for key, value in find_socials(page_soup, page.url).items():
            socials.setdefault(key, value)
        texts.append(page_text)

    site_host = _host(resp.url)
    ordered = sorted(emails, key=lambda e: (not e.endswith(site_host), e))
    existing = [e for e in lead["email"].split(", ") if e]
    lead["email"] = ", ".join(dict.fromkeys(existing + ordered))  # keep order, drop repeats
    lead["email"] = ", ".join(lead["email"].split(", ")[:3])
    for key, value in socials.items():
        lead[key] = lead[key] or value

    name, snippets = find_owner_name(texts)
    if not name and snippets and owner_extractor:
        try:
            name = owner_extractor(lead.get("name", ""), snippets)
        except Exception:
            name = ""
    lead["owner_name"] = lead["owner_name"] or name
    lead["website_issues"] = "; ".join(issues)
