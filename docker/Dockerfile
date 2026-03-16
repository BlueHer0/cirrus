FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=cirrus.settings

WORKDIR /app

# System deps for Playwright + lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libxml2-dev \
    libxslt1-dev \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Playwright browser
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.browsers
RUN playwright install chromium --with-deps

# App code
COPY . .

# Collect static
RUN python manage.py collectstatic --noinput 2>/dev/null || true

EXPOSE 8200

CMD ["gunicorn", "cirrus.wsgi:application", "--bind", "0.0.0.0:8200", "--workers", "3", "--timeout", "120"]
