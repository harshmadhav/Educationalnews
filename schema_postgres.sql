-- ============================================================
-- Educational News App — Database Schema (PostgreSQL)
-- Works with Render, Railway, or Supabase's managed Postgres.
-- ============================================================

CREATE TABLE IF NOT EXISTS news_cards (
    id              SERIAL PRIMARY KEY,

    category        TEXT NOT NULL CHECK (
                        category IN (
                            'competitive_exams',
                            'govt_jobs',
                            'private_jobs',
                            'courses'
                        )
                    ),

    headline        TEXT NOT NULL,
    summary         TEXT NOT NULL,          -- AI-rewritten, ~100 words

    thumbnail_url   TEXT,                   -- nullable; frontend falls back
                                             -- to a category placeholder

    source_link     TEXT NOT NULL UNIQUE,   -- prevents duplicate storage
                                             -- at the DB level too
    source_domain   TEXT,                   -- e.g. "ssc.nic.in" — used for
                                             -- source-ranking / filtering

    published_at    TEXT,                   -- original publish date, as
                                             -- text from the source feed
    status          TEXT NOT NULL DEFAULT 'published'
                        CHECK (status IN ('draft', 'published', 'archived')),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Fast filtering by category, most-recent-first (the main app query)
CREATE INDEX IF NOT EXISTS idx_news_category_created
    ON news_cards (category, created_at DESC);

-- Fast lookup when checking "have we already stored this story?"
CREATE INDEX IF NOT EXISTS idx_news_source_link
    ON news_cards (source_link);


-- ============================================================
-- Optional (add later, not needed for MVP):
--   users            — if you add saved/bookmarked stories
--   user_bookmarks   — many-to-many: user_id <-> news_card id
--   raw_articles     — store pre-AI raw scrape, for debugging/audit
-- ============================================================
