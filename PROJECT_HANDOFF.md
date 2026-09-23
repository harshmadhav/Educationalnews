# StudyBrief — Project Handoff Document

**What this is:** An Inshorts-style educational news app for India, covering
exams, government jobs, private jobs, courses, and general education news.
Content is scraped from multiple sources, rewritten by AI into simple
100-word summaries, human-reviewed, then published to a live public feed.

Owner: Harsh. Repo: `github.com/harshmadhav/Educationalnews`.
Local folder: `C:\Harsh\studybrief-app`.

---

## 1. Architecture — 8 files, how they connect

```
edu_news_pipeline.py     → runs every 3 hrs via GitHub Actions.
                            Fetches from all sources below, dedupes,
                            AI-rewrites, pushes to the live database.

nta_scraper.py            → NTA exam portals (JEE Main, NEET, CUET, UGC NET,
                             CSIR NET, CMAT, ICAR, NCHM JEE, NIFT, SWAYAM).
                             Downloads & extracts text from PDF notices.

employment_news_scraper.py → employmentnews.gov.in "All Jobs" table.
                              Filters out already-expired postings.

private_jobs_scraper.py   → Greenhouse public Job Board API.
                             COMPANY_SLUGS list — currently just Razorpay
                             (razorpaysoftwareprivatelimited), confirmed
                             working. Add more slugs as you find them.

schema_postgres.sql       → Database schema + all migrations (see §2).

api_postgres.py           → FastAPI backend on Render. Public read endpoints
                             + admin-token-protected write endpoints.

admin.html                → Local-only review tool. Approve/reject/edit
                             drafts, "Write News" compose form, search.
                             NOT deployed anywhere — runs off your computer.

index.html                → The actual public-facing app. Published as a
                             Claude artifact for preview; needs real
                             deployment (Vercel/Netlify) to go fully live.
```

**Data flow:** pipeline scrapes → AI rewrites → saves as `draft` →
you review in `admin.html` → approve → appears in `index.html`.

---

## 2. Database (Postgres on Render)

Single table: `news_cards`. Full column list, in the order they were added
(each addition came with an `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`
migration in `schema_postgres.sql`, so re-running the schema is always safe):

| Column | Type | Notes |
|---|---|---|
| `id` | SERIAL PK | |
| `category` | TEXT | `competitive_exams` \| `govt_jobs` \| `private_jobs` \| `courses` \| `news` |
| `subcategory` | TEXT | one of 56 built-in tags, or an admin-added custom one (see `custom_subcategories` table) |
| `is_original` | BOOLEAN | true only for stories written via "Write News" |
| `is_sponsored` | BOOLEAN | affiliate/ad content — always shows an "Ad" badge |
| `sponsor_name` | TEXT | required when `is_sponsored` is true |
| `headline` / `summary` | TEXT | AI-rewritten, ~100 words |
| `thumbnail_url` | TEXT | real photo, YouTube thumbnail, or data-URI upload; null → app shows a vector icon |
| `source_link` | TEXT UNIQUE | **must be unique** — this bit us hard with Employment News (all rows shared one URL); fixed by appending a `#hash` fragment per row |
| `source_domain` | TEXT | auto-derived server-side from `source_link` |
| `published_at` | TEXT | free-text, not a real DATE column (deliberately — feeds give inconsistent formats) |
| `deadline` | DATE | AI-extracted "last date to apply," never guessed |
| `eligibility` / `age_limit` / `application_fee` / `how_to_apply` | TEXT | pre-extracted once by AI, shown as tappable info chips — never generated live per reader (cost control) |
| `state` | TEXT | one of 33 Indian states/UTs, only when story is state-specific; null = central/all-India |
| `status` | TEXT | `draft` \| `published` \| `archived` |
| `created_at` | TIMESTAMPTZ | |

Second table: `custom_subcategories` (category, slug, label) — lets the
admin tool add new sub-interest tags without a code change. Both
`admin.html` and `index.html` fetch this on load and merge it with the
built-in list.

---

## 3. API (`api_postgres.py`, on Render)

**Public (no auth):**
- `GET /api/news` — the live feed. Filters: `category`, `subcategory`, `limit`, `offset`
- `GET /api/subcategories` — merged custom tag list
- `GET /api/health`

**Admin (require `X-Admin-Token` header matching the `ADMIN_TOKEN` env var):**
- `GET /api/admin/news?status=draft|published|archived`
- `POST /api/news` — create (used by pipeline's bulk push, and by "Write News")
- `POST /api/news/bulk` — pipeline's main entry point
- `PATCH /api/admin/news/{id}` — edit any field on any story, any status
- `POST /api/admin/news/{id}/approve` / `/reject` / `/unpublish`
- `POST /api/admin/subcategories` — add a custom tag

**Environment variables on Render:** `DATABASE_URL`, `ADMIN_TOKEN`.

---

## 4. Known bugs already fixed (don't reintroduce these)

1. **CORS only allowed GET/POST** — PATCH requests (Save edits) were
   silently blocked by the browser. Fixed: `allow_methods` now includes
   PATCH and DELETE.
2. **Clearing a field to "None" didn't save** — the edit endpoint used
   `if v is not None` to filter fields, so an explicit null was
   indistinguishable from "don't touch this." Fixed: now uses
   `edit.dict(exclude_unset=True)`.
3. **`created_at` / `deadline` typed as `str` in Pydantic** — Postgres
   returns real `datetime`/`date` objects, causing
   `ResponseValidationError` crashes. Fixed: typed correctly as
   `datetime` / `date`.
4. **Every Employment News story shared one `source_link`** — violated
   the UNIQUE constraint, so only the first-ever row could save; every
   later one silently "already existed." Fixed: append an MD5-hash
   URL fragment per row to make each link unique while still pointing
   to the real page.
5. **Google News thumbnails sometimes showed Google's own logo** —
   scraping `og:image` from a `news.google.com` redirect page doesn't
   reliably resolve to the real article. Fixed: skip thumbnail
   scraping entirely for `news.google.com` links (app shows the vector
   icon instead); same treatment for PDFs.
6. **Greenhouse job descriptions came through double-HTML-encoded**
   (`&lt;div&gt;` instead of `<div>`) — stripping tags once left the
   escaped tags visible as text. Fixed: `html.unescape()` before
   BeautifulSoup parsing.

---

## 5. Design decisions worth knowing

- **AI cost control is a first-class concern.** The eligibility/age/fee/
  how-to-apply chips are extracted **once per story** during the same
  rewrite call that produces the headline/summary — never generated
  live per reader. This was a deliberate rejection of a "live Q&A"
  feature that would have scaled cost with traffic.
- **"News" is a real 5th category**, separate from the four actionable
  ones. The AI is instructed to prefer "News" whenever a story is
  commentary/analysis rather than something with a specific action to
  take — e.g. "why selection rates are low" → News; "SSC CGL 2026
  vacancies" → stays `govt_jobs`.
- **State-priority and implicit personalization are additive, not
  filters.** Nothing is ever hidden — stories are only reordered
  (state match first, then engagement history), so a wrong guess
  never costs the user visibility into something relevant.
- **Sponsored content requires a filled-in advertiser name** before
  the compose form will submit — this was intentional, to make an
  empty/missing disclosure impossible rather than just discouraged.

---

## 6. Deployment

- **Backend:** Render (free tier — spins down after 15 min idle, ~30-50s
  cold start on next request)
- **Database:** Render Postgres (free tier)
- **Scheduler:** GitHub Actions, `.github/workflows/fetch-news.yml`,
  cron `0 */3 * * *` (every 3 hours), plus manual "Run workflow"
- **Secrets needed in GitHub:** `ANTHROPIC_API_KEY`, `EMAIL_USERNAME`,
  `EMAIL_APP_PASSWORD`, `EMAIL_TO` (failure alerts)
- **Repo variable needed:** `NEWS_API_URL` = your Render URL
- **Frontend (`index.html`):** currently only previewed as a Claude
  artifact — **not yet deployed to a real public URL**. Next step:
  Vercel or Netlify (drag-and-drop, both free).

---

## 7. What's built vs. not yet (as of this handoff)

**Fully built:** content pipeline (4 sources), dedup, AI rewrite with
category/subcategory/deadline/chips/state extraction, human review
workflow (draft → approve/reject/unpublish → edit → re-approve),
"Write News" compose tool, sponsored/affiliate content with disclosure,
search (both admin and app), bookmarks, share, "mark as applied,"
deadline badges + calendar export + browser reminders, "new since
last visit," related stories, onboarding, Customize interest filters,
state-level relevance, implicit personalization.

**Recommended but not built** (see earlier chat for full reasoning on
each): UPSC/CBSE/State PSC/AICTE/UGC/RRB/IBPS scrapers, Hindi toggle,
eligibility-based filtering (using the visitor's own profile), font
size control, PWA install, skeleton loading states, report/correction
button, shareable deadline images, offline reading, text-to-speech,
visual deadline calendar, exam comparison tool, application history
timeline.

**Immediate next step suggested:** deploy `index.html` to Vercel/Netlify
so the app has a real public URL, not just a chat preview.

---

## 8. If you're Claude Code picking this up

Read all 8 files listed in §1 in full before making changes — several
depend on exact matching between the pipeline's `SUBCATEGORY_OPTIONS`
dict, `admin.html`'s `SUBCATEGORY_LABELS`, and `index.html`'s copy of
the same. When adding a new field to `news_cards`, the pattern is:
schema migration → `NewsCardIn`/`NewsCardEdit` Pydantic models → both
INSERT statements in `api_postgres.py` → pipeline's AI prompt +
`build_card()` + `push_to_api()` → admin.html form field → index.html
`mapApiCards()` + display. Skipping a step in this chain is the most
common source of "it saved but doesn't show up" bugs in this project's
history.
