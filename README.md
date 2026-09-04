# SL-ORM Backend — Social Listening & Online Reputation Management

FastAPI backend for the social listening dashboard. It tracks brand mentions across YouTube, Facebook, Instagram, the Play Store, News, and Google Maps, classifies each one's sentiment with an LLM, and posts replies back to the originating platform.

The React frontend that consumes this API lives in its own repository: [Social-and-ORM-FE](https://github.com/silo-prod/Social-and-ORM-FE).

## What it does

- **Collects mentions** on a recurring schedule (every 30 minutes, via APScheduler) from:
  - YouTube (comments and replies on your videos, plus comments across the wider platform matching your keywords)
  - Facebook & Instagram (post/comment mentions, and direct messages)
  - Google Play Store (app reviews)
  - News articles (Google News RSS — no API key required)
  - Google Maps / Business Profile (location reviews)
- **Classifies sentiment** (Positive/Negative/Neutral), a topic label, and a short explanation for every mention using a Groq-hosted LLM.
- **Posts replies** to comments, reviews, and DMs, and saves each sent reply immediately so it appears without waiting for the next fetch cycle.
- **Serves analytics**: sentiment trends over time and KPI summaries across platforms.
- **Creates Google Business Profile store listings** from a dashboard form.
- **Protects every endpoint** behind JWT auth with sliding session expiration.

## Tech stack

FastAPI, SQLAlchemy, PostgreSQL, APScheduler, Groq (via the OpenAI-compatible client), and the Google API client libraries. Tests use pytest against an in-memory SQLite database.

## Project structure

```
main.py                  # FastAPI app & API routes
collector.py             # AI sentiment analysis + shared mention-saving logic
models.py                # SQLAlchemy models, MentionSource platform list
database.py              # DB session/engine setup
auth.py                  # Login + JWT auth
youtube_service.py       # YouTube fetch/reply logic
meta_service.py          # Facebook & Instagram (comments + DMs) fetch/reply logic
playstore_service.py     # Play Store review fetch logic
news_service.py          # News article fetch logic
google_maps_service.py   # Google Business Profile reviews & store listings
requirement.txt          # Python dependencies
tests/                   # pytest suite
```

## Setup

1. Install dependencies: `pip install -r requirement.txt`
2. Copy `.env.example` to `.env` and fill in the values. Each variable is commented in that file.
3. Set up a PostgreSQL database and point `DATABASE_URL` at it.
4. For YouTube, add a Google OAuth `client_secret.json`. See the module docstring in `youtube_service.py` for the one-time interactive login that generates `token.pickle`.
5. For Google Maps, add that integration's own OAuth client credentials, renamed to `maps_client_secrets.json`. Either run an interactive login, which generates `business_token.pickle`, or set `GOOGLE_MAPS_REFRESH_TOKEN` in `.env` for a headless setup. See the module docstring in `google_maps_service.py`.
6. Run the server: `python -m uvicorn main:app --reload`

The API listens on `http://localhost:8000` by default, which is where the frontend expects to find it.

Each integration degrades on its own. Missing credentials for one platform logs a warning and returns no results for that platform. It never blocks startup or affects the others, so you can add platforms one at a time.

## Running tests

```
pytest
```

## Notes

- Secrets and credential files (`.env`, `client_secret.json`, `maps_client_secrets.json`, `token.pickle`, `playstore_key.json`, `business_token.pickle`) are gitignored and must never be committed.
- `requirement.txt` intentionally lists dependencies without pinned versions.
