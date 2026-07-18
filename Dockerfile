FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY hakchi_sync ./hakchi_sync

ENTRYPOINT ["python", "-m", "hakchi_sync", "--config", "/config/config.yaml"]
