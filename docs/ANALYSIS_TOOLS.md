# Cirrus — Herramientas de Análisis Fiscal

## Acceso
Aparecen en /app/cfdis/ cuando el usuario filtra por empresa + año + mes.
Barra de herramientas con 6 botones + FiscScore dinámico.

## Herramientas implementadas

### 1. Resumen Rápido (/app/analysis/summary/)
- Total CFDIs, emitidos, recibidos, cancelados (+ delta vs mes anterior)
- Montos: facturado, gastos, resultado estimado, ticket promedio, factura max
- Barras: tipo comprobante, forma de pago
- Gráfico: actividad diaria del mes
- Top 5 clientes y proveedores por monto
- Alertas: cancelados, efectivo no deducible, PPD sin complemento

### 2. Análisis Fiscal (/app/analysis/fiscal/)
- Ingresos, gastos deducibles, utilidad fiscal
- ISR provisional (×0.30), retenciones ISR e IVA
- Gastos no deducibles
- Barra de deducibilidad (% deducible vs no deducible)
- Motivos de no deducibilidad

### 3. IVA del Periodo (/app/analysis/iva/)
- Flujo: IVA trasladado − IVA acreditable = IVA por pagar
- IVA retenido, efectivo no acreditable, neto a enterar
- Desglose por tasa (16%, 8%, 0%)
- Tendencia IVA a pagar (6 meses)

### 4. Estado de Resultados (/app/analysis/income/)
- Waterfall: Ingresos → Gastos → Utilidad
- Margen bruto, ISR estimado, margen neto
- Comparativo 6 meses (barras ingresos vs gastos)

### 5. Top RFC (/app/analysis/top-rfc/)
- Top 10 clientes por monto (barras proporcionales)
- Top 10 proveedores por monto
- Concentración cliente/proveedor #1 (riesgo alto/medio/bajo)

### 6. Riesgos Fiscales (/app/analysis/risks/)
- FiscScore (0-100) gauge SVG
- Semáforos: cancelados, PPD sin complemento, efectivo >$2K, duplicados
- Listas negras SAT: "próximamente"
- Cards: cumplimiento %, deducibilidad %, diversificación %

## FiscScore
Fórmula: cumplimiento×0.35 + IVA×0.25 + deducibilidad×0.20 +
         diversificación×0.10 + errores×0.10

## Disclaimer
Todos los reportes incluyen:
"Este reporte es únicamente con fines informativos y de referencia.
No constituye opinión contable, fiscal ni legal."

## Pendientes fase 3
- Exportar ZIP (todos los XMLs del filtro)
- Reporte PDF ejecutivo (consolida todos los análisis)
- Red de relaciones (grafo visual)
- Listas negras SAT (69-B)
