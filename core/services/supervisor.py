"""Cirrus Supervisor — Autonomous monitoring and corrective actions.

Runs every 15 minutes via Celery Beat, before the sync agent.
"""

import logging
import shutil
from datetime import datetime, timedelta, timezone

from core.models import DescargaJob, DescargaLog, Empresa

logger = logging.getLogger("core.supervisor")


class CirrusSupervisor:
    """Monitors system health and takes corrective actions."""

    def __init__(self):
        self.now = datetime.now(timezone.utc)
        self.acciones = []

    def ejecutar(self):
        """Run all checks, return report string."""
        from core.services.monitor import log_info
        from core.services.alerts import send_telegram

        self.limpiar_zombies()
        self.verificar_empresas_sin_descargas()
        self.detectar_huecos_descarga()
        self.detectar_sat_lento()
        self.verificar_espacio_disco()
        self.detectar_errores_repetidos()

        if self.acciones:
            reporte = "🤖 Supervisor Cirrus:\n" + "\n".join(self.acciones)
            log_info("system", reporte)
            criticas = [a for a in self.acciones if "🔴" in a or "⚠️" in a]
            if criticas:
                send_telegram(reporte[:500], "warning")
            return reporte
        return "Supervisor: todo OK"

    def limpiar_zombies(self):
        """Clean downloads stuck in ejecutando > 1 hour."""
        cutoff = self.now - timedelta(hours=1)
        zombies = DescargaLog.objects.filter(
            estado="ejecutando", iniciado_at__lt=cutoff
        )
        count = zombies.count()
        if count > 0:
            zombies.update(
                estado="error",
                progreso=f"Zombie limpiado por supervisor ({self.now:%H:%M})",
            )
            self.acciones.append(f"🧹 {count} zombies limpiados")

    def verificar_empresas_sin_descargas(self):
        """Flag empresas with sync active but 0 completed downloads after 24h."""
        cutoff = self.now - timedelta(hours=24)
        for emp in Empresa.objects.filter(sync_activa=True, fiel_verificada=True):
            completadas = DescargaLog.objects.filter(
                empresa=emp, estado="completado"
            ).count()
            if completadas == 0 and emp.created_at < cutoff:
                dias = (self.now.date() - emp.created_at.date()).days
                self.acciones.append(
                    f"⚠️ {emp.rfc} lleva {dias} día(s) con sync activa y 0 descargas"
                )

    def detectar_huecos_descarga(self):
        """Detect months without a DescargaJob and create them automatically."""
        total_creados = 0

        for empresa in Empresa.objects.filter(sync_activa=True, fiel_verificada=True):
            plan = empresa.owner.perfil.get_plan() if hasattr(empresa.owner, "perfil") else None
            slug = plan.slug if plan else "free"
            prioridad = {"owner": 1, "enterprise": 1, "pro": 3, "basico": 5, "free": 9}.get(slug, 9)

            start_y = empresa.sync_desde_year or 2025
            start_m = empresa.sync_desde_month or 1
            delay_minutes = total_creados * 5

            y, m = start_y, start_m
            while (y < self.now.year) or (y == self.now.year and m <= self.now.month):
                for tipo in ["recibidos", "emitidos"]:
                    job, created = DescargaJob.objects.get_or_create(
                        empresa=empresa, year=y, month=m, tipo=tipo,
                        defaults={
                            "estado": "en_cola",
                            "prioridad": prioridad,
                            "programado_para": self.now + timedelta(minutes=delay_minutes),
                        },
                    )
                    if created:
                        total_creados += 1
                        delay_minutes += 5
                    elif job.estado == "error" and job.intentos < job.max_intentos:
                        job.estado = "en_cola"
                        job.programado_para = self.now + timedelta(minutes=30)
                        job.save(update_fields=["estado", "programado_para"])
                        total_creados += 1

                m += 1
                if m > 12:
                    m = 1
                    y += 1

        if total_creados > 0:
            self.acciones.append(
                f"🔧 {total_creados} jobs creados/re-encolados (huecos detectados)"
            )

    def detectar_sat_lento(self):
        """Alert if recent downloads are 2x slower than historical average."""
        try:
            from core.models import DescargaTelemetria
            from django.db.models import Avg

            avg_global = DescargaTelemetria.objects.filter(
                fase="engine_run", exitoso=True
            ).aggregate(avg=Avg("duracion_ms"))["avg"]
            if not avg_global:
                return

            cutoff = self.now - timedelta(hours=1)
            avg_reciente = DescargaTelemetria.objects.filter(
                fase="engine_run", exitoso=True, inicio__gte=cutoff
            ).aggregate(avg=Avg("duracion_ms"))["avg"]

            if avg_reciente and avg_reciente > avg_global * 2:
                self.acciones.append(
                    f"⚠️ SAT lento — última hora: {avg_reciente / 1000:.0f}s "
                    f"vs histórico: {avg_global / 1000:.0f}s"
                )
        except Exception:
            pass

    def verificar_espacio_disco(self):
        """Alert on low disk space."""
        total, used, free = shutil.disk_usage("/")
        pct = used / total * 100
        gb_libre = free / (1024**3)

        if pct > 85:
            self.acciones.append(f"🔴 Disco al {pct:.0f}% — {gb_libre:.1f} GB libres")
        elif pct > 70:
            self.acciones.append(f"⚠️ Disco al {pct:.0f}% — {gb_libre:.1f} GB libres")

    def detectar_errores_repetidos(self):
        """Flag empresas with 3+ consecutive download errors."""
        for emp in Empresa.objects.filter(sync_activa=True):
            ultimos = DescargaLog.objects.filter(empresa=emp).order_by("-iniciado_at")[:5]
            errores = 0
            for d in ultimos:
                if d.estado == "error":
                    errores += 1
                else:
                    break
            if errores >= 3:
                self.acciones.append(
                    f"🔴 {emp.rfc}: {errores} errores consecutivos — posible problema FIEL/SAT"
                )
