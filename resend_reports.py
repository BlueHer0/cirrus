import sys
import os
import django

sys.path.append('/var/www/cirrus')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cirrus.settings')
django.setup()

from core.models import Empresa
from reportes.tasks import generar_y_enviar_reporte_anual
from django.contrib.auth.models import User

def main():
    print("Iniciando RE-envío de reportes anuales 2025 SIN PDF...")
    
    jessica_user = User.objects.filter(first_name__icontains='Jessica').first()
    if not jessica_user:
        jessica_user = User.objects.filter(email__icontains='jessica').first()
        
    jessica_email = jessica_user.email if jessica_user else None
    print(f"Correo principal detectado (Jessica): {jessica_email}")

    empresas = Empresa.objects.filter(descarga_activa=True)
    anio = 2025

    for empresa in empresas:
        print(f"→ Generando {anio} para {empresa.nombre} ({empresa.rfc})...")
        try:
            res = generar_y_enviar_reporte_anual(
                empresa_id=str(empresa.id),
                anio=anio,
                emails_extra=["farizpe@icloud.com"],
                override_owner_email=jessica_email
            )
            print(f"  ✓ Resultado: {res}")
        except Exception as e:
            print(f"  ERROR en {empresa.rfc}: {e}")

if __name__ == "__main__":
    main()
