FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi uvicorn anthropic openai rich PyYAML requests python-dotenv
COPY . .
ENV ORIGIN_CONFIG=/app/origin.config.railway.yaml
ENV PYTHONUNBUFFERED=1
CMD ["sh","-c","python -m origin.serve --host 0.0.0.0 --port $PORT --token $ORIGIN_TOKEN"]
