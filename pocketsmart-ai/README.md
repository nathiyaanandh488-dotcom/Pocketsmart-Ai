# PocketSmart AI

Budget-based recommendations for home interiors, parties and jewelry, built with FastAPI, Jinja2 and Google Gemini.

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then edit .env
uvicorn main:app --reload
```

Open http://127.0.0.1:8000, create an account, and pick a planner.

## Get a Gemini API key

1. Go to https://aistudio.google.com/apikey and sign in with your Google account.
2. Click **Create API key** and copy it.
3. Paste it into `.env` as `GEMINI_API_KEY`.

Without a key the app still runs. Plans fall back to built-in rule-based estimates and the results page says so.

## Model name

The project document mentions Gemini 1.5 Flash. Google has retired the 1.5 models, so the model is set in `.env` with `GEMINI_MODEL` (default `gemini-2.5-flash`). Change it there if you want another model.

## Project structure

```
pocketsmart-ai/
  main.py            FastAPI app, routes, auth pages, history
  gemini_utils.py    Prompts, Gemini calls, budget checks, fallbacks
  auth.py            Password hashing, JWT tokens, current-user dependency
  database.py        SQLite tables for users and history
  templates/         Jinja2 pages (one shared results.html for all planners)
  static/            style.css, app.js
```

## Routes

| Route | Purpose |
| --- | --- |
| `/`, `/testimonials` | Public pages |
| `/register`, `/login`, `/logout` | Account pages |
| `/token` | Issue a JWT for API clients (`Authorization: Bearer <token>`) |
| `/session-info`, `/session-data` | Current session details and usage counts (JSON) |
| `/dashboard` | Counts and recent plans |
| `/home-planner`, `/generate-home` | Home interior planner |
| `/party-planner`, `/generate-party` | Party planner |
| `/jewelry-planner`, `/generate-jewelry` | Jewelry planner with optional outfit photo |
| `/history`, `/recommendation/{id}` | Saved plans |
| `/recommendations-details` | JSON of saved plans (`?id=` or `?category=`) |

## How a plan is made

1. The form is validated (budget range, guest count, image type and size).
2. `gemini_utils.py` builds a prompt and asks Gemini for structured JSON, with the outfit image attached for jewelry.
3. If the plan costs more than the budget, Gemini is asked once to correct it.
4. If Gemini fails, or returns no items, rule-based fallback estimates are used.
5. The result is normalised (store links, subtotals, split percentages, over-budget warning) and saved to history.

## Things to know

- **Store links are search links.** Amazon, Flipkart, IKEA, Swiggy and OYO links open a search for the suggested item. Zomato links go to its home page. There is no live product or price API, so prices are Gemini's estimates. Real prices need each store's own API or an affiliate feed.
- **Testimonials are placeholders.** Replace the `TESTIMONIALS` list in `main.py` with real reviews before publishing.
- **Before deploying:** set a long random `SECRET_KEY`, set `COOKIE_SECURE=true` behind HTTPS, and set `ALLOWED_ORIGINS` to your real domain.
