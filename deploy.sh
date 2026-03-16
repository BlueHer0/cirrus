#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# Cirrus — Deploy Script
# Usage: sudo bash /var/www/cirrus/deploy.sh
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

APP_DIR="/var/www/cirrus"
VENV="$APP_DIR/venv/bin"
USER="farizpe"

echo "═══════════════════════════════════════════════"
echo "  🚀 Cirrus Deploy"
echo "  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════"

# ── 1. Pull latest code ─────────────────────────────────────────────
echo ""
echo "📥 Pulling latest changes..."
cd "$APP_DIR"
sudo -u "$USER" git pull --ff-only

# ── 2. Install dependencies ─────────────────────────────────────────
echo ""
echo "📦 Installing Python dependencies..."
sudo -u "$USER" "$VENV/pip" install -r requirements.txt --quiet

# ── 3. Run migrations ───────────────────────────────────────────────
echo ""
echo "🗄️  Running migrations..."
sudo -u "$USER" "$VENV/python" manage.py migrate --noinput

# ── 4. Collect static files ─────────────────────────────────────────
echo ""
echo "📁 Collecting static files..."
sudo -u "$USER" "$VENV/python" manage.py collectstatic --noinput --clear 2>/dev/null || \
sudo -u "$USER" "$VENV/python" manage.py collectstatic --noinput

# ── 5. Initialize schedules (if any new empresas) ───────────────────
echo ""
echo "📅 Initializing schedules for new empresas..."
sudo -u "$USER" "$VENV/python" manage.py init_schedules

# ── 6. Restart services ─────────────────────────────────────────────
echo ""
echo "🔄 Restarting services..."
systemctl restart cirrus-web cirrus-worker cirrus-beat

# ── 7. Verify ────────────────────────────────────────────────────────
echo ""
echo "🔍 Verifying services..."
sleep 3

for svc in cirrus-web cirrus-worker cirrus-beat; do
    if systemctl is-active --quiet "$svc"; then
        echo "  ✅ $svc is running"
    else
        echo "  ❌ $svc FAILED"
        systemctl status "$svc" --no-pager -l | head -15
    fi
done

# ── 8. Quick health check ───────────────────────────────────────────
echo ""
echo "🏥 Health check..."
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8200/ 2>/dev/null || echo "000")
if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "  ✅ Web server responding (HTTP $HTTP_CODE)"
else
    echo "  ⚠️  Web server returned HTTP $HTTP_CODE"
fi

echo ""
echo "═══════════════════════════════════════════════"
echo "  ✅ Deploy complete!"
echo "═══════════════════════════════════════════════"
