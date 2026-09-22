"""
Private Jobs Scraper — plugs into the main pipeline
----------------------------------------------------------
Uses Greenhouse's official public Job Board API — not scraping in the
usual sense, since this endpoint is specifically published by Greenhouse
for exactly this purpose (the same one each company's own careers page
calls). No login, no key, no ToS conflict.

    GET https://boards-api.greenhouse.io/v1/boards/{company_slug}/jobs

CONFIRMED WORKING SLUG (verified 2026-09):
    razorpaysoftwareprivatelimited  →  Razorpay
    (found directly in Razorpay's live job posting URLs)

UNVERIFIED — test these yourself, they're guesses based on common
naming patterns, not confirmed:
    Many companies use a shorter/different token than their brand name
    (e.g. it might be "razorpay" for some other company, or include
    "india", "technologies", "pvt-ltd", etc.) There's no public
    directory to look these up — you have to find one real job URL
    from each company's careers page and read the slug out of it,
    the same way we found Razorpay's above.

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
import requests
from bs4 import BeautifulSoup

COMPANY_SLUGS = [
    "razorpaysoftwareprivatelimited",  # Razorpay — confirmed working

    # Unverified guesses — test before trusting, see note above:
    # "meesho", "groww", "cred", "freshworks", "browserstack",
    # "postman", "hasura", "darwinbox", "whatfix",
]

API_BASE = "https://boards-api.greenhouse.io/v1/boards"


def fetch_company_jobs(slug, timeout=15):
    """
    Fetches all open jobs for one company from Greenhouse's public API.
    Returns an empty list (not an error) if the slug is wrong or the
    company doesn't use Greenhouse — this is a normal, expected outcome
    for a guessed slug, not a bug.
    """
    url = f"{API_BASE}/{slug}/jobs?content=true"
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code != 200:
            return []
        data = resp.json()
        return data.get("jobs", [])
    except (requests.RequestException, ValueError):
        return []


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
    rewrite_with_ai() unchanged.

    Unlike Employment News, each job here has its own real, distinct
    URL from Greenhouse — no artificial uniqueness trick needed.
    """
    articles = []
    for slug in COMPANY_SLUGS:
        jobs = fetch_company_jobs(slug)[:limit_per_company]
        for job in jobs:
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
                "published": job.get("updated_at", ""),
            })
    return articles


if __name__ == "__main__":
    # Quick standalone test — run this file directly to see what each
    # company slug actually returns before wiring it into the main
    # pipeline. A slug returning 0 jobs usually just means it's wrong
    # or that company doesn't use Greenhouse — not a script bug.
    print("Testing each company slug in COMPANY_SLUGS:\n")
    total = 0
    for slug in COMPANY_SLUGS:
        jobs = fetch_company_jobs(slug)
        print(f"  {slug}: {len(jobs)} job(s) found")
        total += len(jobs)

    print(f"\nTotal: {total} job(s) across {len(COMPANY_SLUGS)} compan(y/ies)\n")

    results = fetch_private_job_articles()
    for a in results[:10]:  # just show the first 10 so this doesn't flood your terminal
        print(f"- {a['title']}")
        print(f"  {a['raw_summary'][:150]}...")
        print(f"  {a['link']}\n")
