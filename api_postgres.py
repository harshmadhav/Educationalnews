"""
Educational News App — Backend API (PostgreSQL version)
----------------------------------------------------------
A small FastAPI service that stores news cards (produced by the pipeline
script) in Postgres and serves them to the frontend.

SETUP:
    pip install -r requirements.txt

ENVIRONMENT:
    DATABASE_URL — your Postgres connection string, e.g.:
    postgresql://user:password@host:5432/dbname
    (Render, Railway, and Supabase all give you this string directly
    when you create a Postgres database — copy it as-is.)

    ADMIN_TOKEN — any secret string you choose. Required on every
    /api/admin/* request (as an X-Admin-Token header) to view, edit,
    approve, or reject draft cards. Set this in your hosting service's
    environment variables — never hardcode it.

RUN:
    uvicorn api:app --reload --port 8000

The table is created automatically on startup, using schema_postgres.sql.
"""

from datetime import datetime, date
import os
from contextlib import contextmanager
from typing import Optional, List
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from psycopg2 import errors as pg_errors
from psycopg2.pool import SimpleConnectionPool
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

DATABASE_URL = os.environ["DATABASE_URL"]  # fails loudly if not set — on purpose
ADMIN_TOKEN = os.environ["ADMIN_TOKEN"]     # required — protects the review endpoints
SCHEMA_PATH = "schema_postgres.sql"

app = FastAPI(title="Educational News API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # lock this down to your real app's domain before going live
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["*"],
)

pool = SimpleConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)


# ---------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------

@contextmanager
def get_db():
    conn = pool.getconn()
    try:
        yield conn
    finally:
        pool.putconn(conn)


def init_db():
    with get_db() as conn, open(SCHEMA_PATH) as f:
        with conn.cursor() as cur:
            cur.execute(f.read())
        conn.commit()


init_db()


# ---------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------

class NewsCardIn(BaseModel):
    category: str
    subcategory: Optional[str] = None
    headline: str
    summary: str
    thumbnail_url: Optional[str] = None
    source_link: str
    published_at: Optional[str] = None
    is_original: bool = False
    is_sponsored: bool = False
    sponsor_name: Optional[str] = None
    deadline: Optional[date] = None  # accepts "2026-10-15" as input,
                                      # serializes correctly either way
    eligibility: Optional[str] = None
    age_limit: Optional[str] = None
    application_fee: Optional[str] = None
    how_to_apply: Optional[str] = None
    state: Optional[str] = None
    source_key: Optional[str] = None  # e.g. "greenhouse:groww" — see sync_open_jobs


class NewsCardOut(NewsCardIn):
    id: int
    source_domain: Optional[str] = None
    status: str
    created_at: datetime


class NewsCardEdit(BaseModel):
    headline: Optional[str] = None
    summary: Optional[str] = None
    thumbnail_url: Optional[str] = None
    category: Optional[str] = None
    subcategory: Optional[str] = None
    deadline: Optional[date] = None
    published_at: Optional[str] = None
    eligibility: Optional[str] = None
    age_limit: Optional[str] = None
    application_fee: Optional[str] = None
    how_to_apply: Optional[str] = None
    state: Optional[str] = None


def verify_admin(x_admin_token: str = Header(...)):
    """Every /api/admin/* route requires this header:
       X-Admin-Token: <your ADMIN_TOKEN value>"""
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid admin token")


# ---------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------

@app.get("/api/news", response_model=List[NewsCardOut])
def list_news(
    category: Optional[str] = Query(
        None, description="competitive_exams | govt_jobs | private_jobs | courses"
    ),
    subcategory: Optional[str] = Query(
        None, description="fine-grained interest tag, e.g. 'engineering_entrance'"
    ),
    limit: int = Query(20, le=100),
    offset: int = 0,
):
    """Main feed endpoint — what the app's card feed calls."""
    query = "SELECT * FROM news_cards WHERE status = 'published'"
    params: list = []

    if category:
        query += " AND category = %s"
        params.append(category)

    if subcategory:
        query += " AND subcategory = %s"
        params.append(subcategory)

    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params += [limit, offset]

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            return cur.fetchall()


@app.get("/api/news/{card_id}", response_model=NewsCardOut)
def get_news_item(card_id: int):
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT * FROM news_cards WHERE id = %s", (card_id,))
            row = cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            return row


@app.post("/api/news", response_model=NewsCardOut)
def add_news_item(card: NewsCardIn):
    """
    Called by the pipeline script after it fetches, deduplicates, and
    AI-rewrites a story. source_link is UNIQUE, so re-submitting the
    same story is safely ignored rather than duplicated.
    """
    domain = urlparse(card.source_link).netloc

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO news_cards
                        (category, subcategory, headline, summary, thumbnail_url,
                         source_link, source_domain, published_at, is_original,
                         is_sponsored, sponsor_name, deadline, eligibility,
                         age_limit, application_fee, how_to_apply, state,
                         source_key)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (card.category, card.subcategory, card.headline, card.summary,
                     card.thumbnail_url, card.source_link, domain,
                     card.published_at, card.is_original, card.is_sponsored,
                     card.sponsor_name, card.deadline, card.eligibility,
                     card.age_limit, card.application_fee, card.how_to_apply,
                     card.state, card.source_key),
                )
                conn.commit()
            except pg_errors.UniqueViolation:
                conn.rollback()
                cur.execute(
                    "SELECT * FROM news_cards WHERE source_link = %s",
                    (card.source_link,),
                )
                return cur.fetchone()

            cur.execute(
                "SELECT * FROM news_cards WHERE source_link = %s",
                (card.source_link,),
            )
            return cur.fetchone()


@app.post("/api/news/bulk")
def add_news_bulk(cards: List[NewsCardIn]):
    """Convenience endpoint: the pipeline script can POST its whole
    news_cards.json list here in one call after each run."""
    added, skipped = 0, 0

    with get_db() as conn:
        with conn.cursor() as cur:
            for card in cards:
                domain = urlparse(card.source_link).netloc
                try:
                    cur.execute(
                        """
                        INSERT INTO news_cards
                            (category, subcategory, headline, summary, thumbnail_url,
                             source_link, source_domain, published_at, deadline,
                             eligibility, age_limit, application_fee, how_to_apply, state,
                             source_key)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (card.category, card.subcategory, card.headline, card.summary,
                         card.thumbnail_url, card.source_link, domain,
                         card.published_at, card.deadline, card.eligibility,
                         card.age_limit, card.application_fee, card.how_to_apply,
                         card.state, card.source_key),
                    )
                    conn.commit()
                    added += 1
                except pg_errors.UniqueViolation:
                    conn.rollback()
                    skipped += 1

    return {"added": added, "skipped_duplicates": skipped}


@app.post("/api/news/existing-links")
def existing_links(links: List[str]):
    """Called by the pipeline BEFORE the AI rewrite step: given a list of
    source_links, returns the ones already stored (in any status — draft,
    published, or archived). The pipeline skips those, so it doesn't pay
    to rewrite a story the database would reject as a duplicate anyway."""
    if len(links) > 1000:
        raise HTTPException(status_code=400, detail="Too many links (max 1000)")
    if not links:
        return {"existing": []}

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT source_link FROM news_cards WHERE source_link = ANY(%s)",
                (links,),
            )
            return {"existing": [row[0] for row in cur.fetchall()]}


class CustomSubcategoryIn(BaseModel):
    category: str
    slug: str
    label: str


@app.get("/api/subcategories")
def list_custom_subcategories():
    """
    Public, read-only: every admin-added sub-interest tag, beyond the
    built-in list shipped in the app's own code. Both admin.html and
    index.html fetch this on load and merge it with their built-in
    list, so a newly added tag appears everywhere without a redeploy.
    """
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT category, slug, label FROM custom_subcategories ORDER BY label")
            return cur.fetchall()


@app.post("/api/admin/subcategories", dependencies=[Depends(verify_admin)])
def add_custom_subcategory(item: CustomSubcategoryIn):
    """Adds a new sub-interest tag for a category. Safe to call more than
    once with the same slug — it just won't create a duplicate."""
    valid_categories = {"competitive_exams", "govt_jobs", "private_jobs", "courses", "news"}
    if item.category not in valid_categories:
        raise HTTPException(status_code=400, detail=f"Invalid category: {item.category}")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO custom_subcategories (category, slug, label)
                VALUES (%s, %s, %s)
                ON CONFLICT (category, slug) DO NOTHING
                """,
                (item.category, item.slug, item.label),
            )
            conn.commit()
    return {"added": True, "category": item.category, "slug": item.slug, "label": item.label}


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------
# Admin / human-review routes — all require the X-Admin-Token header
# ---------------------------------------------------------------------

@app.get("/api/admin/news", response_model=List[NewsCardOut], dependencies=[Depends(verify_admin)])
def admin_list_news(
    status: str = Query("draft", description="draft | published | archived"),
    limit: int = Query(50, le=200),
    offset: int = 0,
):
    """The review queue — cards waiting for a human decision."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT * FROM news_cards
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s OFFSET %s
                """,
                (status, limit, offset),
            )
            return cur.fetchall()


@app.post("/api/admin/news/{card_id}/approve", response_model=NewsCardOut,
          dependencies=[Depends(verify_admin)])
def approve_card(card_id: int):
    """Makes the card visible on the live feed."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE news_cards SET status = 'published' WHERE id = %s RETURNING *",
                (card_id,),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            return row


@app.post("/api/admin/news/{card_id}/unpublish", response_model=NewsCardOut,
          dependencies=[Depends(verify_admin)])
def unpublish_card(card_id: int):
    """Pulls a live card back to draft so it stops showing in the app
    while you edit it. Re-approve it afterward to publish the update."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE news_cards SET status = 'draft' WHERE id = %s RETURNING *",
                (card_id,),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            return row


@app.post("/api/admin/news/{card_id}/reject", response_model=NewsCardOut,
          dependencies=[Depends(verify_admin)])
def reject_card(card_id: int):
    """Keeps the card out of the live feed permanently (kept for records,
    not deleted — deleting would let the same story get re-scraped and
    re-submitted since source_link would no longer be blocked)."""
    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "UPDATE news_cards SET status = 'archived' WHERE id = %s RETURNING *",
                (card_id,),
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            return row


@app.patch("/api/admin/news/{card_id}", response_model=NewsCardOut,
           dependencies=[Depends(verify_admin)])
def edit_card(card_id: int, edit: NewsCardEdit):
    """Fix an AI mistake (wrong date, awkward wording, etc.) before approving.
    Only send the fields you want to change. A field explicitly sent as
    null (e.g. clearing the subcategory to "None") DOES clear it — only
    fields left out of the request entirely are left untouched."""
    fields = edit.dict(exclude_unset=True)
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to update")

    set_clause = ", ".join(f"{k} = %s" for k in fields)
    values = list(fields.values()) + [card_id]

    with get_db() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"UPDATE news_cards SET {set_clause} WHERE id = %s RETURNING *",
                values,
            )
            row = cur.fetchone()
            conn.commit()
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            return row


class OpenJobsIn(BaseModel):
    source_key: str          # one job board, e.g. "greenhouse:groww"
    open_links: List[str]    # EVERY job currently listed on that board


@app.post("/api/admin/news/sync-open-jobs", dependencies=[Depends(verify_admin)])
def sync_open_jobs(boards: List[OpenJobsIn]):
    """
    Called by the pipeline after each run, once per job board it fetched
    successfully. For each board:
      1. tags stored stories whose link is on the board with its
         source_key (so jobs stored before this column existed get
         covered too), then
      2. archives that board's draft/published stories whose link is no
         longer listed — the position has closed.
    The pipeline must only send boards it fetched successfully; a failed
    fetch sent as an empty list would archive every job from that board.
    Archived stories stay visible in the admin tool and can be re-approved.
    """
    tagged, closed = 0, 0
    with get_db() as conn:
        with conn.cursor() as cur:
            for board in boards:
                cur.execute(
                    """
                    UPDATE news_cards SET source_key = %s
                    WHERE source_key IS NULL AND source_link = ANY(%s)
                    """,
                    (board.source_key, board.open_links),
                )
                tagged += cur.rowcount
                cur.execute(
                    """
                    UPDATE news_cards SET status = 'archived'
                    WHERE source_key = %s
                      AND status IN ('draft', 'published')
                      AND NOT (source_link = ANY(%s))
                    """,
                    (board.source_key, board.open_links),
                )
                closed += cur.rowcount
        conn.commit()
    return {"tagged": tagged, "closed": closed}


@app.post("/api/admin/reset-to-draft", dependencies=[Depends(verify_admin)])
def reset_all_to_draft():
    """
    One-time fix: sets every card back to 'draft' and updates the
    column default, in case cards were created before this schema
    change reached the deployed database. Safe to call more than once.
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE news_cards ALTER COLUMN status SET DEFAULT 'draft'")
            cur.execute("UPDATE news_cards SET status = 'draft' WHERE status = 'published'")
            updated = cur.rowcount
            conn.commit()
    return {"reset_to_draft": updated}
