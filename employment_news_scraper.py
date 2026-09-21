"""
Employment News Scraper — plugs into the main pipeline
----------------------------------------------------------
Employment News (employmentnews.gov.in) shows a "JOB HIGHLIGHTS" table
on its homepage — free, no login needed — with columns:
    ORGANISATION | POST | METHOD OF APPOINTMENT | LAST DATE (DD/MM/YYYY)

This scraper reads that table directly. Confirmed working against the
real homepage on 2026-09 — table structure, not free text.

SETUP:
    pip install requests beautifulsoup4
"""

import re
import requests
from bs4 import BeautifulSoup

HOMEPAGE_URL = "https://employmentnews.gov.in/NewEmp/Home.aspx"

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def find_job_highlights_table(url=HOMEPAGE_URL):
    """
    Locates the "JOB HIGHLIGHTS" table on the homepage and returns its
    rows as a list of dicts: {organisation, post, method, last_date}.
    """
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")

    # Find the "JOB HIGHLIGHTS" heading, then the first <table> after it.
    heading = soup.find(string=re.compile(r"JOB\s+HIGHLIGHTS", re.IGNORECASE))
    if not heading:
        return []
    table = heading.find_parent().find_next("table")
    if not table:
        return []

    rows = []
    for tr in table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        # Skip the header row and any malformed rows
        if len(cells) < 4 or cells[0].upper() == "ORGANISATION":
            continue
        organisation, post, method, last_date = cells[0], cells[1], cells[2], cells[3]
        rows.append({
            "organisation": organisation,
            "post": post,
            "method": method,
            "last_date": last_date,
        })
    return rows


def format_deadline_sentence(last_date_ddmmyyyy):
    """Converts '11/10/2026' into a clear sentence the AI rewrite step
    can reliably extract as a deadline, e.g. 'Last date to apply is
    11 October 2026.'"""
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", last_date_ddmmyyyy)
    if not match:
        return ""
    day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
    if not (1 <= month <= 12):
        return ""
    return f"Last date to apply is {day} {MONTH_NAMES[month]} {year}."


def fetch_employment_news_articles(category="govt_jobs", limit=15):
    """
    Matches the shape of fetch_articles() in the main pipeline script,
    so its output can go straight into deduplicate_articles() and
    rewrite_with_ai() unchanged.
    """
    rows = find_job_highlights_table()
    articles = []
    for row in rows[:limit]:
        title = f"{row['organisation']}: {row['post']} Recruitment"
        deadline_sentence = format_deadline_sentence(row["last_date"])
        raw_summary = (
            f"{row['organisation']} is recruiting for the post of {row['post']} "
            f"through {row['method']}. {deadline_sentence} "
            f"Candidates should check the official notification for full "
            f"eligibility criteria before applying."
        )
        articles.append({
            "category": category,
            "title": title,
            "link": HOMEPAGE_URL,
            "raw_summary": raw_summary,
            "published": "",
        })
    return articles


if __name__ == "__main__":
    # Quick standalone test — run this file directly to see what it
    # finds, before wiring it into the main pipeline.
    results = fetch_employment_news_articles()
    print(f"Found {len(results)} job highlight(s):\n")
    for a in results:
        print(f"- {a['title']}")
        print(f"  {a['raw_summary']}\n")

    if not results:
        print("--- DEBUG: table not found, inspecting page ---")
        resp = requests.get(HOMEPAGE_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        print(f"HTTP status: {resp.status_code}")
        text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)
        print(f"Contains 'JOB HIGHLIGHTS': {'JOB HIGHLIGHTS' in text.upper()}")
