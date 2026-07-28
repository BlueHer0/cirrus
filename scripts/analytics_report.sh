#!/bin/bash
# Genera el reporte de analytics (GoAccess) desde el log dedicado de nginx.
# Cron: cada hora. Salida: logs/analytics.html (servido en /panel/analytics/).
# Persistencia entre rotaciones de log: --restore/--persist en logs/goaccess-db/.
set -e
DB=/var/www/cirrus/logs/goaccess-db
OUT=/var/www/cirrus/logs/analytics.html
mkdir -p "$DB"
goaccess /var/log/nginx/cirrus.access.log \
    --log-format=COMBINED \
    --restore --persist --db-path="$DB" \
    --ignore-crawlers \
    --exclude-ip=127.0.0.1 \
    --html-report-title="Cirrus Analytics" \
    -o "$OUT" >/dev/null 2>&1
