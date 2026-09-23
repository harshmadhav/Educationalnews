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

try:
    from employment_news_scraper import fetch_employment_news_articles
    EMPLOYMENT_NEWS_SCRAPER_AVAILABLE = True
except ImportError:
    EMPLOYMENT_NEWS_SCRAPER_AVAILABLE = False

try:
    from private_jobs_scraper import fetch_private_job_articles
    PRIVATE_JOBS_SCRAPER_AVAILABLE = True
except ImportError:
    PRIVATE_JOBS_SCRAPER_AVAILABLE = False

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

def get_thumbnail(url, timeout=8):
    if url.lower().endswith(".pdf"):
        return None  # PDFs never have a preview photo — skip the request

    # Google-branding assets to reject even if found — these show up
    # when a redirect lands on a Google interstitial instead of the
    # real article, and we'd rather show no photo than a wrong one.
    BAD_IMAGE_HOSTS = ("gstatic.com", "www.google.com/images")

    try:
        resp = requests.get(url, timeout=timeout,
                             headers={"User-Agent": "Mozilla/5.0"},
                             allow_redirects=True)
        final_url = resp.url

        if "news.google.com" in final_url:
            # Still on Google's own page after following redirects —
            # it didn't resolve to the real article. Nothing reliable
            # to scrape here.
            return None

        soup = BeautifulSoup(resp.text, "html.parser")

        # Prefer the highest-quality image available: check secure_url
        # first (often a larger/CDN version), then the standard og:image,
        # then Twitter's card image as a last resort.
        candidates = []
        for prop in ("og:image:secure_url", "og:image", "twitter:image"):
            tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
            if tag and tag.get("content"):
                candidates.append(tag["content"])

        for image_url in candidates:
            if not any(bad in image_url for bad in BAD_IMAGE_HOSTS):
                return image_url

    except Exception:
        pass
    return None  # frontend falls back to the category's vector illustration


# ---------------------------------------------------------------------
# 5. AI REWRITE — headline + 100-word body, simple professional tone
# ---------------------------------------------------------------------

# Fine-grained interest tags per category. The AI picks ONE of these per
# story (from the list matching that story's category) so the app can
# offer a "Customize" filter within each broad category.
INDIAN_STATES = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka",
    "Kerala", "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya",
    "Mizoram", "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim",
    "Tamil Nadu", "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand",
    "West Bengal", "Delhi", "Jammu and Kashmir", "Ladakh", "Puducherry",
    "Chandigarh",
]

SUBCATEGORY_OPTIONS = {
    "competitive_exams": [
        "engineering_entrance", "medical_entrance", "pg_research",
        "management_entrance", "law_entrance", "design_entrance",
        "architecture_entrance", "agriculture_entrance", "hospitality_entrance",
        "university_entrance", "civil_services_exam", "teaching_eligibility",
        "school_boards", "state_entrance",
    ],
    "courses": [
        "engineering_course", "medical_course", "it_data_science",
        "management_course", "commerce_finance", "law_course",
        "design_arts", "pure_sciences", "humanities", "agriculture_course",
        "hospitality_course", "teacher_training", "vocational_skills",
        "online_courses",
    ],
    "govt_jobs": [
        "banking_govt", "railways", "ssc", "defence", "police_paramilitary",
        "civil_services_job", "state_psc", "teaching_govt", "psu",
        "judiciary", "postal", "insurance_govt", "healthcare_govt",
        "municipal",
    ],
    "private_jobs": [
        "it_tech", "banking_finance_pvt", "bpo_kpo", "sales_marketing",
        "retail_ecommerce", "manufacturing", "startups",
        "healthcare_pharma", "media_content", "hospitality_pvt",
        "consulting", "internships",
    ],
    "news": [
        "policy_updates", "results_analysis", "career_trends",
        "success_stories", "opinion_commentary", "sector_trends",
    ],
}

REWRITE_PROMPT = """You are a news editor for a student and job-seeker audience.

This story was originally filed under the "{original_category}" category.

Rewrite the following news into:
1. A short, clear headline (max 12 words)
2. A summary of exactly around 100 words, in simple professional English
3. The best category for this story — choose ONE:
   - "{original_category}" — keep it here ONLY if this is a specific,
     actionable notification someone would apply to or act on: a
     particular exam's dates, a specific job vacancy, a course
     admission window, or a results declaration relevant to applicants.
   - "news" — use this instead if the story is general educational or
     career news, analysis, opinion, a human-interest story, or
     commentary that isn't a specific notification to act on (e.g.
     "why selection rates are low", "a coaching culture debate", "a
     candidate's inspiring story"). When in doubt between the two,
     prefer "news" — only use "{original_category}" when there's a
     clear, specific thing to apply for.
4. A subcategory tag — pick EXACTLY ONE value from the list matching
   whichever category you chose in step 3:
   - If you chose "{original_category}": {original_subcategory_options}
   - If you chose "news": {news_subcategory_options}
   If truly nothing fits, use null.
5. A deadline — if the story mentions a specific "last date to apply",
   "closing date", "apply by" date, or similar application/registration
   deadline, extract it as YYYY-MM-DD. If no such deadline is mentioned
   (e.g. it's a results announcement, general news, or a syllabus), use
   null. Never guess a date that isn't explicitly stated.
6. Eligibility — if the story explicitly states an eligibility
   requirement (degree, subject, experience, etc.), summarize it in
   under 15 words. If not stated, use null.
7. Age limit — if the story explicitly states an age requirement,
   give it in under 12 words (e.g. "18-27 years, relaxation for
   reserved categories"). If not stated, use null.
8. Application fee — if the story explicitly states a fee amount,
   give it in under 12 words. If not stated, use null.
9. How to apply — if the story explicitly states an application
   method (a website, portal, or process), summarize it in under 15
   words. If not stated, use null.
10. State — if this is specific to ONE Indian state or UT (e.g. a
    State PSC notice, a state government job, a state board exam),
    give the exact state name from this list: {state_options}
    If it's a central/all-India notice, or doesn't clearly belong to
    one specific state, use null. Never guess.

For 6-9: only extract what's explicitly stated in the text. Never
infer, estimate, or guess a typical value — leave it null if it isn't
actually there. These will be shown to readers as factual, so
accuracy matters more than completeness.

Keep all facts accurate. No opinions. No fluff. Output ONLY valid JSON in
this exact format, nothing else:

{{"headline": "...", "summary": "...", "category": "...", "subcategory": "...", "deadline": "...", "eligibility": "...", "age_limit": "...", "application_fee": "...", "how_to_apply": "...", "state": "..."}}

Original title: {title}
Original content: {content}
"""

def rewrite_with_ai(title, raw_text, category):
    original_options = SUBCATEGORY_OPTIONS.get(category, [])
    news_options = SUBCATEGORY_OPTIONS.get("news", [])
    prompt = REWRITE_PROMPT.format(
        title=title, content=raw_text,
        original_category=category,
        original_subcategory_options=", ".join(original_options),
        news_subcategory_options=", ".join(news_options),
        state_options=", ".join(INDIAN_STATES),
    )
    response = client.messages.create(
        model="claude-sonnet-5",
        # Sonnet 5's tokenizer counts ~30% more tokens than Sonnet 4.6 for
        # the same text, so 500 could cut the JSON off mid-way. This is a
        # ceiling, not a charge — you only pay for tokens actually generated.
        max_tokens=800,
        # Sonnet 5 thinks by default (4.6 didn't). This is a simple
        # extraction task, and thinking tokens are billed at the output
        # rate, so keep it off — same behaviour and cost profile as before.
        thinking={"type": "disabled"},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next((b.text for b in response.content if b.type == "text"), "").strip()
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # fallback: keep original if the model didn't return clean JSON
        return {"headline": title, "summary": raw_text[:400],
                "category": category, "subcategory": None, "deadline": None,
                "eligibility": None, "age_limit": None,
                "application_fee": None, "how_to_apply": None, "state": None}

    # Guard against a hallucinated category — the AI may only choose the
    # story's original category or "news", nothing else.
    if result.get("category") not in (category, "news"):
        result["category"] = category

    # Validate the subcategory against whichever category was actually
    # chosen — better to leave it untagged than store a mismatched tag.
    valid_options = SUBCATEGORY_OPTIONS.get(result["category"], [])
    if result.get("subcategory") not in valid_options:
        result["subcategory"] = None

    # Sanity-check the deadline format — reject anything that isn't a
    # clean YYYY-MM-DD, rather than storing a malformed date.
    deadline = result.get("deadline")
    if deadline and not re.match(r"^\d{4}-\d{2}-\d{2}$", str(deadline)):
        result["deadline"] = None

    # Keep the four info-chip fields short — if the model ignored the
    # word limits, truncate rather than store an oversized value.
    for field, max_len in [("eligibility", 120), ("age_limit", 80),
                            ("application_fee", 80), ("how_to_apply", 120)]:
        value = result.get(field)
        if value and len(str(value)) > max_len:
            result[field] = str(value)[:max_len].rsplit(" ", 1)[0] + "…"

    # Guard against a hallucinated state name — only accept an exact
    # match from the approved list.
    if result.get("state") not in INDIAN_STATES:
        result["state"] = None

    return result


# ---------------------------------------------------------------------
# 6. BUILD CARD — final Inshorts-style structure
# ---------------------------------------------------------------------

def build_card(article):
    ai_result = rewrite_with_ai(article["title"], article["raw_summary"], article["category"])
    thumbnail = get_thumbnail(article["link"])

    return {
        "category": ai_result.get("category", article["category"]),
        "subcategory": ai_result.get("subcategory"),
        "headline": ai_result["headline"],
        "summary": ai_result["summary"],
        "thumbnail_url": thumbnail,
        "source_link": article["link"],
        "published": article["published"],
        "deadline": ai_result.get("deadline"),
        "eligibility": ai_result.get("eligibility"),
        "age_limit": ai_result.get("age_limit"),
        "application_fee": ai_result.get("application_fee"),
        "how_to_apply": ai_result.get("how_to_apply"),
        "state": ai_result.get("state"),
    }


# ---------------------------------------------------------------------
# 8. RUN PIPELINE
# ---------------------------------------------------------------------

API_URL = os.environ.get("NEWS_API_URL", "http://localhost:8000")


def fetch_custom_subcategories():
    """
    Pulls admin-added sub-interest tags from the live API and merges
    them into SUBCATEGORY_OPTIONS, so the AI rewrite step can tag new
    stories with anything added through the admin tool — not just the
    56 built-in tags shipped in this file. Safe to call even if the
    API is unreachable; it just falls back to the built-in list.
    """
    try:
        resp = requests.get(f"{API_URL}/api/subcategories", timeout=10)
        resp.raise_for_status()
        items = resp.json()
        added = 0
        for item in items:
            category, slug = item.get("category"), item.get("slug")
            if category in SUBCATEGORY_OPTIONS and slug and slug not in SUBCATEGORY_OPTIONS[category]:
                SUBCATEGORY_OPTIONS[category].append(slug)
                added += 1
        if added:
            print(f"Loaded {added} admin-added sub-interest tag(s) from the API.")
    except requests.RequestException as e:
        print(f"Could not load custom sub-interest tags ({e}) — using built-in list only.")

def filter_already_stored(articles):
    """
    Drops articles whose link is already in the database (any status),
    BEFORE the AI rewrite step. Each run re-fetches mostly the same
    stories, and without this check every one of them was rewritten
    (and paid for) again, only for the API to reject it as a duplicate.
    If the API is unreachable, keeps everything — same as before.
    """
    links = [a["link"] for a in articles]
    try:
        resp = requests.post(f"{API_URL}/api/news/existing-links", json=links, timeout=60)
        resp.raise_for_status()
        existing = set(resp.json()["existing"])
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"Could not check for already-stored stories ({e}) — "
              f"rewriting all {len(articles)}.")
        return articles

    new_articles = [a for a in articles if a["link"] not in existing]
    print(f"Already stored: {len(articles) - len(new_articles)} skipped, "
          f"{len(new_articles)} new story/stories to rewrite.")
    return new_articles


def push_to_api(cards):
    """Sends the finished cards to the backend API (see api.py) so the
    app's live feed picks them up. Falls back gracefully if the API
    isn't running — the JSON file is still saved either way."""
    payload = [
        {
            "category": c["category"],
            "subcategory": c.get("subcategory"),
            "headline": c["headline"],
            "summary": c["summary"],
            "thumbnail_url": c["thumbnail_url"],
            "source_link": c["source_link"],
            "published_at": c["published"],
            "deadline": c.get("deadline"),
            "eligibility": c.get("eligibility"),
            "age_limit": c.get("age_limit"),
            "application_fee": c.get("application_fee"),
            "how_to_apply": c.get("how_to_apply"),
            "state": c.get("state"),
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
    # Step 0: pick up any sub-interest tags added since the last run
    fetch_custom_subcategories()

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

    # Also pull real job postings from Employment News's Job Highlights
    # table, if the scraper module is present alongside this script.
    if EMPLOYMENT_NEWS_SCRAPER_AVAILABLE:
        print("Fetching: Employment News job highlights ...")
        try:
            en_articles = fetch_employment_news_articles()
            print(f"  Found {len(en_articles)} job highlight(s) from Employment News.")
            all_articles.extend(en_articles)
        except Exception as e:
            print(f"  Employment News scraper failed, continuing without it: {e}")
    else:
        print("employment_news_scraper.py not found — skipping Employment "
              "News (place it in the same folder to enable it).")

    # Also pull private-sector job postings from Greenhouse's public
    # Job Board API (see COMPANY_SLUGS in private_jobs_scraper.py),
    # if the scraper module is present alongside this script.
    if PRIVATE_JOBS_SCRAPER_AVAILABLE:
        print("Fetching: Private jobs (Greenhouse) ...")
        try:
            pj_articles = fetch_private_job_articles()
            print(f"  Found {len(pj_articles)} private job posting(s).")
            all_articles.extend(pj_articles)
        except Exception as e:
            print(f"  Private jobs scraper failed, continuing without it: {e}")
    else:
        print("private_jobs_scraper.py not found — skipping private jobs "
              "(place it in the same folder to enable it).")

    # Step 2: remove duplicates across all categories/sources
    unique_articles = deduplicate_articles(all_articles)

    # Step 2b: skip stories already in the database from earlier runs —
    # the AI rewrite is the only step that costs money, so filter first
    unique_articles = filter_already_stored(unique_articles)

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
