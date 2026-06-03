"""
Template filters for reportes module.

Usage:
    {% load reporte_filters %}
    {{ valor|pesos }}            → "$180,382 MXN"
    {{ valor|semaforo }}         → "green" / "red" / "gray"
    {{ valor|pct_bar_width }}    → "75" (clamped 0-100)
"""

from decimal import Decimal
from django import template

register = template.Library()


@register.filter
def pesos(value):
    """Format number as '$180,382 MXN'."""
    try:
        val = Decimal(str(value))
        if val < 0:
            return f"-${abs(val):,.0f} MXN"
        return f"${val:,.0f} MXN"
    except (TypeError, ValueError, ArithmeticError):
        return "$0 MXN"


@register.filter
def pesos_decimal(value):
    """Format number as '$180,382.50'."""
    try:
        val = Decimal(str(value))
        if val < 0:
            return f"-${abs(val):,.2f}"
        return f"${val:,.2f}"
    except (TypeError, ValueError, ArithmeticError):
        return "$0.00"


@register.filter
def semaforo(value):
    """Return CSS color class based on sign: positive=green, negative=red, zero=gray."""
    try:
        val = float(value)
        if val > 0:
            return "green"
        elif val < 0:
            return "red"
        return "gray"
    except (TypeError, ValueError):
        return "gray"


@register.filter
def semaforo_hex(value):
    """Return hex color based on sign."""
    try:
        val = float(value)
        if val > 0:
            return "#3fb950"
        elif val < 0:
            return "#f85149"
        return "#8b949e"
    except (TypeError, ValueError):
        return "#8b949e"


@register.filter
def pct_bar_width(value):
    """Clamp value to 0-100 for bar width."""
    try:
        return max(0, min(100, int(float(value))))
    except (TypeError, ValueError):
        return 0


@register.filter
def health_color(score):
    """Return color for health score: green > 70, amber 40-70, red < 40."""
    try:
        s = int(score)
        if s > 70:
            return "#3fb950"
        elif s >= 40:
            return "#e3b341"
        return "#f85149"
    except (TypeError, ValueError):
        return "#8b949e"


@register.filter
def abs_val(value):
    """Return absolute value."""
    try:
        return abs(Decimal(str(value)))
    except (TypeError, ValueError, ArithmeticError):
        return 0


@register.filter
def prioridad_color(prioridad):
    """Return border color for action priority."""
    colors = {
        "alta": "#f85149",
        "media": "#e3b341",
        "baja": "#3fb950",
    }
    return colors.get(prioridad, "#8b949e")


@register.filter
def truncate_uuid(value):
    """Truncate UUID to first 8 chars + '...'."""
    s = str(value)
    if len(s) > 8:
        return s[:8] + "..."
    return s
