"""Telemetry service — measure each step of a download.

Usage:
    from core.services.telemetry import StepTimer

    with StepTimer(descarga_log, "fiel_download", "minio") as step:
        # ... do work ...
        step.metadata = {"bytes": 1024}
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("core.telemetry")


class StepTimer:
    """Context manager for measuring a download step."""

    def __init__(self, descarga_log, fase, atribuible_a="cirrus"):
        self.descarga_log = descarga_log
        self.fase = fase
        self.atribuible_a = atribuible_a
        self.record = None

    def __enter__(self):
        from core.models import DescargaTelemetria

        self.record = DescargaTelemetria.objects.create(
            descarga_log=self.descarga_log,
            fase=self.fase,
            atribuible_a=self.atribuible_a,
            inicio=datetime.now(timezone.utc),
        )
        return self.record

    def __exit__(self, exc_type, exc_val, exc_tb):
        now = datetime.now(timezone.utc)
        self.record.fin = now
        self.record.duracion_ms = int(
            (now - self.record.inicio).total_seconds() * 1000
        )
        if exc_type:
            self.record.exitoso = False
            self.record.error = str(exc_val)[:1000]
        try:
            self.record.save()
        except Exception as e:
            logger.warning("Failed to save telemetry: %s", e)
        return False  # never suppress exceptions


def get_telemetry_summary(descarga_log):
    """Build a summary dict from telemetry records."""
    records = descarga_log.telemetria.all()
    if not records:
        return None

    total_ms = sum(r.duracion_ms for r in records)
    by_attribution = {}
    phases = []

    for r in records:
        label = dict(r.ATRIBUCION_CHOICES).get(r.atribuible_a, r.atribuible_a)
        by_attribution.setdefault(r.atribuible_a, 0)
        by_attribution[r.atribuible_a] += r.duracion_ms
        phases.append({
            "fase": r.fase,
            "label": dict(r.FASE_CHOICES).get(r.fase, r.fase),
            "ms": r.duracion_ms,
            "pct": round(r.duracion_ms / max(total_ms, 1) * 100, 1),
            "attr": r.atribuible_a,
            "ok": r.exitoso,
            "error": r.error,
            "metadata": r.metadata,
        })

    breakdown = []
    for attr, ms in sorted(by_attribution.items(), key=lambda x: -x[1]):
        breakdown.append({
            "attr": attr,
            "ms": ms,
            "pct": round(ms / max(total_ms, 1) * 100, 1),
        })

    return {
        "total_ms": total_ms,
        "total_s": round(total_ms / 1000, 1),
        "phases": phases,
        "breakdown": breakdown,
    }


def format_telegram_telemetry(descarga_log):
    """Format telemetry for Telegram alert."""
    summary = get_telemetry_summary(descarga_log)
    if not summary:
        return ""

    attr_labels = {"cirrus": "Servidor", "sat": "SAT", "minio": "MinIO", "red": "Red"}
    lines = [f"⏱ Duración total: {summary['total_s']}s"]
    for b in summary["breakdown"]:
        label = attr_labels.get(b["attr"], b["attr"])
        lines.append(f"  {label}: {round(b['ms']/1000, 1)}s ({b['pct']}%)")

    return "\n".join(lines)
