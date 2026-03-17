#!/bin/bash
# Backup diario de Cirrus
# Cron: 0 4 * * * /var/www/cirrus/scripts/backup.sh >> /var/www/cirrus/logs/backup.log 2>&1

DATE=$(date +%Y%m%d_%H%M)
BACKUP_DIR="/var/www/cirrus/backups"
mkdir -p $BACKUP_DIR

echo "$(date) Iniciando backup..."

# 1. PostgreSQL dump
PGPASSWORD=TPTYHFwVK7LO73WeLpd3rIm7ZP6ORKYU pg_dump -h localhost -U cirrus cirrus_db | gzip > $BACKUP_DIR/pg_${DATE}.sql.gz
echo "PostgreSQL: $(du -h $BACKUP_DIR/pg_${DATE}.sql.gz | cut -f1)"

# 2. Metadata snapshot
cd /var/www/cirrus && source venv/bin/activate
python manage.py shell -c "
from core.models import CFDI, Empresa
import json
data = {
    'empresas': list(Empresa.objects.values('id','rfc','nombre','fiel_verificada')),
    'cfdis_count': CFDI.objects.count(),
    'cfdis_por_empresa': {e.rfc: e.cfdis.count() for e in Empresa.objects.all()},
}
with open('$BACKUP_DIR/metadata_${DATE}.json', 'w') as f:
    json.dump(data, f, indent=2, default=str)
print(f'Metadata: {len(data[\"empresas\"])} empresas, {data[\"cfdis_count\"]} CFDIs')
"

# 3. Config backup
cp /var/www/cirrus/.env $BACKUP_DIR/env_${DATE}.bak
cp /var/www/cirrus/cirrus/settings.py $BACKUP_DIR/settings_${DATE}.py

# 4. Cleanup (30 days retention)
find $BACKUP_DIR -name "*.gz" -mtime +30 -delete
find $BACKUP_DIR -name "*.json" -mtime +30 -delete
find $BACKUP_DIR -name "*.bak" -mtime +30 -delete
find $BACKUP_DIR -name "*.py" -mtime +30 -delete

# 5. Report
TOTAL=$(du -sh $BACKUP_DIR | cut -f1)
echo "$(date) Backup completo. Total: $TOTAL"

# 6. Telegram notification
TELEGRAM_BOT_TOKEN=$(grep ^TELEGRAM_BOT_TOKEN /var/www/cirrus/.env | cut -d= -f2)
TELEGRAM_CHAT_ID=$(grep ^TELEGRAM_CHAT_ID /var/www/cirrus/.env | cut -d= -f2)
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    PG_SIZE=$(du -h $BACKUP_DIR/pg_${DATE}.sql.gz | cut -f1)
    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="$TELEGRAM_CHAT_ID" \
        -d text="💾 Backup Cirrus OK — PG: $PG_SIZE — Total: $TOTAL" > /dev/null
fi
