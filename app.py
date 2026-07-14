"""
Carrefour Operational Purchase Order (PSS) Parser - Web Service
=================================================================

Thin FastAPI wrapper around parse_carrefour_po.py. Uploaded PDFs are
parsed entirely in memory (never written to disk), so this is safe to
run on ephemeral/stateless platforms like Railway.

Endpoints:
  GET  /                 Service info
  GET  /health           Health check (used by Railway)
  POST /parse            Upload a PDF -> JSON result (default)
                          Query param ?format=json|products_csv|shipment_csv|zip
  POST /parse/zip        Upload a PDF -> zip with json + both CSVs

Interactive docs are auto-generated at /docs (Swagger UI) and /redoc.
"""

import csv
import io
import json
import zipfile
from typing import Literal

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from parse_carrefour_po import parse_pdf, write_products_csv, write_shipment_csv

MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB - generous for these multi-page POs

app = FastAPI(
    title="Carrefour PO Parser",
    description="Parses Carrefour Global Sourcing 'Operational Purchase Order Confirmation' PDFs into structured JSON/CSV.",
    version="1.0.0",
)

# Wide-open CORS by default so this can be called from a browser-based
# front end on another domain. Tighten allow_origins for production if
# this API will only ever be called from one known origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    return {
        "service": "carrefour-po-parser",
        "status": "ok",
        "endpoints": {
            "POST /parse": "Upload a PDF (field name 'file'). Optional ?format=json|products_csv|shipment_csv|zip (default json).",
            "GET /health": "Health check",
            "GET /docs": "Interactive API docs",
        },
    }


@app.get("/health")
def health():
    return {"status": "healthy"}


async def _read_upload(file: UploadFile) -> bytes:
    if file.content_type not in (
        "application/pdf", "application/octet-stream", None
    ) and not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Uploaded file must be a PDF.")

    contents = await file.read()
    if not contents:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(contents)} bytes). Max is {MAX_UPLOAD_BYTES} bytes.",
        )
    return contents


@app.post("/parse")
async def parse_endpoint(
    file: UploadFile = File(...),
    format: Literal["json", "products_csv", "shipment_csv", "zip"] = "json",
):
    """
    Upload a Carrefour Operational PO Confirmation PDF and get back
    structured data. This never raises on a malformed/unusual PDF -
    partial results plus a 'warnings' list are returned instead.
    """
    contents = await _read_upload(file)

    result = parse_pdf(io.BytesIO(contents), source_name=file.filename)

    if format == "json":
        return JSONResponse(content=result)

    if format == "products_csv":
        buf = io.StringIO()
        write_products_csv(result, buf)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{_stem(file.filename)}_products.csv"'},
        )

    if format == "shipment_csv":
        buf = io.StringIO()
        write_shipment_csv(result, buf)
        return StreamingResponse(
            iter([buf.getvalue()]),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{_stem(file.filename)}_shipment.csv"'},
        )

    if format == "zip":
        return _zip_response(result, file.filename)

    # Unreachable given the Literal type, but keep a safe fallback.
    return JSONResponse(content=result)


@app.post("/parse/zip")
async def parse_zip_endpoint(file: UploadFile = File(...)):
    """Convenience alias: always returns the zip bundle (json + both CSVs)."""
    contents = await _read_upload(file)
    result = parse_pdf(io.BytesIO(contents), source_name=file.filename)
    return _zip_response(result, file.filename)


def _zip_response(result: dict, filename: str) -> StreamingResponse:
    stem = _stem(filename)

    products_buf = io.StringIO()
    write_products_csv(result, products_buf)

    shipment_buf = io.StringIO()
    write_shipment_csv(result, shipment_buf)

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{stem}.json", json.dumps(result, indent=2, ensure_ascii=False))
        zf.writestr(f"{stem}_products.csv", products_buf.getvalue())
        zf.writestr(f"{stem}_shipment.csv", shipment_buf.getvalue())
        warnings = result.get("warnings") or []
        zf.writestr(f"{stem}_warnings.log", "\n".join(warnings) if warnings else "No warnings.\n")
    zip_buf.seek(0)

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{stem}_parsed.zip"'},
    )


def _stem(filename: str) -> str:
    if not filename:
        return "output"
    name = filename.rsplit("/", 1)[-1]
    if "." in name:
        name = name.rsplit(".", 1)[0]
    return name or "output"
