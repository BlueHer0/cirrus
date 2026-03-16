#!/bin/bash
# Cirrus Health Check — runs every 5 minutes via cron
# Checks all critical services and auto-restarts if needed.
# Sends Telegram alert if any service is down.

ERRORS=0
MESSAGES=""
LOGFILE="/var/www/cirrus/logs/health_check.log"

# Load env for Telegram credentials
set -a
source /var/www/cirrus/.env 2>/dev/null
set +a

# Check web (gunicorn on port 8200)
if ! curl -sf --max-time 5 http://127.0.0.1:8200/ > /dev/null 2>&1; then
    MESSAGES+="cirrus-web NO RESPONDE — reiniciando | "
    sudo systemctl restart cirrus-web
    ERRORS=$((ERRORS+1))
fi

# Check worker
if ! sudo systemctl is-active --quiet cirrus-worker; then
    MESSAGES+="cirrus-worker CAÍDO — reiniciando | "
    sudo systemctl restart cirrus-worker
    ERRORS=$((ERRORS+1))
fi

# Check beat
if ! sudo systemctl is-active --quiet cirrus-beat; then
    MESSAGES+="cirrus-beat CAÍDO — reiniciando | "
    sudo systemctl restart cirrus-beat
    ERRORS=$((ERRORS+1))
fi

# Check Redis
if ! redis-cli ping > /dev/null 2>&1; then
    MESSAGES+="Redis NO RESPONDE | "
    ERRORS=$((ERRORS+1))
fi

# Check PostgreSQL
if ! pg_isready -q 2>/dev/null; then
    MESSAGES+="PostgreSQL NO RESPONDE | "
    ERRORS=$((ERRORS+1))
fi

# Check disk space (alert if >90%)
DISK_USAGE=$(df / | awk 'NR==2{print $5}' | tr -d '%')
if [ "$DISK_USAGE" -gt 90 ]; then
    MESSAGES+="Disco al ${DISK_USAGE}% | "
    ERRORS=$((ERRORS+1))
fi

# Alert if errors
if [ $ERRORS -gt 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ERRORS=$ERRORS: $MESSAGES" >> "$LOGFILE"

    # Send Telegram alert
    if [ "$TELEGRAM_ALERTS_ENABLED" = "True" ] && [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
        curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
            -d chat_id="$TELEGRAM_CHAT_ID" \
            -d text="🚨 *CIRRUS ALERTA*: ${ERRORS} servicio(s) con problemas.
${MESSAGES}" \
            -d parse_mode="Markdown" > /dev/null 2>&1
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') OK" >> "$LOGFILE"
fi
