"""
Private Jobs Scraper — plugs into the main pipeline
----------------------------------------------------------
Uses Greenhouse's official public Job Board API — not scraping in the
usual sense, since this endpoint is specifically published by Greenhouse
for exactly this purpose (the same one each company's own careers page
calls). No login, no key, no ToS conflict.

    GET https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs

CONFIRMED WORKING SLUGS: see COMPANY_SLUGS below.
    Many companies use a shorter/different token than their brand name
    (it may include "india", "securities", "privatelimited", etc.), so
    guesses usually fail. TheirStack lists Indian companies that use
    Greenhouse (theirstack.com/en/technology/greenhouse/in) — a good
    source of names to try.

    Tried and NOT on Greenhouse under these names (2026-09): meesho,
    cred, dreamplug, freshworks, browserstack, postman, hasura,
    darwinbox, whatfix, phonepe (board taken down), myntra, dezerv,
    enzene, vymo, quadeye, podiuminfotech, go2andaman, ghx, modmed,
    sila, forbesandcompany, berkadia, shubhashray, veeventures.

    Valid boards with no Indian jobs right now (add if that changes):
    rajasthanroyals, sadaindia, propstack, compassindia (all empty);
    paypay (Japan), georgepjohnsonexperiencemarketing (US),
    careeredge, valpro, trufru (US only).

INDIA-ONLY FILTER:
    Only jobs located in India (or fully remote) are kept — see
    is_india_or_remote(). This runs here, before the AI rewrite, so
    foreign jobs (e.g. AlphaGrep's Shanghai roles) cost zero tokens.

HOW TO ADD A NEW COMPANY:
    1. Google "<company name> careers" and open a specific job listing
    2. If the URL contains greenhouse.io, copy the slug right after
       /boards/ or /job-boards/
    3. Add it to COMPANY_SLUGS below
    4. Run this file directly to confirm it returns real jobs before
       relying on it

SETUP:
    pip install requests beautifulsoup4
"""

import re
import html as html_module
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

COMPANY_SLUGS = [
    # All confirmed working 2026-09. Counts are India/remote jobs at the
    # time; global companies' foreign jobs are dropped by the filter below.
    "razorpaysoftwareprivatelimited",  # Razorpay
    "groww",                           # Groww
    "blenheimchalcotindia",            # Blenheim Chalcot India
    "alphagrepsecurities",             # AlphaGrep Securities (also has China jobs)
    "inmobi",                          # InMobi (~42)
    "glance",                          # Glance (~26)
    "tripactions",                     # Navan, formerly TripActions (~17; mostly US/EU)
    "gravitonresearchcapital",         # Graviton Research Capital (~14)
    "kraftonindia",                    # KRAFTON India (~14)
    "nksecuritiesresearch",            # NK Securities Research (~13)
    "karya",                           # Karya (~13)
    "taboola",                         # Taboola (~10; mostly Israel/US)
    "acurussolutions",                 # Acurus (~7)
    "twinhealth",                      # Twin Health (~6; mostly US)
    "sumologic",                       # Sumo Logic (~5)
    "kulficollective",                 # Kulfi Collective (~4)
    "sonymusicentertainment",          # Sony Music (~3 in Mumbai; mostly US/UK)
    "kimbal",                          # Kimbal (~2)
]

API_BASE = "https://boards-api.greenhouse.io/v1/boards"

INDIA_PLACES = [
    "india",
    # States and union territories
    "andhra pradesh", "arunachal pradesh", "assam", "bihar", "chhattisgarh",
    "goa", "gujarat", "haryana", "himachal pradesh", "jharkhand",
    "karnataka", "kerala", "madhya pradesh", "maharashtra", "manipur",
    "meghalaya", "mizoram", "nagaland", "odisha", "orissa", "punjab",
    "rajasthan", "sikkim", "tamil nadu", "telangana", "tripura",
    "uttar pradesh", "uttarakhand", "west bengal", "jammu", "kashmir",
    "ladakh", "puducherry", "pondicherry", "andaman",
    # Cities (job postings often give only the city)
    "bengaluru", "bangalore", "mumbai", "navi mumbai", "thane", "pune",
    "hyderabad", "secunderabad", "chennai", "delhi", "new delhi", "ncr",
    "gurgaon", "gurugram", "noida", "greater noida", "faridabad",
    "ghaziabad", "kolkata", "ahmedabad", "gandhinagar", "gift city",
    "surat", "vadodara", "rajkot", "jaipur", "udaipur", "jodhpur",
    "kochi", "cochin", "thiruvananthapuram", "trivandrum", "kozhikode",
    "calicut", "coimbatore", "madurai", "tiruchirappalli", "trichy",
    "chandigarh", "mohali", "ludhiana", "amritsar", "indore", "bhopal",
    "mysuru", "mysore", "mangaluru", "mangalore", "hubli", "belgaum",
    "visakhapatnam", "vizag", "vijayawada", "lucknow", "kanpur", "agra",
    "varanasi", "prayagraj", "allahabad", "patna", "ranchi", "jamshedpur",
    "bhubaneswar", "cuttack", "raipur", "nagpur", "nashik", "aurangabad",
    "dehradun", "guwahati", "shimla", "srinagar",
]
# Whole words only, so "Indiana" or "Indianapolis" don't count as India
INDIA_PATTERN = re.compile(r"\b(" + "|".join(INDIA_PLACES) + r")\b")
REMOTE_WORDS = re.compile(r"\b(fully|remote|anywhere|work from home|wfh)\b")


def is_india_or_remote(job):
    """
    Keeps a job if its location or office names an Indian place, or if
    it's plain remote with no other country attached ("Remote" yes,
    "Remote - US" no). Everything else — foreign cities, or vague
    labels like "Multiple Location" with no India office — is dropped.
    """
    location = (job.get("location") or {}).get("name", "")
    offices = " ".join(o.get("name", "") for o in job.get("offices") or [])
    where = f"{location} {offices}".lower()

    if INDIA_PATTERN.search(where):
        return True
    # Remote: whatever's left after removing remote-words and punctuation
    # must be empty — otherwise it names somewhere else (e.g. "remote us")
    loc = location.lower()
    if REMOTE_WORDS.search(loc):
        return not re.sub(r"[^a-z]", "", REMOTE_WORDS.sub("", loc))
    return False


def fetch_company_jobs(slug, timeout=30):
    """
    Fetches all open jobs for one company from Greenhouse's public API.
    Returns None if the request failed (wrong slug, timeout, server
    error) — distinct from [] (board exists, no open jobs). The difference
    matters: an empty board means every stored job from it has closed,
    while a failed fetch tells us nothing.
    """
    url = f"{API_BASE}/{slug}/jobs?content=true"
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return None
        jobs = resp.json().get("jobs")
        return jobs if isinstance(jobs, list) else None
    except (requests.RequestException, ValueError):
        return None


# Only jobs first posted within this many days are sent to the AI. Many
# boards keep roles listed for months; half of the Indian jobs checked in
# 2026-09 were over 2 months old. Older jobs are skipped at zero cost.
MAX_JOB_AGE_DAYS = 30


def job_age_days(job):
    """Days since the job was first posted, or None if unknown.
    Uses first_published — updated_at changes whenever the posting is
    edited, so it would make old jobs look new."""
    stamp = job.get("first_published") or job.get("updated_at")
    if not stamp:
        return None
    try:
        posted = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - posted).days


def html_to_text(html, max_chars=1500):
    """Strips HTML tags from a job's description. Greenhouse sometimes
    returns the content HTML-entity-encoded (e.g. "&lt;div&gt;" instead
    of "<div>"), which would otherwise show up as literal tag text
    instead of being stripped — unescape first, then strip tags."""
    if not html:
        return ""
    text = html_module.unescape(html)
    text = BeautifulSoup(text, "html.parser").get_text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text)
    return text[:max_chars]


def fetch_private_job_articles(category="private_jobs", limit_per_company=20):
    """
    Matches the shape of fetch_articles() in the main pipeline script,
    so its output can go straight into deduplicate_articles() and
    rewrite_all_with_ai() unchanged.

    Unlike Employment News, each job here has its own real, distinct
    URL from Greenhouse — no artificial uniqueness trick needed.

    Returns (articles, open_links_by_board). The second is every job
    currently listed on each board fetched successfully (all locations,
    all ages) — the pipeline uses it to archive stored jobs that have
    since closed. Boards whose fetch failed are left out of it.
    """
    articles = []
    open_links_by_board = {}
    for slug in COMPANY_SLUGS:
        all_jobs = fetch_company_jobs(slug)
        if all_jobs is None:
            print(f"  {slug}: fetch failed — skipped this run")
            continue
        source_key = f"greenhouse:{slug}"
        open_links_by_board[source_key] = [
            j["absolute_url"] for j in all_jobs if j.get("absolute_url")
        ]

        # Filter BEFORE the per-company limit, so the limit's slots go to
        # recent Indian jobs rather than being used up by foreign/old ones
        indian = [j for j in all_jobs if is_india_or_remote(j)]
        jobs = [j for j in indian
                if (job_age_days(j) is None or job_age_days(j) <= MAX_JOB_AGE_DAYS)]
        skipped = []
        if len(indian) < len(all_jobs):
            skipped.append(f"{len(all_jobs) - len(indian)} outside India")
        if len(jobs) < len(indian):
            skipped.append(f"{len(indian) - len(jobs)} older than {MAX_JOB_AGE_DAYS} days")
        if skipped:
            print(f"  {slug}: skipped {', '.join(skipped)}")
        for job in jobs[:limit_per_company]:
            title = job.get("title", "").strip()
            location = (job.get("location") or {}).get("name", "")
            link = job.get("absolute_url", "")
            if not title or not link:
                continue

            raw_summary = html_to_text(job.get("content", ""))
            if not raw_summary:
                raw_summary = f"{title} — an open position at this company."
            if location:
                raw_summary = f"Location: {location}. {raw_summary}"

            articles.append({
                "category": category,
                "title": f"{title} — {location}" if location else title,
                "link": link,
                "raw_summary": raw_summary,
                # first_published, not updated_at: editing a posting
                # bumps updated_at and would make an old job look new
                "published": job.get("first_published") or job.get("updated_at", ""),
                "source_key": source_key,
            })
    return articles, open_links_by_board


if __name__ == "__main__":
    # Quick standalone test — run this file directly to see what each
    # company slug actually returns before wiring it into the main
    # pipeline. A slug returning 0 jobs usually just means it's wrong
    # or that company doesn't use Greenhouse — not a script bug.
    print("Testing each company slug in COMPANY_SLUGS:\n")
    total = 0
    for slug in COMPANY_SLUGS:
        jobs = fetch_company_jobs(slug)
        print(f"  {slug}: {'FETCH FAILED' if jobs is None else f'{len(jobs)} job(s) found'}")
        total += len(jobs or [])

    print(f"\nTotal: {total} job(s) across {len(COMPANY_SLUGS)} compan(y/ies)\n")

    results, _ = fetch_private_job_articles()
    for a in results[:10]:  # just show the first 10 so this doesn't flood your terminal
        print(f"- {a['title']}")
        print(f"  {a['raw_summary'][:150]}...")
        print(f"  {a['link']}\n")
