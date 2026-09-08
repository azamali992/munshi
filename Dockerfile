FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    MUNSHI_DB=/data/munshi.db MLFLOW_TRACKING_DIR=/data/mlruns

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/manifest.webmanifest')" || exit 1
CMD ["python3", "-m", "uvicorn", "munshi.web.app:app", "--host", "0.0.0.0", "--port", "8000"]
