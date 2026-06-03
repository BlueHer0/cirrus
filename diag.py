import sys
import os
import django

sys.path.append('/var/www/cirrus')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cirrus.settings')
django.setup()

from core.models import CFDI, Empresa
from datetime import date

empresa = Empresa.objects.get(rfc='VEN191127M21')
print(f"\n=============================")
print(f"Empresa: {empresa.nombre}")
print(f"RFC empresa: {empresa.rfc}")
print(f"Usuario: {getattr(empresa, 'owner', getattr(empresa, 'usuario', 'No tiene usuario'))}")

# Total CFDIs de esta empresa sin filtro de fecha
total = CFDI.objects.filter(rfc_empresa='VEN191127M21').count()
print(f"Total CFDIs sin filtro: {total}")

# Por tipo
from django.db.models import Count, Sum
por_tipo = CFDI.objects.filter(rfc_empresa='VEN191127M21').values('tipo_comprobante').annotate(cnt=Count('uuid'), total=Sum('total'))
print("Por tipo:")
for t in por_tipo:
    print(f"  Tipo {t['tipo_comprobante']}: {t['cnt']} CFDIs, ${t['total']}")

# Por año
por_anio = CFDI.objects.filter(rfc_empresa='VEN191127M21').values('fecha__year').annotate(cnt=Count('uuid'), total=Sum('total')).order_by('fecha__year')
print("Por año:")
for a in por_anio:
    print(f"  Año {a['fecha__year']}: {a['cnt']} CFDIs, ${a['total']}")

# Específico 2025
cfdis_2025 = CFDI.objects.filter(
    rfc_empresa='VEN191127M21',
    fecha__date__gte=date(2025,1,1),
    fecha__date__lte=date(2025,12,31)
)
print(f"CFDIs 2025 con filtro fecha__date: {cfdis_2025.count()}")

# Ver cómo está guardado rfc_emisor vs rfc_receptor para ingresos
ingresos = CFDI.objects.filter(rfc_empresa='VEN191127M21', tipo_comprobante='I').first()
if ingresos:
    print(f"\nIngreso ejemplo - rfc_emisor: {ingresos.rfc_emisor}, rfc_receptor: {ingresos.rfc_receptor}, fecha: {ingresos.fecha}")
else:
    print("\nNO HAY CFDIs tipo I para este RFC")

# Ver un CFDI cualquiera para entender la estructura
cualquiera = CFDI.objects.filter(rfc_empresa='VEN191127M21').first()
if cualquiera:
    print(f"CFDI ejemplo - tipo: {cualquiera.tipo_comprobante}, emisor: {cualquiera.rfc_emisor}, receptor: {cualquiera.rfc_receptor}, total: {cualquiera.total}, fecha: {cualquiera.fecha}")
print("=============================\n")
