"""
Employment News Scraper — plugs into the main pipeline
----------------------------------------------------------
Employment News (employmentnews.gov.in) has a dedicated "All Jobs"
listing page — free, no login needed — with columns:
    ISSUED DATE (MM/DD/YYYY) | ORGANISATION | POST |
    METHOD OF APPOINTMENT | LAST DATE (DD/MM/YYYY)

This is a more complete, purpose-built listing than the small "Job
Highlights" snippet on the homepage (which this file used to scrape).
Confirmed working against the real page on 2026-09.

This scraper only returns vacancies whose last date hasn't passed yet
— no point surfacing an expired posting.

SETUP:
    pip install requests beautifulsoup4
"""

import re
from datetime import date
import requests
from bs4 import BeautifulSoup

ALL_JOBS_URL = "https://employmentnews.gov.in/NewEmp/AllJobs.aspx?k=All"

MONTH_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]


def find_jobs_table(url=ALL_JOBS_URL):
    """
    Locates the jobs table by its header row (looking for "ORGANISATION"
    and "LAST DATE" columns, rather than relying on a specific heading
    nearby — more robust if the page layout shifts slightly) and
    returns rows as a list of dicts:
    {issued_date, organisation, post, method, last_date}.
    """
    resp = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")

    target_table = None
    for table in soup.find_all("table"):
        header_text = table.get_text(" ", strip=True).upper()
        if "ORGANISATION" in header_text and "LAST DATE" in header_text:
            target_table = table
            break
    if not target_table:
        return []

    rows = []
    for tr in target_table.find_all("tr"):
        cells = [td.get_text(strip=True) for td in tr.find_all(["td", "th"])]
        if len(cells) < 5 or cells[1].upper() == "ORGANISATION":
            continue  # header row or malformed row
        issued_date, organisation, post, method, last_date = cells[:5]
        rows.append({
            "issued_date": issued_date,
            "organisation": organisation,
            "post": post,
            "method": method,
            "last_date": last_date,
        })
    return rows


def is_still_open(last_date_ddmmyyyy):
    """True if the last date to apply is today or in the future."""
    match = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", last_date_ddmmyyyy)
    if not match:
        return False  # can't parse it — safer to skip than guess
    day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
    try:
        deadline = date(year, month, day)
    except ValueError:
        return False
    return deadline >= date.today()


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


def fetch_employment_news_articles(category="govt_jobs", limit=30):
    """
    Matches the shape of fetch_articles() in the main pipeline script,
    so its output can go straight into deduplicate_articles() and
    rewrite_with_ai() unchanged. Only includes vacancies that are
    still open (last date hasn't passed).
    """
    rows = find_jobs_table()
    open_rows = [r for r in rows if is_still_open(r["last_date"])]

    articles = []
    for row in open_rows[:limit]:
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
            "link": ALL_JOBS_URL,
            "raw_summary": raw_summary,
            "published": "",
        })
    return articles


if __name__ == "__main__":
    # Quick standalone test — run this file directly to see what it
    # finds, before wiring it into the main pipeline.
    all_rows = find_jobs_table()
    open_rows = [r for r in all_rows if is_still_open(r["last_date"])]
    print(f"Found {len(all_rows)} row(s) in the table total, "
          f"{len(open_rows)} still open (not past their last date).\n")

    results = fetch_employment_news_articles()
    for a in results:
        print(f"- {a['title']}")
        print(f"  {a['raw_summary']}\n")

    if not all_rows:
        print("--- DEBUG: table not found, inspecting page ---")
        resp = requests.get(ALL_JOBS_URL, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        print(f"HTTP status: {resp.status_code}")
        text = BeautifulSoup(resp.text, "html.parser").get_text(separator=" ", strip=True)
        print(f"Contains 'ORGANISATION': {'ORGANISATION' in text.upper()}")
        print(f"Contains 'LAST DATE': {'LAST DATE' in text.upper()}")
