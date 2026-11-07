# CommercePulse

Sales and inventory intelligence for a small retail operation. CommercePulse imports orders, keeps an auditable stock ledger, calculates SQL reports, and turns recent sales into seven-day demand forecasts and unusual-sales alerts. The interface is a responsive React dashboard backed by a Python API.

## What it demonstrates

- **Relational modeling:** products, orders, order items, and stock movements with unique SKU and order constraints.
- **Reliable imports:** CSV validation before writing, one database transaction for the entire file, duplicate-order detection, and stock checks.
- **SQL analytics:** parameterized joins and aggregations for revenue, gross margin, category mix, daily trends, inventory value, and a window-ranked product leaderboard. See [`backend/app/analytics.py`](backend/app/analytics.py).
- **Practical forecasting:** a Ridge model with trend and weekday features is evaluated against a weekly seasonal baseline on a 14-day holdout. The lower-error approach is used for each product. The UI exposes the selected method and validation MAE.
- **Human-reviewed alerts:** median/MAD-based sales anomaly signals and inventory reorder suggestions. These are decision aids, not automatic purchasing actions.

## Stack

Python 3.11+, FastAPI, SQLAlchemy 2, SQLite by default (PostgreSQL via `DATABASE_URL`), scikit-learn, React, TypeScript, Recharts, and Vite.

## Run locally on Windows

From the `CommercePulse` folder:

```powershell
cd frontend
npm ci
npm run build
cd ..\backend
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:COMMERCEPULSE_DEMO = "1"
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

Open <http://127.0.0.1:8001>. The demo switch seeds fictional orders for the most recent 90 days into a new SQLite database. Seeding is idempotent. To start with an empty workspace, omit `COMMERCEPULSE_DEMO`. The database file is `backend/commercepulse.db` by default.

For frontend development, run `npm run dev` in `frontend` while the API is running on port 8001. Vite proxies `/api` to the backend.

## Docker

```bash
docker compose up --build
```

Open <http://127.0.0.1:8001>. The SQLite database persists in a Docker volume. Docker is optional; the local instructions above work without it.

## Import format

Download the template in the Orders page. Required columns:

```csv
order_id,ordered_on,sku,quantity,unit_price,channel
NEW-1001,2026-11-07,BREW-MINI,2,89.00,Web
```

Use an ISO date (`YYYY-MM-DD`), an existing SKU, positive integer quantity, and a USD price with at most two decimals. Each order may span multiple rows, with one row per SKU. Identical orders can be imported again safely; conflicting reuse of an order ID returns `409`. A failed import rolls back all rows. CSV files are limited to 2 MB or 10,000 lines.

## API

FastAPI's interactive docs are at <http://127.0.0.1:8001/docs>. Key endpoints:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/reports/overview?days=30` | Revenue, margin, daily, category, and product reports |
| `GET /api/products` / `POST /api/products` | Catalog |
| `POST /api/products/{id}/adjust` | Audited stock adjustment |
| `GET /api/products/{id}/movements` | Stock ledger |
| `GET /api/orders` / `POST /api/orders/import` | Recent orders and atomic CSV import |
| `GET /api/forecast` | Seven-day product demand and stock cover |
| `GET /api/alerts` | Inventory and unusual-sales signals |

## Tests

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest -q
```

Tests cover demo reports and signals, idempotent and atomic imports, stock movements, and invalid CSVs.

## Scope and limitations

This is a local portfolio application. It has no authentication or tenant isolation, so do not expose it to the public internet or use real customer data. Currency is USD. Demand forecasts use daily units and a short history; they do not account for promotions, holidays, returns, or supplier constraints. Check predictions and alerts before acting. The seeded data is fictional.
