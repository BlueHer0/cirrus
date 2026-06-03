import os
import django

# Only needed if executing via plain python instead of manage.py shell
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cirrus.settings')
django.setup()

from django.contrib.auth import get_user_model
from core.models import Empresa, Colaborador, ColaboradorEmpresa
from accounts.models import ClienteProfile

User = get_user_model()

def run_migration():
    try:
        fernando_user = User.objects.get(email='farizpe@icloud.com')
        jessica_user = User.objects.get(email='jessycr100916@gmail.com')
    except User.DoesNotExist as e:
        print(f"Error encontrando los usuarios: {e}")
        return

    fernando_profile = fernando_user.perfil
    jessica_profile = jessica_user.perfil

    # 1. Migrar empresas de Jessica a Fernando
    empresas_jessica = Empresa.objects.filter(owner=jessica_user)
    print(f"Migrando {empresas_jessica.count()} empresas de Jessica a Fernando:")
    for emp in empresas_jessica:
        print(f"  - {emp.rfc}: {emp.nombre}")
        emp.owner = fernando_user
        emp.save()

    # 2. Cambiar plan de Jessica a Owner
    jessica_profile.plan_legacy = 'owner'
    jessica_profile.plan_fk = None
    jessica_profile.save()
    print(f"Jessica plan cambiado a: owner")

    # 3. Crear colaboración
    colab, created = Colaborador.objects.get_or_create(
        cuenta_principal=fernando_user,
        usuario=jessica_user,
        defaults={
            'estado': 'activo',
            'puede_ver_cfdis': True,
            'puede_ver_analisis': True,
            'puede_exportar': True,
            'puede_subir_fiel': True,
            'puede_subir_xmls': True,
            'puede_crear_empresa': True,
            'puede_descargar_sat': True,
            'puede_ver_csf': True,
        }
    )
    print(f"Colaboración creada: {colab}" if created else f"Colaboración ya existía: {colab}")

    # Si ya existía, asegurarse de que tiene todos los permisos activos
    if not created:
        colab.puede_ver_cfdis = True
        colab.puede_ver_analisis = True
        colab.puede_exportar = True
        colab.puede_subir_fiel = True
        colab.puede_subir_xmls = True
        colab.puede_crear_empresa = True
        colab.puede_descargar_sat = True
        colab.puede_ver_csf = True
        colab.estado = 'activo'
        colab.save()

    # 4. Asignar TODAS las empresas de Fernando a Jessica
    todas_empresas = Empresa.objects.filter(owner=fernando_user)
    for emp in todas_empresas:
        ce, created = ColaboradorEmpresa.objects.get_or_create(
            colaborador=colab,
            empresa=emp,
            defaults={
                'puede_ver_cfdis': True,
                'puede_subir_fiel': True,
                'puede_exportar': True,
            }
        )
        if created:
            print(f"  Asignada: {emp.rfc}")

    print(f"\nResumen:")
    print(f"  Empresas de Fernando: {todas_empresas.count()}")
    print(f"  Jessica puede ver: {ColaboradorEmpresa.objects.filter(colaborador=colab).count()} empresas de Fernando")
    print(f"  Permisos de Jessica: TODOS")

if __name__ == '__main__':
    run_migration()
