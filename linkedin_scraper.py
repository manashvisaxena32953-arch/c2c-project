"""LinkedIn content-search scraper (Playwright Chromium) + strict filters + recruiters.csv I/O."""
import csv
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from urllib.parse import quote, urlencode

from playwright.sync_api import sync_playwright

import client_config as cfg

log = logging.getLogger(__name__)

RECRUITER_FIELDS = [
    "recruiter_name", "recruiter_email", "role", "post_url",
    "job_description", "email_sent_status", "timestamp",
]

# Selectors are collected here because LinkedIn changes its DOM often.
CARD_SELECTORS = [
    "div.feed-shared-update-v2",
    "div[data-view-name='feed-full-update']",
    "li.reusable-search__result-container",
    "div[role='listitem']",
]
SEE_MORE_SELECTOR = (
    "button.feed-shared-inline-show-more-text__see-more-less-toggle, "
    "button:has-text('…more'), button:has-text('...more'), button:has-text('see more')"
)
MENU_BUTTON = "button[aria-label*='control menu for post']"
COPY_LINK_OPTION = "text=/copy link/i >> visible=true"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

# ------------------------------------------------------------------ compiled filter regexes
NON_USA_RE = re.compile(r"\b(?:" + "|".join(re.escape(x) for x in cfg.NON_USA_INDICATORS) + r")\b", re.I)
_us_pattern = (
    r",\s*(?:" + "|".join(cfg.US_STATE_ABBR) + r")\b"
    r"|\b(?:" + "|".join(re.escape(x) for x in cfg.US_STATE_NAMES + cfg.US_CITIES) + r")\b"
    r"|(?:" + "|".join(re.escape(x) for x in cfg.US_TERMS) + r")"
)
US_RE = re.compile(_us_pattern, re.I)
US_ABBR_RE = re.compile(r"\b(?:US|USA)\b")  # case-sensitive
BENCH_RE = re.compile("|".join(re.escape(p) for p in cfg.BENCH_PHRASES), re.I)
C2C_RE = re.compile(r"\b(?:c2c|c\s*2\s*c|corp[\s-]*to[\s-]*corp)\b", re.I)
NO_C2C_RE = re.compile(
    r"\b(?:no\s+c2c|c2c\s+(?:is\s+)?not\s+(?:allowed|accepted|considered|available)|w-?2\s+only|"
    r"only\s+w-?2|w-?2\s+candidates\s+only|not\s+open\s+to\s+c2c|without\s+c2c)\b", re.I)
DOMAIN_RES = {k: re.compile(v, re.I) for k, v in cfg.DOMAIN_KEYWORDS.items()}
TITLE_RE = re.compile(
    r"^\s*(?:job\s*title|title|role|position|hiring|we\s+are\s+hiring|opening|requirement|job)\s*[:\-–]\s*(.+)$",
    re.I | re.M)
TITLE_WORD_RE = re.compile(r"(developer|engineer|analyst|architect|consultant|tester|lead|manager|administrator|scientist)", re.I)


# ================================================================== CSV helpers (recruiters.csv)
def _ensure_recruiters_file():
    if not cfg.RECRUITERS_CSV.exists():
        write_recruiters([])


def read_recruiters():
    _ensure_recruiters_file()
    with open(cfg.RECRUITERS_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_recruiters(rows):
    tmp = str(cfg.RECRUITERS_CSV) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RECRUITER_FIELDS, extrasaction="ignore", restval="")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, cfg.RECRUITERS_CSV)


def add_recruiters(new_rows):
    """Append rows that are not already stored (same post_url, or same email+role+text hash)."""
    rows = read_recruiters()
    seen_urls = {r["post_url"] for r in rows if r.get("post_url")}
    seen_keys = {(r["recruiter_email"].lower(), r["role"], _h(r["job_description"])) for r in rows}
    added = 0
    for r in new_rows:
        key = (r["recruiter_email"].lower(), r["role"], _h(r["job_description"]))
        if (r.get("post_url") and r["post_url"] in seen_urls) or key in seen_keys:
            continue
        rows.append(r)
        seen_keys.add(key)
        if r.get("post_url"):
            seen_urls.add(r["post_url"])
        added += 1
    write_recruiters(rows)
    return added


def _h(text: str) -> str:
    return hashlib.md5((text or "")[:300].encode("utf-8", "ignore")).hexdigest()


# ================================================================== filters
def build_query(role: str, exclusions=None) -> str:
    """'<Role> + C2C -hotlist -fulltime -"available consultants" -Bench'"""
    exclusions = exclusions or cfg.DEFAULT_EXCLUSION_KEYWORDS
    parts = [f"{role} + C2C"]
    for kw in exclusions:
        parts.append(f'-"{kw}"' if " " in kw else f"-{kw}")
    return " ".join(parts)


def build_search_url(role: str, exclusions=None) -> str:
    params = {
        "keywords": build_query(role, exclusions),
        "origin": "FACETED_SEARCH",
        "datePosted": '"past-24h"',
        "geoUrn": f'["{cfg.US_GEO_URN}"]',
        "sortBy": '"date_posted"',
    }
    return "https://www.linkedin.com/search/results/content/?" + urlencode(params, quote_via=quote)


def is_usa_job(text: str) -> bool:
    if NON_USA_RE.search(re.sub(r"\bNew\s+Mexico\b", "New-Mex", text, flags=re.I)):  # US state, not Mexico
        return False
    if US_RE.search(text) or US_ABBR_RE.search(text):
        return True
    return not cfg.REQUIRE_EXPLICIT_US


def is_bench_post(text: str) -> bool:
    return bool(BENCH_RE.search(text))


def _exclusion_hit(text: str, client: dict) -> bool:
    has_c2c = bool(C2C_RE.search(text))
    for kw in client.get("exclusion_keywords", []):
        k = kw.lower().strip()
        pattern = r"full[\s-]?time" if k in cfg.SOFT_EXCLUSIONS else r"\b" + re.escape(k).replace(r"\ ", r"\s+") + r"s?\b"
        if k in cfg.SOFT_EXCLUSIONS and has_c2c:
            continue  # "Contract C2C / W2 / full time" style posts are still fine
        if re.search(pattern, text, re.I):
            return True
    return False


def matches_strict_filter(text: str, client: dict):
    """Text-level filters. Returns (ok, reason)."""
    if is_bench_post(text):
        return False, "bench_marketing"
    if _exclusion_hit(text, client):
        return False, "excluded_keyword"
    if not is_usa_job(text):
        return False, "non_usa"
    if NO_C2C_RE.search(text) or not C2C_RE.search(text):
        return False, "not_c2c"
    return True, ""


def detect_job_title(text: str) -> str:
    m = TITLE_RE.search(text)
    if m:
        return m.group(1).strip()[:120]
    for line in [l.strip() for l in text.splitlines() if l.strip()][:6]:
        if TITLE_WORD_RE.search(line) and len(line) < 140:
            return line
    return ""


def classify_domains(text: str) -> set:
    return {d for d, rx in DOMAIN_RES.items() if rx.search(text)}


def candidate_domains(client: dict) -> set:
    if client.get("allowed_domains"):
        return set(client["allowed_domains"])
    doms = set()
    for r in client.get("search_roles", []) + [client.get("primary_role", "")]:
        doms |= classify_domains(r)
    specific = doms - cfg.GENERIC_DOMAINS
    return doms if specific else doms | cfg.PROGRAMMING_DOMAINS


def domain_matches(title: str, client: dict) -> bool:
    """False when the declared job title is a different engineering domain (QA, DevOps, ...)."""
    if not title:
        return True
    specific = classify_domains(title) - cfg.GENERIC_DOMAINS
    if not specific:
        return True
    return bool(specific & candidate_domains(client))


# ================================================================== extraction helpers
def extract_emails(text: str, mailtos=()):
    found = []

    def _add(e):
        e = e.strip().strip("<>()[],;:\"'").lower().rstrip(".")
        if (EMAIL_RE.fullmatch(e) and not e.endswith(BAD_EMAIL_SUFFIX) and ".." not in e
                and "linkedin.com" not in e and e not in found):
            found.append(e)

    for m in mailtos:  # mailto: links first
        _add(m.split(":", 1)[-1].split("?")[0])
    for token in re.split(r"\s+", text):  # whitespace-separated tokens
        for m in EMAIL_RE.findall(token):
            _add(m)
    return found


def clean_url(url: str) -> str:
    return url.strip().split("?")[0].rstrip("/") if url else ""


def _recruiter_name(card) -> str:
    try:
        link = card.locator("a[href*='/in/']").first
        raw = link.inner_text(timeout=1500).strip()
        name = raw.splitlines()[0].strip() if raw else ""
        name = re.sub(r"(?i)^view\s+|['’]s\s+profile.*$|\s*[•·].*$|\s+\d(st|nd|rd)\+?$", "", name).strip()
        return name or "Recruiter"
    except Exception:
        return "Recruiter"


def copy_post_url(page, card) -> str:
    """Three-dots -> 'Copy link' -> read clipboard (fallback: activity URN in the card)."""
    try:
        card.evaluate("el => el.scrollIntoView({block: 'center'})")
        page.wait_for_timeout(400)
        page.evaluate("navigator.clipboard.writeText('')")
        card.locator(MENU_BUTTON).first.click(timeout=2500)
        page.wait_for_timeout(500)
        page.locator(COPY_LINK_OPTION).first.click(timeout=2500)
        page.wait_for_timeout(600)
        url = page.evaluate("navigator.clipboard.readText()")
        if url and "linkedin.com" in url:
            return clean_url(url)
    except Exception as e:
        log.debug("copy-link failed: %s", e)
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
    try:  # fallback: URN embedded in the DOM
        urn = card.get_attribute("data-urn", timeout=1500) or card.locator("[data-urn]").first.get_attribute("data-urn", timeout=1500)
        if urn:
            return f"https://www.linkedin.com/feed/update/{urn}"
    except Exception:
        pass
    return ""


def _find_cards(page):
    for sel in CARD_SELECTORS:
        loc = page.locator(sel)
        if loc.count() > 0:
            return loc
    return page.locator(CARD_SELECTORS[0])


def _expand_see_more(page):
    try:
        buttons = page.locator(SEE_MORE_SELECTOR)
        for i in range(min(buttons.count(), 40)):
            try:
                buttons.nth(i).click(timeout=800, force=True)
            except Exception:
                pass
    except Exception:
        pass


# ================================================================== session + scrape
class LinkedInSession:
    """Context manager holding one Playwright browser/context (reused across candidates in batch mode)."""

    def __init__(self, headless: bool = True):
        self.headless = headless

    def __enter__(self):
        if not cfg.SESSION_FILE.exists():
            raise RuntimeError("session.json missing - run the LinkedIn login setup first.")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.launch(headless=self.headless)
        self.ctx = self.browser.new_context(
            storage_state=str(cfg.SESSION_FILE),
            viewport={"width": 1366, "height": 900},
            permissions=["clipboard-read", "clipboard-write"],
        )
        try:
            self.ctx.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.linkedin.com")
        except Exception:
            pass
        self.page = self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.browser.close()
        finally:
            self.pw.stop()


def _record_stats(candidate, role, counters):
    data = {"runs": []}
    if cfg.STATS_JSON.exists():
        try:
            data = json.loads(cfg.STATS_JSON.read_text(encoding="utf-8"))
        except Exception:
            pass
    data["runs"].append({"timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "candidate": candidate, "role": role, **counters})
    cfg.STATS_JSON.write_text(json.dumps(data, indent=2), encoding="utf-8")


def scrape_role(session: LinkedInSession, client: dict, role: str) -> dict:
    page = session.page
    counters = {"scraped": 0, "valid": 0, "rejected": 0, "mismatched": 0, "reasons": {}}

    def reject(reason):
        counters["rejected"] += 1
        counters["reasons"][reason] = counters["reasons"].get(reason, 0) + 1

    url = build_search_url(role, client.get("exclusion_keywords"))
    log.info("[%s] searching: %s", role, build_query(role, client.get("exclusion_keywords")))
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(4000)
    if any(x in page.url for x in ("/login", "/authwall", "/checkpoint")):
        raise RuntimeError("LinkedIn session expired - re-run the login setup.")

    for _ in range(cfg.SCROLL_COUNT):
        _expand_see_more(page)
        page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
        page.wait_for_timeout(2500)
    _expand_see_more(page)

    cards = _find_cards(page)
    total = cards.count()
    log.info("[%s] %d result cards", role, total)
    seen, new_rows = set(), []
    for i in range(total):
        card = cards.nth(i)
        try:
            text = card.inner_text(timeout=4000)
        except Exception:
            continue
        text = re.sub(r"[ \t]+", " ", text).strip()
        if len(text) < 40:
            continue
        sig = _h(text)
        if sig in seen:
            continue
        seen.add(sig)
        counters["scraped"] += 1

        ok, reason = matches_strict_filter(text, client)
        if not ok:
            reject(reason)
            continue
        try:
            mailtos = [a.get_attribute("href") or "" for a in card.locator("a[href^='mailto:']").all()]
        except Exception:
            mailtos = []
        emails = extract_emails(text, mailtos)
        if not emails:
            reject("no_email")
            continue

        title = detect_job_title(text)
        row = {
            "recruiter_name": _recruiter_name(card),
            "recruiter_email": emails[0],
            "role": role,
            "post_url": "",
            "job_description": text[:6000],
            "email_sent_status": "pending",
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if not domain_matches(title, client):
            row["email_sent_status"] = "mismatched_role"
            counters["mismatched"] += 1
            reject("domain_mismatch")
            new_rows.append(row)
            continue
        row["post_url"] = copy_post_url(page, card)
        counters["valid"] += 1
        new_rows.append(row)

    counters["added_to_csv"] = add_recruiters(new_rows)
    _record_stats(client["name"], role, {k: v for k, v in counters.items()})
    log.info("[%s] scraped=%d valid=%d rejected=%d new_rows=%d", role, counters["scraped"],
             counters["valid"], counters["rejected"], counters["added_to_csv"])
    return counters


def scrape_candidate(session: LinkedInSession, client_key: str, roles=None) -> dict:
    client = cfg.get_client(client_key)
    roles = roles or client["search_roles"]
    totals = {"scraped": 0, "valid": 0, "rejected": 0, "added_to_csv": 0}
    for role in roles:
        try:
            c = scrape_role(session, client, role)
            for k in totals:
                totals[k] += c.get(k, 0)
        except RuntimeError:
            raise
        except Exception as e:
            log.error("Scrape failed for role '%s': %s", role, e)
    return totals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    key = sys.argv[1] if len(sys.argv) > 1 else cfg.list_clients()[0]
    with LinkedInSession(headless=True) as s:
        print(scrape_candidate(s, key))
