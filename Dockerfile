FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    API_LISTEN_ADDRESS=0.0.0.0 \
    API_PORT=8018 \
    REDDIT_SOURCE_MODE=auto \
    REDDIT_HTTP_BASE_URL=https://www.reddit.com \
    REDDIT_USER_AGENT="docker:quant_reddit:v0.1.0 (by /u/homelabids)" \
    QUANT_DISTILL_URL=http://localhost:8021 \
    QUANT_REDDIT_SUBREDDITS=wallstreetbets,stocks,investing \
    QUANT_REDDIT_INGEST_INTERVAL=300 \
    QUANT_REDDIT_PROCESS_INTERVAL=60 \
    QUANT_REDDIT_POST_BATCH=50 \
    QUANT_REDDIT_COMMENTS_PER_POST=50 \
    QUANT_REDDIT_COMMENT_MIN_SCORE=50 \
    QUANT_REDDIT_COMMENT_MIN_COMMENTS=20 \
    QUANT_REDDIT_POST_MIN_CHARS=800 \
    QUANT_REDDIT_POST_MAX_CHARS=800 \
    QUANT_REDDIT_DISTILL_TIMEOUT=180 \
    QUANT_REDDIT_HTTP_RETRIES=3 \
    QUANT_REDDIT_DEFAULT_PAGE_SIZE=25 \
    QUANT_REDDIT_MAX_PAGE_SIZE=100

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Browser-backed scrape mode requires Chromium + system deps inside the image.
RUN python -m playwright install --with-deps chromium

COPY . .

EXPOSE 8018

# Run migrations, then hand off to supervisord which manages API + ingest/process workers.
CMD ["/bin/sh", "-c", "alembic upgrade head && supervisord -c /app/supervisord.conf -n"]
