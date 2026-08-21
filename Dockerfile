# Origin — hosted server image (Railway / Render / any Docker host).
FROM python:3.12-slim
WORKDIR /app
# pymupdf reads digital PDFs for compliance_intake. (Scanned-PDF OCR via
# tesseract is a deliberate follow-up — kept out here so the image stays lean
# and boots inside the Railway healthcheck window.)
RUN pip install --no-cache-dir fastapi uvicorn python-multipart anthropic openai rich PyYAML requests python-dotenv youtube-transcript-api xhtml2pdf mammoth pymupdf python-docx
COPY . .
ENV ORIGIN_CONFIG=/app/origin.config.railway.yaml
ENV PYTHONUNBUFFERED=1
# Persist projects/conversations/memory here. Mount a Railway Volume at /data.
ENV ORIGIN_DATA_DIR=/data
# Reads $PORT and $ORIGIN_TOKEN from the host environment (not passed as args,
# so an unset token can't break startup). Token still required for /api access.
CMD ["sh","-c","python -m origin.serve --host 0.0.0.0 --port ${PORT:-8000}"]
