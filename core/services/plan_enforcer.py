"""Plan limit enforcement for Cirrus."""

from datetime import datetime, timezone

from core.models import CFDI, DescargaLog, Empresa


class PlanEnforcer:
    """Verify plan limits for a user. Owner plan has no limits."""

    def __init__(self, user):
        self.user = user
        self.profile = user.perfil
        self.plan = self.profile.get_plan()
        self.now = datetime.now(timezone.utc)
        self._is_owner = self.plan and self.plan.slug == "owner"

    def _unlimited(self, **extra):
        """Return unlimited result for owner plan."""
        return {"permitido": True, "limite": 99999, **extra}

    def puede_crear_empresa(self):
        actuales = Empresa.objects.filter(owner=self.user).count()
        if self._is_owner:
            return {**self._unlimited(), "actuales": actuales, "mensaje": ""}
        limite = self.plan.max_empresas if self.plan else 1
        ok = actuales < limite
        return {
            "permitido": ok,
            "actuales": actuales,
            "limite": limite,
            "pct": min(100, int(actuales / max(limite, 1) * 100)),
            "mensaje": "" if ok else (
                f"Has alcanzado el límite de {limite} empresa(s) "
                f"en tu plan {self.plan.nombre}."
            ),
        }

    def puede_ver_cfdi(self):
        from django.db.models import Q

        user_rfcs = list(
            Empresa.objects.filter(owner=self.user).values_list("rfc", flat=True)
        )
        total = CFDI.objects.filter(
            Q(rfc_empresa__in=user_rfcs) | Q(uploaded_by=self.user)
        ).count()
        if self._is_owner:
            return {
                **self._unlimited(), "total": total, "visibles": total,
                "excedente": 0, "tiene_excedente": False, "mensaje": "",
                "pct": 0,
            }
        limite = self.plan.max_cfdis_visibles if self.plan else 30
        excedente = max(0, total - limite)
        return {
            "total": total,
            "limite": limite,
            "visibles": min(total, limite),
            "excedente": excedente,
            "tiene_excedente": excedente > 0,
            "pct": min(100, int(min(total, limite) / max(limite, 1) * 100)),
            "mensaje": "" if not excedente else (
                f"Tienes {total} CFDIs pero tu plan muestra {limite}. "
                f"{excedente} CFDIs ocultos."
            ),
        }

    def puede_descargar(self):
        usadas = DescargaLog.objects.filter(
            empresa__owner=self.user,
            estado="completado",
            completado_at__year=self.now.year,
            completado_at__month=self.now.month,
        ).count()
        if self._is_owner:
            return {**self._unlimited(), "usadas": usadas, "mensaje": ""}
        limite = self.plan.max_descargas_mes if self.plan else 1
        ok = usadas < limite
        return {
            "permitido": ok,
            "usadas": usadas,
            "limite": limite,
            "pct": min(100, int(usadas / max(limite, 1) * 100)),
            "mensaje": "" if ok else (
                f"Has usado {usadas} de {limite} descargas este mes."
            ),
        }

    def puede_convertir_pdf(self):
        usadas = self.profile.conversiones_este_mes
        if self._is_owner:
            return {**self._unlimited(), "usadas": usadas, "mensaje": ""}
        limite = self.plan.max_conversiones_pdf if self.plan else 10
        ok = usadas < limite
        return {
            "permitido": ok,
            "usadas": usadas,
            "limite": limite,
            "pct": min(100, int(usadas / max(limite, 1) * 100)),
        }

    def puede_convertir_excel(self):
        usadas = self.profile.conversiones_este_mes  # shared counter
        if self._is_owner:
            return {**self._unlimited(), "usadas": usadas, "mensaje": ""}
        limite = self.plan.max_conversiones_excel if self.plan else 3
        ok = usadas < limite
        return {
            "permitido": ok,
            "usadas": usadas,
            "limite": limite,
        }

    def puede_upload(self):
        usados = CFDI.objects.filter(
            uploaded_by=self.user,
            descargado_at__year=self.now.year,
            descargado_at__month=self.now.month,
        ).count()
        if self._is_owner:
            return {**self._unlimited(), "usados": usados, "mensaje": ""}
        limite = self.plan.max_uploads_mes if self.plan else 10
        ok = usados < limite
        return {
            "permitido": ok,
            "usados": usados,
            "limite": limite,
            "pct": min(100, int(usados / max(limite, 1) * 100)),
        }

    def puede_usar_api(self):
        if self._is_owner:
            return {"permitido": True, "mensaje": ""}
        ok = self.plan.api_rest if self.plan else False
        return {
            "permitido": ok,
            "nivel": self.plan.api_nivel if self.plan else "none",
            "mensaje": "" if ok else (
                "La API REST está disponible desde el plan Profesional."
            ),
        }

    def resumen(self):
        return {
            "plan": self.plan,
            "empresas": self.puede_crear_empresa(),
            "cfdis": self.puede_ver_cfdi(),
            "descargas": self.puede_descargar(),
            "pdf": self.puede_convertir_pdf(),
            "uploads": self.puede_upload(),
            "api": self.puede_usar_api(),
        }
