# SL-ORM Frontend — Social Listening & Online Reputation Management

React dashboard for the social listening app. It displays brand mentions collected from YouTube, Facebook, Instagram, the Play Store, News, and Google Maps, shows their AI-classified sentiment, and lets you reply to any of them from one place.

The FastAPI backend that serves this dashboard lives in its own repository: [Social-and-ORM-BE](https://github.com/silo-prod/Social-and-ORM-BE).

## What it does

- **Mentions feed** with filtering by platform, showing each mention's sentiment, topic label, and AI explanation.
- **Reply inline** to comments, reviews, and DMs. Sent replies appear immediately rather than waiting for the next backend fetch cycle.
- **Sentiment analytics**: trend chart over time plus KPI summary cards across platforms.
- **Google Maps store listings**: a form to create a new Business Profile location, shown when Google Maps is the selected source.
- **Login screen** gating the whole dashboard, using JWT with sliding session expiration.

## Tech stack

React 19, Vite, Tailwind CSS, Recharts, and lucide-react for icons. Tests use vitest with React Testing Library.

## Project structure

```
src/
├── App.jsx                        # Root component, auth gating
├── Dashboard.jsx                  # Main dashboard layout
├── Login.jsx                      # Login screen
├── ErrorBoundary.jsx              # Catches render errors
├── auth.js                        # Token storage and session handling
├── api.js                         # API base URL
└── components/
    ├── MentionsFeed.jsx           # Mentions list, reply UI, store-listing form
    ├── SentimentChart.jsx         # Sentiment trend chart
    └── KPICard.jsx                # Summary stat cards
tests/                             # vitest suite
```

## Setup

1. Install dependencies: `npm install`
2. Start the backend first. See the backend repository for its setup.
3. Run the dev server: `npm run dev`

The dashboard expects the API at `http://localhost:8000`. To point it somewhere else, set `VITE_API_BASE_URL` in a `.env` file at the project root.

## Scripts

```
npm run dev       # start the dev server
npm run build     # production build
npm run preview   # preview the production build
npm run lint      # run eslint
npm test          # run the vitest suite
```
