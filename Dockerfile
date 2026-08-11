# Origin — hosted server image (Railway / Render / any Docker host).
FROM python:3.12-slim

WORKDIR /app

# Slim server deps only (cloud brain, no native desktop/browser pieces)
COPY requirements-server.txt .
RUN pip install --no-cache-dir -r requirements-server.txt

COPY . .

# Use the cloud-brain config; the app reads $ORIGIN_CONFIG
ENV ORIGIN_CONFIG=/app/origin.config.railway.yaml
ENV PYTHONUNBUFFERED=1

# Railway/Render inject $PORT. Token comes from $ORIGIN_TOKEN (set it in the host).
CMD python -m origin.serve --host 0.0.0.0 --port $PORT --token $ORIGIN_TOKEN
