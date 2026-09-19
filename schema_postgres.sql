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

    subcategory     TEXT,  -- fine-grained interest tag (e.g. 'engineering_entrance');
                            -- nullable so older rows and edge cases still work

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
    status          TEXT NOT NULL DEFAULT 'draft'
                        CHECK (status IN ('draft', 'published', 'archived')),

    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- If this table already existed from an earlier deploy (created with the
-- old 'published' default), this brings it in line with the new
-- human-review workflow. Safe to run every time — it's a no-op once applied.
ALTER TABLE news_cards ALTER COLUMN status SET DEFAULT 'draft';

-- Fast filtering by category, most-recent-first (the main app query)
CREATE INDEX IF NOT EXISTS idx_news_category_created
    ON news_cards (category, created_at DESC);

-- Fast lookup when checking "have we already stored this story?"
CREATE INDEX IF NOT EXISTS idx_news_source_link
    ON news_cards (source_link);

-- Migration for databases created before subcategory existed — safe to
-- run every startup, does nothing once the column is already there.
ALTER TABLE news_cards ADD COLUMN IF NOT EXISTS subcategory TEXT;

-- Fast filtering by subcategory (the "Customize" feature's main query)
CREATE INDEX IF NOT EXISTS idx_news_subcategory
    ON news_cards (subcategory);


-- ============================================================
-- Optional (add later, not needed for MVP):
--   users            — if you add saved/bookmarked stories
--   user_bookmarks   — many-to-many: user_id <-> news_card id
--   raw_articles     — store pre-AI raw scrape, for debugging/audit
-- ============================================================
