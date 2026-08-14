# rs3graph_webui

A full-stack replacement for the static matplotlib graph generator in
[`rs3graph`](../rs3graph). It fetches 24 hours of RuneScape 3 price/volume
data from the [RS Wiki Prices API](https://prices.runescape.wiki/api/v2/rs)
and renders interactive, dark-themed charts in the browser.

- **Backend:** Python FastAPI + SQLite (single file, zero config)
- **Frontend:** vanilla HTML/CSS/JS + [Chart.js](https://www.chart.js.org) (CDN)
- **Deployment:** Dockerfile + docker-compose.yml, ready for a VPS

## Features

### Market Watch (`/`)
- Dark theme matching the original script: background `#343434`, buy/high
  line `#2ff259`, sell/low line `#f2802f`, text/grid `#ababab`
- Market dropdown + Refresh button (background job with live progress —
  safe for markets with hundreds of items; the API is called with a 0.5 s
  delay between requests)
- One chart card per item: icon + name, stats bar (latest buy, latest sell,
  total 24h volume), price chart, volume chart
- Continuous price lines across missing data (forward-fill then back-fill
  leading gaps — same logic as `generate_graphs.py`)
- Data-point dots **only** at indices where real data exists
- Volume bars 5 minutes wide: buy = positive green, sell = negative orange
- Time x-axis in **EST**; tooltips show buy/sell price + buy/sell volume,
  with "(N Minutes Ago)" / "(N Hours Ago)" when a value is missing
- Lazy rendering via `IntersectionObserver` (charts are created when a card
  scrolls near the viewport and destroyed when it leaves — important for
  markets with ~300 items)

### Admin (`/admin`)
- Create, rename, delete markets
- Click a market to edit its item list
- Add items by RS Wiki item ID with live name/icon lookup from `/mapping`
- Remove items from a market
- First run seeds a **Default** market with the 3 IDs from `items.txt`
  (`4151`, `49430`, `1519`)

## Quick start (Docker)

```bash
cd rs3graph_webui
cp .env_sample .env   # optional: customize your User-Agent
docker compose up -d --build
# open http://localhost:8000  (admin: http://localhost:8000/admin)
```

`./data` (SQLite DB) and `./icons` (cached icons) are mounted as volumes so
they survive container rebuilds.

## Configuration (.env)

A sample `.env_sample` file ships with the repo. Copy it to `.env` to customize:

```bash
cp .env_sample .env
```

Then edit `.env` as needed:

```
# RS3 Graph WebUI configuration
# Copy this file to .env and customize values as needed.
# The User-Agent string is sent with every API request to the RS Wiki.
# The RS Wiki API requires a descriptive User-Agent per their guidelines.
# Format: project-name/version (contact info; purpose)
# Example: my-bot/1.0 (contact@example.com; personal price tracker)
RS3GRAPH_USER_AGENT=rs3graph-webui/1.0 (RuneScape price tracker; contact: local)
```

- **`RS3GRAPH_USER_AGENT`** — the User-Agent string sent with every request to
the RS Wiki Prices API. The default is polite and works out of the box; set it
to your own project name plus contact info so the wiki admins can reach you if
needed. If the variable is unset, the app falls back to a generic but polite
default (`rs3graph-webui/1.0 (RuneScape price tracker)`).
- The `.env` file is loaded automatically at startup (via `python-dotenv`).

## Local development

```bash
cd rs3graph_webui
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Then open <http://localhost:8000>.

## How it works

- **Data:** `POST /api/markets/{id}/refresh` starts a background job that
  fetches `/timeseries?lookback=24h&id={item_id}` for every item in the
  market (0.5 s apart), replaces the stored 24h window in SQLite, and prunes
  anything older than 24 h. Progress is polled via `GET /api/jobs/{job_id}`.
- **Icons:** cached in `icons/`, downloaded from
  `https://runescape.wiki/images/{icon_name}` (spaces → underscores) on
  demand.
- **Chart data:** `GET /api/markets/{id}/items/{iid}/data` returns
  gap-filled price arrays, zero-filled volume arrays, and boolean "real
  data" masks so the frontend can place markers and annotate tooltips
  exactly like the Python version.

### Database

`data/rs3graph.db` (auto-created). Schema per spec: `markets`,
`market_items`, `price_data`, plus a small `meta` table storing per-market
"last refreshed" timestamps for the UI.

### API endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/markets` | List markets (with item counts + last refresh) |
| POST | `/api/markets` | Create market `{name}` |
| PUT | `/api/markets/{id}` | Rename market `{name}` |
| DELETE | `/api/markets/{id}` | Delete market + orphaned price data |
| GET | `/api/markets/{id}/items` | List items in a market |
| POST | `/api/markets/{id}/items` | Add item `{item_id}` (live wiki lookup) |
| DELETE | `/api/markets/{id}/items/{iid}` | Remove item |
| GET | `/api/markets/{id}/items/{iid}/data` | Chart-ready series + stats |
| POST | `/api/markets/{id}/refresh` | Start background refresh, returns `{job_id}` |
| GET | `/api/jobs/{job_id}` | Refresh job status |
| GET | `/api/lookup/{item_id}` | Name/icon lookup from `/mapping` |
| GET | `/icons/{filename}` | Cached icon (downloaded on demand) |

## Notes / etiquette

- All API calls send a `User-Agent` and are spaced 0.5 s apart, per the RS
  Wiki API guidelines.
- Run uvicorn with a **single worker** (the Dockerfile does this): the
  refresh-job registry and SQLite connection are in-process.

## File structure

```
rs3graph_webui/
├── app/
│   ├── main.py              # FastAPI app, routes, refresh jobs, seeding
│   ├── db.py                # SQLite setup + helpers (schema per spec)
│   ├── api_client.py        # RS Wiki API fetching + icon cache
│   ├── models.py            # Pydantic models
│   └── static/
│       ├── index.html       # Market watch UI
│       ├── admin.html       # Admin panel
│       ├── style.css        # Dark theme styles
│       └── app.js           # Chart.js frontend logic
├── icons/                   # Cached icons (persistent volume)
├── data/
│   └── rs3graph.db          # SQLite file (auto-created)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── README.md
```
