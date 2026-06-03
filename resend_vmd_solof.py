import sys
import os
import django

sys.path.append('/var/www/cirrus')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cirrus.settings')
django.setup()

from core.models import Empresa
from reportes.tasks import generar_y_enviar_reporte_anual

def main():
    # Enviar EXCLUSIVAMENTE a farizpe@icloud.com
    target_email = "farizpe@icloud.com"
    empresa = Empresa.objects.filter(rfc="VEN191127M21").first()
    anio = 2025

    if empresa:
        print(f"→ Enviando correo aislado de {anio} para {empresa.nombre} a {target_email}...")
        try:
            res = generar_y_enviar_reporte_anual(
                empresa_id=str(empresa.id),
                anio=anio,
                emails_extra=[],
                override_owner_email=target_email
            )
            print(f"  ✓ Resultado: {res}")
        except Exception as e:
            print(f"  ERROR: {e}")
    else:
        print("Empresa no encontrada.")

if __name__ == "__main__":
    main()
