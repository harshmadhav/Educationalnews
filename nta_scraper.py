"""
NTA Notice Scraper — plugs into the main pipeline
----------------------------------------------------
NTA (nta.ac.in) publishes notices as a table of PDF links, not as
articles or an RSS feed. This scraper:
  1. Loads the notices page
  2. Finds each row's title + PDF link
  3. Downloads new PDFs and extracts their text
  4. Returns them in the same shape as fetch_articles() in the main
     pipeline, so they can go straight into deduplicate_articles()
     and rewrite_with_ai() unchanged.

SETUP:
    pip install requests beautifulsoup4 pdfplumber

IMPORTANT — READ THIS FIRST:
    NTA's exam portals (jeemain.nta.nic.in, neet.nta.nic.in, etc.) are
    modern web apps — some load their notice board with JavaScript,
    which plain requests + BeautifulSoup cannot see (it only reads
    the raw HTML, not what JavaScript adds afterward).

    Run this file directly first:
        python nta_scraper.py

    Check the printed count per source:
    - A real number (e.g. "JEE Main: found 5 PDF link(s)") means it
      worked — that source is good to go.
    - "found 0 PDF link(s)" for a source usually means that page needs
      JavaScript to load its content. Tell me which ones show 0 and
      I'll switch those specific sources to a headless-browser method
      (Selenium/Playwright) instead — that's a different approach, so
      it's worth confirming which sources actually need it rather than
      building it for all ten upfront.
"""

import re
import io
import requests
from bs4 import BeautifulSoup
import pdfplumber

NTA_NOTICES_URL = "https://nta.ac.in/Download"  # <-- confirm/replace this

# Each NTA exam runs on its own subdomain but the same underlying
# template, so one scraper function covers all of them — just loop
# through this list. Categorized per your app's four categories.
NTA_EXAM_SOURCES = [
    {"name": "JEE Main",    "url": "https://jeemain.nta.nic.in/",      "category": "competitive_exams"},
    {"name": "NEET",        "url": "https://neet.nta.nic.in/",         "category": "competitive_exams"},
    {"name": "CUET",        "url": "https://cuet.nta.nic.in/",         "category": "competitive_exams"},
    {"name": "UGC NET",     "url": "https://ugcnet.nta.nic.in/",       "category": "competitive_exams"},
    {"name": "CSIR NET",    "url": "https://csirnet.nta.nic.in/",      "category": "competitive_exams"},
    {"name": "CMAT",        "url": "https://cmat.nta.nic.in/",         "category": "competitive_exams"},
    {"name": "ICAR",        "url": "https://exams.nta.nic.in/icar/",   "category": "competitive_exams"},
    {"name": "NCHM JEE",    "url": "https://exams.nta.nic.in/nchm-jee/", "category": "competitive_exams"},
    {"name": "NIFT",        "url": "https://exams.nta.nic.in/niftee/", "category": "competitive_exams"},
    {"name": "SWAYAM",      "url": "https://exams.nta.nic.in/swayam/", "category": "courses"},
]


def find_notice_links(listing_url=NTA_NOTICES_URL, limit=10):
    """
    Scans the notices page for links ending in .pdf, using the link
    text as the notice title. Returns a list of {"title", "pdf_url"}.
    Skips static reference documents (syllabus, FAQ, brochures) that
    don't change often and aren't really "news".
    """
    resp = requests.get(listing_url, timeout=30,
                         headers={"User-Agent": "Mozilla/5.0"})
    soup = BeautifulSoup(resp.text, "html.parser")

    # Titles that are reference material, not time-sensitive updates —
    # skip these so the same "story" doesn't reappear every run.
    SKIP_TITLES = {
        "syllabus", "information bulletin", "faq", "brochure",
        "user manual", "how to apply", "instructions",
    }
    # Question-paper archives look like "<Subject> <date> Shift 1" —
    # skip anything containing "shift" followed by a number.
    SHIFT_PATTERN = re.compile(r"shift\s*\d", re.IGNORECASE)

    notices = []
    for link in soup.find_all("a", href=True):
        href = link["href"]
        if href.lower().endswith(".pdf"):
            title = link.get_text(strip=True)
            if not title:
                continue  # skip icon-only links with no title text
            if any(skip in title.strip().lower() for skip in SKIP_TITLES):
                continue  # skip static reference documents
            if SHIFT_PATTERN.search(title):
                continue  # skip exam-paper archives (e.g. "Shift 1")
            full_url = href if href.startswith("http") else \
                requests.compat.urljoin(listing_url, href)
            notices.append({"title": title, "pdf_url": full_url})
        if len(notices) >= limit:
            break

    return notices


def extract_notice_date(text):
    """
    NTA notices usually print their date somewhere near the top —
    e.g. "Dated: 15.09.2026", "15/09/2026", or "15th September, 2026".
    Scans the first part of the extracted text and returns an ISO
    date string (YYYY-MM-DDT00:00:00Z) if found, else None.
    """
    MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
        "june": 6, "july": 7, "august": 8, "september": 9,
        "october": 10, "november": 11, "december": 12,
    }
    search_area = text[:600]  # the date is almost always near the start

    # Pattern 1: numeric dd.mm.yyyy / dd-mm-yyyy / dd/mm/yyyy
    match = re.search(r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{4})\b", search_area)
    if match:
        day, month, year = (int(g) for g in match.groups())
        if 1 <= day <= 31 and 1 <= month <= 12 and 2000 <= year <= 2100:
            return f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z"

    # Pattern 2: "15th September, 2026" or "15 September 2026"
    match = re.search(
        r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|"
        r"July|August|September|October|November|December)[,]?\s+(\d{4})\b",
        search_area, re.IGNORECASE,
    )
    if match:
        day = int(match.group(1))
        month = MONTHS[match.group(2).lower()]
        year = int(match.group(3))
        return f"{year:04d}-{month:02d}-{day:02d}T00:00:00Z"

    return None


def extract_pdf_text(pdf_url, max_chars=3000):
    """Downloads a PDF and pulls out its text (first few pages only —
    enough context for the AI to summarize, without wasting tokens)."""
    resp = requests.get(pdf_url, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"})
    text = ""
    with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
        for page in pdf.pages[:3]:  # first 3 pages is usually enough
            page_text = page.extract_text() or ""
            text += page_text + "\n"
            if len(text) >= max_chars:
                break

    text = re.sub(r"\s+", " ", text).strip()
    # Some NTA PDFs embed Hindi text with fonts that don't map cleanly,
    # producing junk like "(cid:452)" instead of real characters.
    # Strip that out — the English portions extract fine either way.
    text = re.sub(r"\(cid:\d+\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]



def fetch_nta_articles(category="govt_jobs", limit=10):
    """
    Fetches notices from a single NTA page (kept for the nta.ac.in
    general notices page). Matches the shape of fetch_articles() in
    the main pipeline script.
    """
    notices = find_notice_links(limit=limit)
    return _pdfs_to_articles(notices, category)


def fetch_all_nta_exam_notices(limit_per_source=5):
    """
    Loops through every exam portal in NTA_EXAM_SOURCES and collects
    their notices. Use this to cover JEE Main, NEET, CUET, UGC NET,
    CSIR NET, CMAT, ICAR, NCHM JEE, NIFT, and SWAYAM in one call.
    Prints a per-source count so you can see which ones actually
    returned notices (0 usually means that page loads its content
    with JavaScript, which plain requests can't see — flag it and
    we'll switch that one to a headless-browser approach).
    """
    all_articles = []
    for source in NTA_EXAM_SOURCES:
        # Retry once — some NTA subdomains are just slow, not broken.
        for attempt in range(2):
            try:
                notices = find_notice_links(listing_url=source["url"], limit=limit_per_source)
                print(f"{source['name']}: found {len(notices)} PDF link(s)")
                articles = _pdfs_to_articles(notices, source["category"])
                all_articles.extend(articles)
                break
            except Exception as e:
                if attempt == 0:
                    print(f"{source['name']}: timed out, retrying once...")
                    continue
                print(f"{source['name']}: failed to load ({e})")
    return all_articles


def _pdfs_to_articles(notices, category):
    articles = []
    for notice in notices:
        try:
            pdf_text = extract_pdf_text(notice["pdf_url"])
            if not pdf_text:
                continue  # scanned/image-only PDF, no extractable text
            if "question paper" in pdf_text.lower()[:200]:
                continue  # this is an exam paper archive, not a news notice
            articles.append({
                "category": category,
                "title": notice["title"],
                "link": notice["pdf_url"],
                "raw_summary": pdf_text,
                "published": extract_notice_date(pdf_text) or "",  # from
                                   # the PDF itself when found; otherwise
                                   # left blank (the app falls back to
                                   # "Added on" using its own timestamp)
            })
        except Exception as e:
            print(f"  Skipped notice '{notice['title']}': {e}")
    return articles


if __name__ == "__main__":
    # Quick standalone test — run this file directly to see what each
    # source finds, before wiring it into the main pipeline.
    results = fetch_all_nta_exam_notices()
    print(f"\nTotal: {len(results)} notice(s) with extractable text\n")
    for a in results:
        print(f"[{a['category']}] {a['title']}")
        print(f"  {a['raw_summary'][:150]}...\n")
