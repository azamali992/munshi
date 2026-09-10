FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src \
    MUNSHI_DATA_DIR=/data MLFLOW_TRACKING_DIR=/data/mlruns MLFLOW_DISABLE_AGENT_HINT=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src ./src
RUN mkdir -p /data && useradd -r -u 10001 munshi && chown munshi /data
USER munshi
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=3s CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz')" || exit 1
CMD ["python3", "-m", "munshi.cli", "serve"]
