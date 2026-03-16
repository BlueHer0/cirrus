"""Gunicorn configuration for Cirrus production deployment.

Usage:
    gunicorn -c gunicorn.conf.py cirrus.wsgi:application
"""

import multiprocessing

# ── Bind ─────────────────────────────────────────────────────────────────
bind = "127.0.0.1:8200"

# ── Workers ──────────────────────────────────────────────────────────────
# 3 workers for a small VPS. Adjust based on available CPU cores.
# Rule of thumb: (2 × CPU cores) + 1, but capped for memory on shared VPS.
workers = 3
worker_class = "gthread"
threads = 2  # 3 workers × 2 threads = 6 concurrent requests

# ── Timeouts ─────────────────────────────────────────────────────────────
# Generous timeout for API endpoints that trigger scraping
timeout = 300
graceful_timeout = 120
keepalive = 5

# ── Logging ──────────────────────────────────────────────────────────────
accesslog = "/var/www/cirrus/logs/gunicorn-access.log"
errorlog = "/var/www/cirrus/logs/gunicorn-error.log"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)sμs'

# ── Process naming ───────────────────────────────────────────────────────
proc_name = "cirrus"

# ── Security ─────────────────────────────────────────────────────────────
# Forward X-Forwarded-* headers from Nginx
forwarded_allow_ips = "127.0.0.1"

# ── Reload (disable in production) ──────────────────────────────────────
reload = False

# ── Max requests (prevent memory leaks) ─────────────────────────────────
max_requests = 1000
max_requests_jitter = 50

# ── Tmp upload dir ───────────────────────────────────────────────────────
tmp_upload_dir = "/tmp"
