# Carrefour PO Parser — Web Service

Parses Carrefour Global Sourcing "Operational Purchase Order Confirmation"
PDFs (PSS documents) into structured JSON/CSV. Handles any number of
pages/products — sections are located by content, not fixed page numbers,
and every extraction step degrades gracefully instead of crashing on an
unusual document.

## Files

| File                     | Purpose                                             |
|---------------------------|-----------------------------------------------------|
| `parse_carrefour_po.py`   | Core parser (also runnable standalone as a CLI)     |
| `app.py`                  | FastAPI web service wrapping the parser             |
| `requirements.txt`        | Python dependencies                                 |
| `Dockerfile`               | Container build used by Railway                     |
| `railway.json`            | Railway build/deploy config (points at Dockerfile)  |

## Deploying to Railway

### Option A — Railway CLI
```bash
npm i -g @railway/cli   # if you don't have it
cd carrefour-po-parser
railway login
railway init
railway up
```
Railway auto-detects the `Dockerfile` and builds/deploys it. It also sets
the `PORT` environment variable automatically — the app already reads it
(`app.py` binds via the Dockerfile's `CMD`, defaulting to 8080 locally).

### Option B — Railway dashboard
1. Push this folder to a GitHub repo.
2. In Railway: **New Project → Deploy from GitHub repo**, pick the repo.
3. Railway will detect the `Dockerfile` and deploy automatically.
4. Once deployed, open the generated `*.up.railway.app` domain (or add a
   custom domain under Settings → Networking).

No environment variables are required for basic operation.

### Health check
Railway's `railway.json` already points its health check at `GET /health`.

## API

Interactive docs are available at `/docs` once deployed.

### `POST /parse`
Upload a PDF (multipart form field `file`). Optional query param:
`?format=json|products_csv|shipment_csv|zip` (default `json`).

```bash
curl -F "file=@order.pdf" "https://<your-app>.up.railway.app/parse"

curl -F "file=@order.pdf" \
     "https://<your-app>.up.railway.app/parse?format=zip" \
     -o order_parsed.zip
```

### `POST /parse/zip`
Same as `?format=zip` above — returns a zip with `*.json`,
`*_products.csv`, `*_shipment.csv`, and `*_warnings.log`.

### `GET /health`
Returns `{"status": "healthy"}`. Used by Railway's health check.

## Notes on robustness

- Uploaded PDFs are parsed entirely **in memory** — nothing is written to
  disk, so this is safe on Railway's ephemeral filesystem and needs no
  persistent volume.
- The parser never raises past the API boundary: a malformed or unusual
  PDF returns a partial result plus a `warnings` array rather than a 500.
- Max upload size is capped at 25 MB (`MAX_UPLOAD_BYTES` in `app.py`) —
  raise this if you expect larger scanned/embedded-image PDFs.
- CORS is wide open (`allow_origins=["*"]`) for ease of testing from a
  browser front end. Tighten this in `app.py` before exposing publicly
  if that matters for your use case.
- There's no authentication on the endpoints. If this will be reachable
  publicly, put an API key check in `app.py` (e.g. a `Depends()` that
  validates an `X-API-Key` header against a Railway environment
  variable) before going live.

## Running locally without Docker

```bash
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8080
```

## Running the CLI directly (no server)

```bash
python3 parse_carrefour_po.py order.pdf -o order_parsed
```
