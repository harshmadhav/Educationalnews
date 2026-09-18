"""
Educational News Pipeline — Prototype
--------------------------------------
Fetches articles from an RSS feed, rewrites headline + body into a
100-word simple summary using the Claude API, extracts a thumbnail,
and outputs Inshorts-style news cards as JSON.

SETUP (run on your own machine):
    pip install feedparser requests anthropic beautifulsoup4

    export ANTHROPIC_API_KEY="your-key-here"

USAGE:
    python edu_news_pipeline.py
"""

import os
import re
import json
import feedparser
import requests
from bs4 import BeautifulSoup
from anthropic import Anthropic
from difflib import SequenceMatcher

try:
    from nta_scraper import fetch_all_nta_exam_notices
    NTA_SCRAPER_AVAILABLE = True
except ImportError:
    NTA_SCRAPER_AVAILABLE = False

# ---------------------------------------------------------------------
# 1. CONFIG — add/remove feeds per category
# ---------------------------------------------------------------------

RSS_FEEDS = {
    "govt_jobs": "https://news.google.com/rss/search?q=government+job+exam+India&hl=en-IN&gl=IN",
    "competitive_exams": "https://news.google.com/rss/search?q=competitive+exam+India&hl=en-IN&gl=IN",
    "courses": "https://news.google.com/rss/search?q=new+course+institute+India&hl=en-IN&gl=IN",
    "private_jobs": "https://news.google.com/rss/search?q=private+job+hiring+India&hl=en-IN&gl=IN",
}

MAX_ARTICLES_PER_FEED = 5

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------
# 2. FETCH — pull raw articles from each feed
# ---------------------------------------------------------------------

def fetch_articles(feed_url, category, limit=MAX_ARTICLES_PER_FEED):
    parsed = feedparser.parse(feed_url)
    articles = []
    for entry in parsed.entries[:limit]:
        articles.append({
            "category": category,
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "raw_summary": entry.get("summary", ""),
            "published": entry.get("published", ""),
        })
    return articles


# ---------------------------------------------------------------------
# 3. DEDUPLICATION — skip articles that are basically the same story
# ---------------------------------------------------------------------

def normalize_title(title):
    title = title.lower()
    title = re.sub(r"[^a-z0-9\s]", "", title)   # remove punctuation
    title = re.sub(r"\s+", " ", title).strip()  # collapse spaces
    return title


SOURCE_PRIORITY = {
    # Lower number = more trusted. Domains not listed default to 50.
    "nta.nic.in": 1, "ssc.nic.in": 1, "upsc.gov.in": 1, "rrbcdg.gov.in": 1,
    "cbse.gov.in": 1, "ugc.gov.in": 1,
    "prsindia.org": 5, "pib.gov.in": 5,
    "thehindu.com": 10, "indianexpress.com": 10, "livemint.com": 10,
    "timesofindia.indiatimes.com": 15, "hindustantimes.com": 15,
}


def source_rank(url):
    for domain, rank in SOURCE_PRIORITY.items():
        if domain in url:
            return rank
    return 50  # unknown source — lowest priority


def pick_best(group):
    """
    Given a group of near-duplicate articles (same story, different
    sources), pick the one to keep: prefer a trusted/official source,
    then the article with the most content (usually more complete).
    """
    return sorted(
        group,
        key=lambda a: (source_rank(a["link"]), -len(a.get("raw_summary", "")))
    )[0]


def deduplicate_articles(articles):
    """
    Groups near-duplicate articles across all fetched feeds, then keeps
    only the best-sourced version of each story. Runs BEFORE the AI
    rewrite step, to avoid wasting API calls on stories we'll drop anyway.
    """
    groups = []  # list of {"norm_titles": [...], "articles": [...]}

    for article in articles:
        norm = normalize_title(article["title"])
        matched_group = None
        for group in groups:
            if any(SequenceMatcher(None, norm, seen).ratio() >= 0.85
                   for seen in group["norm_titles"]):
                matched_group = group
                break
        if matched_group:
            matched_group["norm_titles"].append(norm)
            matched_group["articles"].append(article)
        else:
            groups.append({"norm_titles": [norm], "articles": [article]})

    unique_articles = [pick_best(g["articles"]) for g in groups]
    duplicate_count = len(articles) - len(unique_articles)

    print(f"Deduplication: {duplicate_count} duplicate(s) removed, "
          f"{len(unique_articles)} unique story/stories kept "
          f"(best source chosen per story).")
    return unique_articles


# ---------------------------------------------------------------------
# 4. THUMBNAIL — try to pull og:image from the article page
# ---------------------------------------------------------------------

def get_thumbnail(url, timeout=5):
    if url.lower().endswith(".pdf"):
        return None  # PDFs never have a preview photo — skip the request
    if "news.google.com" in url:
        # Google News links are redirect pages, not the real article —
        # scraping og:image here is unreliable (sometimes returns
        # Google's own logo instead of a real photo). Skip it and use
        # the category illustration instead until this resolves the
        # actual publisher URL first.
        return None
    try:
        resp = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        soup = BeautifulSoup(resp.text, "html.parser")
        tag = soup.find("meta", property="og:image")
        if tag and tag.get("content"):
            return tag["content"]
    except Exception:
        pass
    return None  # frontend falls back to the category's vector illustration


# ---------------------------------------------------------------------
# 5. AI REWRITE — headline + 100-word body, simple professional tone
# ---------------------------------------------------------------------

REWRITE_PROMPT = """You are a news editor for a student and job-seeker audience.

Rewrite the following news into:
1. A short, clear headline (max 12 words)
2. A summary of exactly around 100 words, in simple professional English

Keep all facts accurate. No opinions. No fluff. Output ONLY valid JSON in
this exact format, nothing else:

{{"headline": "...", "summary": "..."}}

Original title: {title}
Original content: {content}
"""

def rewrite_with_ai(title, raw_text):
    prompt = REWRITE_PROMPT.format(title=title, content=raw_text)
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # fallback: keep original if the model didn't return clean JSON
        return {"headline": title, "summary": raw_text[:400]}


# ---------------------------------------------------------------------
# 6. BUILD CARD — final Inshorts-style structure
# ---------------------------------------------------------------------

def build_card(article):
    ai_result = rewrite_with_ai(article["title"], article["raw_summary"])
    thumbnail = get_thumbnail(article["link"])

    return {
        "category": article["category"],
        "headline": ai_result["headline"],
        "summary": ai_result["summary"],
        "thumbnail_url": thumbnail,
        "source_link": article["link"],
        "published": article["published"],
    }


# ---------------------------------------------------------------------
# 8. RUN PIPELINE
# ---------------------------------------------------------------------

API_URL = os.environ.get("NEWS_API_URL", "http://localhost:8000")

def push_to_api(cards):
    """Sends the finished cards to the backend API (see api.py) so the
    app's live feed picks them up. Falls back gracefully if the API
    isn't running — the JSON file is still saved either way."""
    payload = [
        {
            "category": c["category"],
            "headline": c["headline"],
            "summary": c["summary"],
            "thumbnail_url": c["thumbnail_url"],
            "source_link": c["source_link"],
            "published_at": c["published"],
        }
        for c in cards
    ]
    try:
        resp = requests.post(f"{API_URL}/api/news/bulk", json=payload, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        print(f"Pushed to API: {result['added']} added, "
              f"{result['skipped_duplicates']} already existed.")
    except requests.RequestException as e:
        print(f"Could not reach API at {API_URL} ({e}). "
              f"Cards were still saved to news_cards.json.")


def run_pipeline():
    # Step 1: fetch everything first
    all_articles = []
    for category, feed_url in RSS_FEEDS.items():
        print(f"Fetching: {category} ...")
        all_articles.extend(fetch_articles(feed_url, category))

    # Also pull real notices directly from NTA's exam portals (JEE Main,
    # NEET, CUET, UGC NET, CSIR NET, CMAT, ICAR, NCHM JEE, NIFT, SWAYAM),
    # if the scraper module is present alongside this script.
    if NTA_SCRAPER_AVAILABLE:
        print("Fetching: NTA exam portals ...")
        try:
            all_articles.extend(fetch_all_nta_exam_notices())
        except Exception as e:
            print(f"  NTA scraper failed, continuing without it: {e}")
    else:
        print("nta_scraper.py not found — skipping NTA sources "
              "(place it in the same folder to enable them).")

    # Step 2: remove duplicates across all categories/sources
    unique_articles = deduplicate_articles(all_articles)

    # Step 3: rewrite + build cards only for unique articles
    all_cards = []
    for article in unique_articles:
        try:
            card = build_card(article)
            all_cards.append(card)
            print(f"  Processed: {card['headline']}")
        except Exception as e:
            print(f"  Skipped one article due to error: {e}")

    with open("news_cards.json", "w", encoding="utf-8") as f:
        json.dump(all_cards, f, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(all_cards)} cards to news_cards.json")
    push_to_api(all_cards)


if __name__ == "__main__":
    run_pipeline()
