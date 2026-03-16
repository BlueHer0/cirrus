/**
 * Cirrus — Fiscal Analysis Modals
 * Vanilla JS: fetch API data → render modal with cards, charts, alerts.
 */
(function () {
  'use strict';

  const API_BASE = '/api/v1/analysis';
  const $ = (sel, ctx) => (ctx || document).querySelector(sel);
  const $$ = (sel, ctx) => [...(ctx || document).querySelectorAll(sel)];
  const fmt = (n) => {
    if (n == null || isNaN(n)) return '$0';
    return '$' + Number(n).toLocaleString('es-MX', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };
  const fmtK = (n) => {
    if (n == null || isNaN(n)) return '$0';
    if (Math.abs(n) >= 1000000) return '$' + (n / 1000000).toFixed(1) + 'M';
    if (Math.abs(n) >= 1000) return '$' + (n / 1000).toFixed(1) + 'K';
    return '$' + Number(n).toFixed(0);
  };
  const delta = (n) => {
    if (!n) return '<span class="an-card-delta neutral">— sin cambio</span>';
    const cls = n > 0 ? 'positive' : 'negative';
    const sign = n > 0 ? '+' : '';
    return `<span class="an-card-delta ${cls}">${sign}${n}</span>`;
  };
  const deltaPct = (n) => {
    if (!n) return '<span class="an-card-delta neutral">—</span>';
    const cls = n > 0 ? 'positive' : 'negative';
    const sign = n > 0 ? '+' : '';
    return `<span class="an-card-delta ${cls}">${sign}${n}%</span>`;
  };
  const BAR_COLORS = ['bar-indigo', 'bar-green', 'bar-blue', 'bar-amber', 'bar-purple', 'bar-pink', 'bar-cyan', 'bar-red'];

  let currentOverlay = null;

  // ── Modal Infrastructure ───────────────────────────────────────────

  function openModal(title, bodyHTML) {
    closeModal();
    const overlay = document.createElement('div');
    overlay.className = 'analysis-overlay';
    overlay.innerHTML = `
      <div class="analysis-modal">
        <div class="analysis-modal-header">
          <h2>${title}</h2>
          <button class="analysis-modal-close" onclick="window.__closeAnalysisModal()">✕</button>
        </div>
        <div class="analysis-modal-body">${bodyHTML}</div>
        <div class="analysis-modal-footer">
          <span>Generado: ${new Date().toLocaleString('es-MX')} — Fuente: CFDIs SAT — Informativo, no sustituye asesoría fiscal</span>
          <span></span>
        </div>
      </div>`;
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
    document.body.appendChild(overlay);
    currentOverlay = overlay;
    document.addEventListener('keydown', escHandler);
  }

  function closeModal() {
    if (currentOverlay) { currentOverlay.remove(); currentOverlay = null; }
    document.removeEventListener('keydown', escHandler);
  }
  function escHandler(e) { if (e.key === 'Escape') closeModal(); }
  window.__closeAnalysisModal = closeModal;

  function showLoading(title) {
    openModal(title, `
      <div class="an-skeleton">
        <div class="an-skeleton-spinner"></div>
        <span style="color:#64748b;font-size:0.75rem">Calculando análisis...</span>
      </div>`);
  }

  async function fetchData(endpoint, params) {
    const qs = new URLSearchParams(params).toString();
    const resp = await fetch(`${API_BASE}/${endpoint}/?${qs}`, {
      credentials: 'same-origin',
      headers: { 'X-Requested-With': 'XMLHttpRequest' },
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `Error ${resp.status}`);
    }
    return resp.json();
  }

  function getParams() {
    const form = $('form[method="get"]');
    if (!form) return null;
    const empresa = form.querySelector('[name="empresa"]')?.value;
    const year = form.querySelector('[name="year"]')?.value;
    const month = form.querySelector('[name="month"]')?.value;
    if (!empresa || !year || !month) return null;
    return { empresa_id: empresa, year, month };
  }

  // ── Render: Summary ────────────────────────────────────────────────

  function renderSummary(d) {
    const maxDaily = Math.max(...d.actividad_diaria.map(x => x.count), 1);
    const dailyBars = d.actividad_diaria.map(x =>
      `<div class="an-daily-bar" style="height:${Math.max((x.count / maxDaily) * 100, 2)}%" data-tip="Día ${x.dia}: ${x.count} CFDIs · ${fmtK(x.monto)}"></div>`
    ).join('');

    const maxTipo = Math.max(...d.por_tipo.map(x => x.count), 1);
    const tipoBars = d.por_tipo.map((x, i) =>
      `<div class="an-bar-item">
        <span class="an-bar-label">${x.label}</span>
        <div class="an-bar-track"><div class="an-bar-fill ${BAR_COLORS[i]}" style="width:${(x.count / maxTipo) * 100}%">${x.count}</div></div>
      </div>`
    ).join('');

    const fpBars = d.por_forma_pago.map((x, i) =>
      `<div class="an-bar-item">
        <span class="an-bar-label">${x.label}</span>
        <div class="an-bar-track"><div class="an-bar-fill ${BAR_COLORS[i + 2]}" style="width:${x.porcentaje}%">${x.porcentaje}%</div></div>
      </div>`
    ).join('');

    const clientes = d.top_clientes.map(x =>
      `<li class="an-rfc-item"><span class="an-rfc-rfc">${x.rfc}</span><span class="an-rfc-monto">${fmt(x.monto)}</span></li>`
    ).join('') || '<li class="an-rfc-item" style="color:#475569">Sin datos</li>';

    const proveedores = d.top_proveedores.map(x =>
      `<li class="an-rfc-item"><span class="an-rfc-rfc">${x.rfc}</span><span class="an-rfc-monto">${fmt(x.monto)}</span></li>`
    ).join('') || '<li class="an-rfc-item" style="color:#475569">Sin datos</li>';

    const alertBadge = (label, count, type) => {
      if (!count) return `<span class="an-alert-badge alert-ok">✓ ${label}: 0</span>`;
      return `<span class="an-alert-badge ${type}">${count} ${label}</span>`;
    };

    return `
      <div class="an-cards an-cards-4">
        <div class="an-card"><div class="an-card-label">Total CFDIs</div><div class="an-card-value text-money-white">${d.total_cfdi}</div>${delta(d.delta_vs_anterior.total)}</div>
        <div class="an-card"><div class="an-card-label">Emitidos</div><div class="an-card-value text-money-green">${d.emitidos}</div>${delta(d.delta_vs_anterior.emitidos)}</div>
        <div class="an-card"><div class="an-card-label">Recibidos</div><div class="an-card-value text-money-blue">${d.recibidos}</div>${delta(d.delta_vs_anterior.recibidos)}</div>
        <div class="an-card"><div class="an-card-label">Resultado Est.</div><div class="an-card-value ${d.resultado_estimado >= 0 ? 'text-money-green' : 'text-money-red'}">${fmt(d.resultado_estimado)}</div></div>
      </div>
      <div class="an-cards an-cards-4">
        <div class="an-card"><div class="an-card-label">Facturado</div><div class="an-card-value text-money-green">${fmtK(d.monto_facturado)}</div></div>
        <div class="an-card"><div class="an-card-label">Gastos</div><div class="an-card-value text-money-blue">${fmtK(d.monto_recibido)}</div></div>
        <div class="an-card"><div class="an-card-label">Ticket Prom.</div><div class="an-card-value text-money-purple">${fmt(d.ticket_promedio)}</div></div>
        <div class="an-card"><div class="an-card-label">Factura Máx.</div><div class="an-card-value text-money-amber">${fmt(d.factura_max)}</div></div>
      </div>
      <div class="an-cards an-cards-2">
        <div class="an-card">
          <div class="an-section-title">Tipo de Comprobante</div>
          <div class="an-bar-group">${tipoBars}</div>
          <div class="an-section-title" style="margin-top:1rem">Forma de Pago</div>
          <div class="an-bar-group">${fpBars}</div>
        </div>
        <div class="an-card">
          <div class="an-section-title">Actividad Diaria</div>
          <div class="an-daily-chart">${dailyBars}</div>
          <div style="display:flex;justify-content:space-between;font-size:0.55rem;color:#475569;margin-top:2px">
            <span>1</span><span>${d.actividad_diaria.length}</span>
          </div>
        </div>
      </div>
      <div class="an-cards an-cards-2">
        <div class="an-card"><div class="an-section-title">Top 5 Clientes</div><ul class="an-rfc-list">${clientes}</ul></div>
        <div class="an-card"><div class="an-section-title">Top 5 Proveedores</div><ul class="an-rfc-list">${proveedores}</ul></div>
      </div>
      <div class="an-section-title">Alertas</div>
      <div class="an-alerts">
        ${alertBadge('Cancelados', d.alertas.cancelados, 'alert-danger')}
        ${alertBadge('Efectivo no deducible', d.alertas.efectivo_no_deducible, 'alert-warn')}
        ${alertBadge('PPD sin complemento', d.alertas.ppd_sin_complemento, 'alert-warn')}
        ${alertBadge('Listas negras', d.alertas.listas_negras, 'alert-danger')}
      </div>`;
  }

  // ── Render: Fiscal ─────────────────────────────────────────────────

  function renderFiscal(d) {
    const motivoBars = d.deducibilidad.motivos.map((x, i) =>
      `<div class="an-bar-item">
        <span class="an-bar-label" style="width:160px;font-size:0.65rem">${x.motivo}</span>
        <div class="an-bar-track"><div class="an-bar-fill bar-red" style="width:${Math.min(100, (x.monto / (d.gastos_no_deducibles || 1)) * 100)}%">${fmtK(x.monto)}</div></div>
        <span class="an-bar-value">${x.count}</span>
      </div>`
    ).join('') || '<div style="color:#475569;font-size:0.75rem">Sin motivos de no deducibilidad</div>';

    return `
      <div class="an-cards an-cards-3">
        <div class="an-card"><div class="an-card-label">Ingresos</div><div class="an-card-value text-money-green">${fmtK(d.ingresos)}</div>${deltaPct(d.delta_ingresos)}</div>
        <div class="an-card"><div class="an-card-label">Gastos Deducibles</div><div class="an-card-value text-money-blue">${fmtK(d.gastos_deducibles)}</div>${deltaPct(d.delta_gastos)}</div>
        <div class="an-card"><div class="an-card-label">Utilidad Fiscal</div><div class="an-card-value ${d.utilidad_fiscal >= 0 ? 'text-money-green' : 'text-money-red'}">${fmtK(d.utilidad_fiscal)}</div></div>
      </div>
      <div class="an-cards an-cards-4">
        <div class="an-card"><div class="an-card-label">ISR Provisional (30%)</div><div class="an-card-value text-money-amber">${fmt(d.isr_provisional)}</div></div>
        <div class="an-card"><div class="an-card-label">Ret. ISR</div><div class="an-card-value text-money-cyan">${fmt(d.retenciones_isr)}</div></div>
        <div class="an-card"><div class="an-card-label">Ret. IVA</div><div class="an-card-value text-money-cyan">${fmt(d.retenciones_iva)}</div></div>
        <div class="an-card"><div class="an-card-label">No Deducible</div><div class="an-card-value text-money-red">${fmt(d.gastos_no_deducibles)}</div></div>
      </div>
      <div class="an-cards an-cards-2">
        <div class="an-card">
          <div class="an-section-title">Deducibilidad</div>
          <div class="an-deducibilidad-bar">
            <div class="an-deducibilidad-fill bar-green" style="width:${d.deducibilidad.deducible_pct}%">${d.deducibilidad.deducible_pct}% Deducible</div>
            <div class="an-deducibilidad-fill bar-red" style="width:${d.deducibilidad.no_deducible_pct}%">${d.deducibilidad.no_deducible_pct > 5 ? d.deducibilidad.no_deducible_pct + '%' : ''}</div>
          </div>
        </div>
        <div class="an-card">
          <div class="an-section-title">Motivos No Deducibles</div>
          ${motivoBars}
        </div>
      </div>`;
  }

  // ── Render: IVA ────────────────────────────────────────────────────

  function renderIVA(d) {
    const maxTendencia = Math.max(...d.tendencia.map(x => Math.abs(x.iva_pagar)), 1);
    const tendBars = d.tendencia.map((x, i) =>
      `<div class="an-bar-item">
        <span class="an-bar-label" style="width:60px;font-size:0.65rem">${x.mes}</span>
        <div class="an-bar-track"><div class="an-bar-fill ${x.iva_pagar >= 0 ? 'bar-indigo' : 'bar-red'}" style="width:${(Math.abs(x.iva_pagar) / maxTendencia) * 100}%">${fmtK(x.iva_pagar)}</div></div>
      </div>`
    ).join('');

    const maxTasa = Math.max(...d.por_tasa.map(x => x.monto), 1);
    const tasaBars = d.por_tasa.map((x, i) =>
      `<div class="an-bar-item">
        <span class="an-bar-label">${x.tasa}</span>
        <div class="an-bar-track"><div class="an-bar-fill ${BAR_COLORS[i]}" style="width:${(x.monto / maxTasa) * 100}%">${fmtK(x.monto)}</div></div>
      </div>`
    ).join('');

    return `
      <div class="an-iva-flow">
        <div class="an-iva-box"><div class="an-iva-box-label">IVA Trasladado</div><div class="an-iva-box-value text-money-green">${fmt(d.iva_trasladado)}</div></div>
        <span class="an-iva-arrow">−</span>
        <div class="an-iva-box"><div class="an-iva-box-label">IVA Acreditable</div><div class="an-iva-box-value text-money-blue">${fmt(d.iva_acreditable)}</div></div>
        <span class="an-iva-arrow">=</span>
        <div class="an-iva-box" style="border-color:rgba(99,102,241,0.3)"><div class="an-iva-box-label">IVA por Pagar</div><div class="an-iva-box-value ${d.iva_por_pagar >= 0 ? 'text-money-amber' : 'text-money-green'}">${fmt(d.iva_por_pagar)}</div></div>
      </div>
      <div class="an-cards an-cards-3">
        <div class="an-card"><div class="an-card-label">IVA Retenido</div><div class="an-card-value text-money-cyan">${fmt(d.iva_retenido)}</div></div>
        <div class="an-card"><div class="an-card-label">IVA Efectivo No Acreditable</div><div class="an-card-value text-money-red">${fmt(d.iva_efectivo_no_acreditable)}</div></div>
        <div class="an-card"><div class="an-card-label">IVA Neto a Enterar</div><div class="an-card-value text-money-amber">${fmt(d.iva_por_pagar - d.iva_retenido)}</div></div>
      </div>
      <div class="an-cards an-cards-2">
        <div class="an-card">
          <div class="an-section-title">IVA por Tasa</div>
          ${tasaBars}
        </div>
        <div class="an-card">
          <div class="an-section-title">Tendencia IVA a Pagar (6 meses)</div>
          ${tendBars}
        </div>
      </div>`;
  }

  // ── Public API ─────────────────────────────────────────────────────

  async function loadAnalysis(type) {
    const params = getParams();
    if (!params) {
      alert('Selecciona empresa, año y mes para usar las herramientas de análisis.');
      return;
    }

    const titles = {
      summary: '📊 Resumen Rápido',
      fiscal: '💰 Análisis Fiscal',
      iva: '🧾 IVA del Periodo',
    };
    const renderers = { summary: renderSummary, fiscal: renderFiscal, iva: renderIVA };

    showLoading(titles[type]);
    try {
      const data = await fetchData(type, params);
      openModal(titles[type], renderers[type](data));
    } catch (err) {
      openModal(titles[type], `<div class="an-skeleton" style="min-height:100px"><span style="color:#f87171;font-size:0.8rem">❌ ${err.message}</span></div>`);
    }
  }

  window.loadAnalysis = loadAnalysis;
})();
