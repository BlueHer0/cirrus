#!/bin/bash
cd /var/www/cirrus
source venv/bin/activate

python manage.py shell -c "
from core.models import DescargaLog, CFDI, Empresa, DescargaTelemetria
from django.db.models import Avg, Max, Min, Count, Sum
from datetime import datetime, timedelta, timezone

now = datetime.now(timezone.utc)
last_hour = now - timedelta(hours=1)

print(f'=== BENCHMARK REPORT {now.strftime(\"%H:%M\")} ===')

# Estado de descargas
for estado in ['completado','ejecutando','pendiente','error']:
    c = DescargaLog.objects.filter(estado=estado).count()
    if c: print(f'  {estado}: {c}')

# CFDIs por empresa
print(f'\nCFDIs por empresa:')
for e in Empresa.objects.all():
    print(f'  {e.rfc}: {e.cfdis.count()}')
print(f'  TOTAL: {CFDI.objects.count()}')

# Última hora
completados_1h = DescargaLog.objects.filter(
    estado='completado', completado_at__gte=last_hour
).count()
errores_1h = DescargaLog.objects.filter(
    estado='error', completado_at__gte=last_hour
).count()
print(f'\nÚltima hora: {completados_1h} completadas, {errores_1h} errores')

# Telemetría promedio
tel = DescargaTelemetria.objects.filter(
    fase='engine_run', exitoso=True,
    inicio__gte=last_hour
).aggregate(
    avg=Avg('duracion_ms'),
    max_val=Max('duracion_ms'),
    min_val=Min('duracion_ms'),
    count=Count('id'),
)
if tel['count']:
    print(f'Engine run (última hora): avg={tel[\"avg\"]/1000:.1f}s, min={tel[\"min_val\"]/1000:.1f}s, max={tel[\"max_val\"]/1000:.1f}s, n={tel[\"count\"]}')

# Errores recientes
errores = DescargaLog.objects.filter(estado='error').order_by('-completado_at')[:3]
for err in errores:
    msgs = err.errores or []
    ultimo = msgs[-1] if msgs else 'sin detalle'
    print(f'ERROR: {err.empresa.rfc} {err.year}/{err.month_start} — {ultimo[:100]}')

# Workers activos
import subprocess
result = subprocess.run(['ps', 'aux'], capture_output=True, text=True)
workers = len([l for l in result.stdout.split('\n') if 'ForkPoolWorker' in l])
print(f'\nWorkers activos: {workers}')

# RAM del worker
result2 = subprocess.run(['systemctl', 'status', 'cirrus-worker', '--no-pager'], capture_output=True, text=True)
for line in result2.stdout.split('\n'):
    if 'Memory' in line:
        print(f'Worker RAM: {line.strip()}')
        break

print('---')
"
