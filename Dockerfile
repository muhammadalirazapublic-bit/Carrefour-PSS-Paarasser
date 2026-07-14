FROM python:3.11-slim

WORKDIR /app

# System deps for pdfplumber's underlying libs (pypdfium2/Pillow wheels
# cover most needs, but keep a minimal toolchain in case a source build
# is ever triggered on an unusual architecture).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY parse_carrefour_po.py app.py ./

# Railway injects PORT at runtime; default to 8080 for local docker run.
ENV PORT=8080
EXPOSE 8080

CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080}"]
