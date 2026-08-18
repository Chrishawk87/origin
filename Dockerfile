# Origin — hosted server image (Railway / Render / any Docker host).
FROM python:3.12-slim
WORKDIR /app
# Tesseract powers OCR for scanned prequal PDFs/images (compliance_intake).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn python-multipart anthropic openai rich PyYAML requests python-dotenv youtube-transcript-api xhtml2pdf mammoth pymupdf pytesseract pillow
COPY . .
ENV ORIGIN_CONFIG=/app/origin.config.railway.yaml
ENV PYTHONUNBUFFERED=1
# Persist projects/conversations/memory here. Mount a Railway Volume at /data.
ENV ORIGIN_DATA_DIR=/data
# Reads $PORT and $ORIGIN_TOKEN from the host environment (not passed as args,
# so an unset token can't break startup). Token still required for /api access.
CMD ["sh","-c","python -m origin.serve --host 0.0.0.0 --port ${PORT:-8000}"]
