FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ghdigest/ ./ghdigest/

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Taipei \
    DB_PATH=/data/ghdigest.db
EXPOSE 8080
USER 1000
ENTRYPOINT ["python", "-m", "ghdigest"]
