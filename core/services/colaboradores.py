"""
Servicios para la gestión de Colaboradores y permisos de acceso a Empresas.
"""

from core.models import Empresa, Colaborador, ColaboradorEmpresa

def get_empresas_visibles(user):
    """
    Retorna todas las empresas que un usuario puede ver:
    1. Empresas propias (donde es dueño directo, owner=user)
    2. Empresas compartidas por cuentas que lo invitaron como colaborador
    3. Empresas de sus colaboradores hijos (donde user es cuenta_principal)
    """
    
    # 1. Empresas propias
    propias = Empresa.objects.filter(owner=user)
    
    # 2. Empresas por colaboración (soy colaborador de alguien más)
    colaboraciones_activas = Colaborador.objects.filter(
        usuario=user,
        estado=Colaborador.Estado.ACTIVO
    )
    
    empresas_compartidas_ids = ColaboradorEmpresa.objects.filter(
        colaborador__in=colaboraciones_activas
    ).values_list('empresa_id', flat=True)
    
    compartidas = Empresa.objects.filter(id__in=empresas_compartidas_ids)
    
    # 3. Empresas de mis colaboradores hijos (soy cuenta_principal)
    hijos_activos = Colaborador.objects.filter(
        cuenta_principal=user,
        estado=Colaborador.Estado.ACTIVO
    ).values_list('usuario_id', flat=True)
    
    de_hijos = Empresa.objects.filter(owner_id__in=hijos_activos)
    
    # Unión de las tres fuentes
    return (propias | compartidas | de_hijos).distinct()
