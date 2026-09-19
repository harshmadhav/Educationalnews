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

from datetime import datetime
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
    allow_methods=["GET", "POST"],
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


class NewsCardOut(NewsCardIn):
    id: int
    source_domain: Optional[str] = None
    status: str
    created_at: datetime


class NewsCardEdit(BaseModel):
    headline: Optional[str] = None
    summary: Optional[str] = None
    thumbnail_url: Optional[str] = None
    subcategory: Optional[str] = None


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
                         is_sponsored, sponsor_name)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (card.category, card.subcategory, card.headline, card.summary,
                     card.thumbnail_url, card.source_link, domain,
                     card.published_at, card.is_original, card.is_sponsored,
                     card.sponsor_name),
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
                             source_link, source_domain, published_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (card.category, card.subcategory, card.headline, card.summary,
                         card.thumbnail_url, card.source_link, domain,
                         card.published_at),
                    )
                    conn.commit()
                    added += 1
                except pg_errors.UniqueViolation:
                    conn.rollback()
                    skipped += 1

    return {"added": added, "skipped_duplicates": skipped}


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
    Only send the fields you want to change."""
    fields = {k: v for k, v in edit.dict().items() if v is not None}
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
