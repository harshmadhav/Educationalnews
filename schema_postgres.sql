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

    is_original     BOOLEAN NOT NULL DEFAULT FALSE,  -- true only for stories
                            -- written directly in the app (not scraped)

    is_sponsored    BOOLEAN NOT NULL DEFAULT FALSE,  -- true for affiliate/ad
                            -- cards — must always render with a visible
                            -- "Ad" label per advertising disclosure rules
    sponsor_name    TEXT,   -- brand/advertiser name shown next to "Ad"

    deadline        DATE,   -- last date to apply/appear, when the story
                            -- mentions one — drives the urgency badge

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
ALTER TABLE news_cards ADD COLUMN IF NOT EXISTS is_original BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE news_cards ADD COLUMN IF NOT EXISTS is_sponsored BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE news_cards ADD COLUMN IF NOT EXISTS sponsor_name TEXT;
ALTER TABLE news_cards ADD COLUMN IF NOT EXISTS deadline DATE;

-- Fast filtering by subcategory (the "Customize" feature's main query)
CREATE INDEX IF NOT EXISTS idx_news_subcategory
    ON news_cards (subcategory);


-- Sub-interest tags added from the admin tool, beyond the built-in list
-- shipped in the app's code. Both the review queue and the reader-facing
-- "Customize" filter read from this table (merged with the built-in
-- list) so a newly added tag shows up everywhere immediately.
CREATE TABLE IF NOT EXISTS custom_subcategories (
    id          SERIAL PRIMARY KEY,
    category    TEXT NOT NULL CHECK (
                    category IN (
                        'competitive_exams', 'govt_jobs',
                        'private_jobs', 'courses'
                    )
                ),
    slug        TEXT NOT NULL,
    label       TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (category, slug)
);


-- ============================================================
-- Optional (add later, not needed for MVP):
--   users            — if you add saved/bookmarked stories
--   user_bookmarks   — many-to-many: user_id <-> news_card id
--   raw_articles     — store pre-AI raw scrape, for debugging/audit
-- ============================================================
