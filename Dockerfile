FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends supervisor \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8018

# Run migrations, then hand off to supervisord which manages the API + worker.
CMD ["/bin/sh", "-c", "alembic upgrade head && supervisord -c /app/supervisord.conf -n"]
