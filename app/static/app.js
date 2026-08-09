/* rs3graph_webui — frontend logic.
   - Watch page: market selector, background refresh with progress polling,
     lazy-rendered Chart.js cards (IntersectionObserver).
   - Admin page: market CRUD + item management with live wiki lookup.

   Charts replicate the original matplotlib styling:
   - continuous gap-filled price lines (forward fill + back fill leading)
   - markers ONLY at real data points
   - 5-minute volume bars (buy green positive / sell orange negative)
   - x-axis formatted in EST (America/New_York)
   - tooltips show all four fields with "(N Minutes Ago)" for missing data
 */
(function () {
  'use strict';

  const COLORS = {
    bg: '#343434',
    green: '#2ff259',
    orange: '#f2802f',
    text: '#ababab',
    grid: 'rgba(171,171,171,0.25)',
  };

  /* ── Crosshair plugin ────────────────────────────────────────────────
     Draws a dashed vertical line at the hovered time on BOTH charts of a
     card (price + volume) so the user can visually align the time axis.

     Chart.js has no cross-chart hover: hovering one chart does NOT activate
     elements on the other. So the plugin keeps per-card shared state: when
     either chart is hovered, it records the hovered data point's timestamp
     and redraws every chart in that card; each chart then draws its line at
     getPixelForValue(ts). Because both charts share the same x scale
     (getXScale with offset:false) and reserve the same fixed y-axis width
     (see yScale below), the pixel x matches exactly on both charts, so the
     line visually spans the two stacked charts. */
  const crosshairByCard = new Map(); // cardEl -> { ts: number|null, charts: Set }

  // Per-card repaint guard. Hovering one chart must propagate the crosshair
  // line to ALL charts in the card in a single, coalesced pass. Because both
  // charts reserve the same fixed y-axis width (see yScale below), their plot
  // areas line up, so getPixelForValue(ts) returns the same pixel x on both.
  // Each chart's afterDraw draws the dashed line on its own canvas; we just
  // need to force every chart in the card to redraw once per hover change.
  // Rapid mousemove between the two charts is coalesced into one rAF per
  // frame, and a re-entrancy flag stops an update from starting while another
  // is in flight. update('none') lays out once then draws — no axis mutation
  // (no afterFit hook), so the layout stays stable and ghosting never occurs.
  const crosshairSched = new Map(); // cardEl -> { raf|0, inFlight:boolean }

  function crosshairCardOf(chart) {
    return chart.canvas && chart.canvas.closest
      ? chart.canvas.closest('.card') || null
      : null;
  }

  const crosshairPlugin = {
    id: 'crosshair',
    afterInit(chart) {
      const card = crosshairCardOf(chart);
      if (!card) return;
      let entry = crosshairByCard.get(card);
      if (!entry) {
        entry = { ts: null, charts: new Set() };
        crosshairByCard.set(card, entry);
      }
      entry.charts.add(chart);
      chart.$crosshairCard = card;
    },
    destroy(chart) {
      const card = chart.$crosshairCard;
      if (!card) return;
      const entry = crosshairByCard.get(card);
      if (entry) {
        entry.charts.delete(chart);
        if (!entry.charts.size) {
          crosshairByCard.delete(card);
        }
      }
    },
    afterEvent(chart) {
      const card = chart.$crosshairCard;
      const entry = card && crosshairByCard.get(card);
      if (!entry) return;

      // Hovered data point's timestamp (null when the mouse leaves /
      // nothing is hovered). Same index across all datasets in the card.
      let ts = null;
      const active = chart.getActiveElements();
      if (active.length) {
        const ds = chart.data.datasets[active[0].datasetIndex];
        const pt = ds && ds.data[active[0].index];
        if (pt && pt.x !== undefined && pt.x !== null) ts = pt.x;
      }

      // Mouse leaving THIS chart while another chart in the same card is
      // still hovered must NOT clear the shared line (the other chart's
      // mousemove will have already updated the timestamp).
      if (ts === null) {
        let otherHovered = false;
        for (const c of entry.charts) {
          if (c !== chart && c.getActiveElements().length) {
            otherHovered = true;
            break;
          }
        }
        if (otherHovered) return;
      }

      if (ts === entry.ts) return; // no change → nothing to redraw
      entry.ts = ts;

      scheduleCrosshairRepaint(card);
    },
    afterDraw(chart) {
      const card = chart.$crosshairCard;
      const entry = card && crosshairByCard.get(card);
      if (!entry || entry.ts === null) return;

      const x = chart.scales.x.getPixelForValue(entry.ts);
      if (x < chart.chartArea.left || x > chart.chartArea.right) return;

      const ctx = chart.ctx;
      const top = chart.chartArea.top;
      const bottom = chart.chartArea.bottom;
      ctx.save();
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.lineWidth = 1;
      ctx.strokeStyle = 'rgba(171, 171, 171, 0.5)';
      ctx.setLineDash([5, 5]);
      ctx.stroke();
      ctx.restore();
    },
  };

  /* Redraw the crosshair on every chart in the card WITHOUT triggering a
     full Chart.js update/layout cycle. We call chart.draw() which simply
     re-paints the already-computed layout. This is much cheaper than
     update('none') and does not re-run scale fitting, so the canvas never
     shifts position — no ghosting. The hovered chart naturally redraws
     itself via Chart.js's internal hover loop. */
  function scheduleCrosshairRepaint(card) {
    let sched = crosshairSched.get(card);
    if (!sched) {
      sched = { raf: 0 };
      crosshairSched.set(card, sched);
    }
    if (sched.raf) return; // a repaint is already queued for this frame

    sched.raf = requestAnimationFrame(() => {
      sched.raf = 0;
      const cur = crosshairByCard.get(card);
      if (!cur) return;
      for (const c of [...cur.charts]) {
        // Chart.js v4 destroy() nulls ctx and canvas; it does NOT set a
        // `_destroyed` flag, so the old guard never matched. A disposed
        // chart left in the set must not be drawn: draw() on it would
        // throw and abort this loop, orphaning the remaining (valid)
        // charts' crosshair lines after scrolling. Prune any such chart so
        // stale references can't accumulate / ghost across scroll cycles.
        if (c.ctx == null || c.canvas == null) {
          cur.charts.delete(c);
          continue;
        }
        try {
          // draw() re-runs the paint pipeline (including plugin afterDraw)
          // without touching layout — fast and ghost-free. Guard each chart
          // so a single failure can't deprive the sibling charts of their
          // crosshair line (the original 'bottom chart goes blank' bug).
          c.draw();
        } catch (err) {
          cur.charts.delete(c); // drop any chart that fails to paint
        }
      }
      if (!cur.charts.size) crosshairByCard.delete(card);
    });
  }
  // Register globally so every chart gets the crosshair. Guarded: the admin
  // page loads this script without Chart.js.
  if (typeof Chart !== 'undefined') {
    Chart.register(crosshairPlugin);
  }

  /* Timezone handling — defaults to EST (America/New_York), switchable via
     the #tz-select dropdown on the watch page. */
  let currentTz = 'America/New_York';

  function getTimeFormatter(tz) {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hour12: false,
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  // Hour-only formatter for the x-axis tick labels (HH, no minutes).
  function getHourFormatter(tz) {
    return new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      hour12: false,
      hour: '2-digit',
    });
  }

  // Format a timestamp in the currently selected timezone. (Named estTime
  // for historical reasons; it now honors currentTz.)
  function estTime(ms) {
    return getTimeFormatter(currentTz).format(new Date(ms));
  }

  // Hour-only variant used on the time axis (the tooltip titles keep the
  // full HH:MM so the exact hovered slot is still identifiable).
  function estTimeHour(ms) {
    return getHourFormatter(currentTz).format(new Date(ms));
  }

  // Get the timezone abbreviation for display ("EST", "CST", "UTC", …).
  function getTzAbbr(tz) {
    const now = new Date();
    const fmt = new Intl.DateTimeFormat('en-US', {
      timeZone: tz,
      timeZoneName: 'short',
    });
    const parts = fmt.formatToParts(now);
    const tzPart = parts.find((p) => p.type === 'timeZoneName');
    return tzPart ? tzPart.value : tz.split('/').pop();
  }

  function fmtNum(v) {
    if (v === null || v === undefined || Number.isNaN(Number(v))) return '—';
    return Math.round(Number(v)).toLocaleString('en-US');
  }

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  async function api(path, opts) {
    const resp = await fetch(path, opts);
    if (!resp.ok) {
      let detail = resp.statusText;
      try { const j = await resp.json(); detail = j.detail || detail; } catch (e) { /* keep default */ }
      throw new Error(detail);
    }
    return resp.json();
  }

  function ago(deltaMs) {
    const mins = Math.round(deltaMs / 60000);
    if (mins < 60) return ` (${mins} Minute${mins === 1 ? '' : 's'} Ago)`;
    const hrs = mins / 60;
    return ` (${hrs < 10 ? hrs.toFixed(1) : Math.round(hrs)} Hours Ago)`;
  }

  /* Actual value + "(N Minutes/Hours Ago)" note for a series at slot i.
     When the slot has a real value it is returned as-is (never annotated,
     even when that value is 0). When the slot is gap-filled (real-mask
     FALSE — data genuinely missing/None from the API), the LAST REAL value
     BEFORE the slot is returned instead of the interpolated point value,
     annotated with how long ago it was recorded; leading gaps (no real
     value behind) fall back to the first real value after the slot. */
  function actualValue(S, real, arr, i) {
    if (real[i]) return { value: arr[i], note: '' };
    for (let j = i - 1; j >= 0; j--) {
      if (real[j]) return { value: arr[j], note: ago(S.timestamps[i] - S.timestamps[j]) };
    }
    for (let j = i + 1; j < S.timestamps.length; j++) {
      if (real[j]) return { value: arr[j], note: ago(S.timestamps[j] - S.timestamps[i]) };
    }
    return { value: null, note: '' };
  }

  /* Convert a parallel value array into Chart.js {x, y} points (linear x). */
  function xy(S, arr) {
    return S.timestamps.map((t, i) => ({ x: t, y: arr[i] }));
  }

  const tooltipStyle = {
    backgroundColor: '#ffffff',
    titleColor: '#1a1a1a',
    bodyColor: '#333333',
    borderColor: 'rgba(0,0,0,0.15)',
    borderWidth: 1,
    padding: 10,
    titleFont: { size: 14 },
    bodyFont: { size: 13 },
  };

  /* ── Cursor-following tooltip ────────────────────────────────────────
     Rendered as ONE shared DOM element via Chart.js's external tooltip
     hook (tooltip.enabled:false so Chart.js never paints its own box).
     This lets the tooltip follow the mouse cursor with a fixed offset
     instead of being anchored to the hovered data point. Every chart
     stores its series on chart.$series; the handler always shows the four
     lines in order — Buy Price / Sell Price / Buy Vol / Sell Vol — each
     with its colored dot, using ACTUAL last-known values (not the
     interpolated gap-fill) for missing data. */
  const TOOLTIP_OFFSET = 15;

  const tooltipEl = (() => {
    const el = document.createElement('div');
    el.style.cssText = [
      'position:fixed',
      'z-index:9999',
      'pointer-events:none',
      'background:#ffffff',
      'color:#333333',
      'border:1px solid rgba(0,0,0,0.15)',
      'border-radius:6px',
      'padding:10px 12px',
      'box-shadow:0 4px 14px rgba(0,0,0,0.3)',
      'font:13px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif',
      'white-space:nowrap',
      'opacity:0',
      'transition:opacity 0.12s ease',
      'left:0',
      'top:0',
    ].join(';');
    document.body.appendChild(el);
    return el;
  })();

  let tooltipMouse = { x: 0, y: 0 }; // last cursor position over a chart
  let tooltipShown = false;

  // Place the tooltip ~15px right/below the cursor, flipping to the other
  // side when it would run off the viewport edge.
  function positionExternalTooltip() {
    const w = tooltipEl.offsetWidth;
    const h = tooltipEl.offsetHeight;
    let left = tooltipMouse.x + TOOLTIP_OFFSET;
    let top = tooltipMouse.y + TOOLTIP_OFFSET;
    if (left + w > window.innerWidth - 8) left = tooltipMouse.x - w - TOOLTIP_OFFSET;
    if (top + h > window.innerHeight - 8) top = tooltipMouse.y - h - TOOLTIP_OFFSET;
    tooltipEl.style.left = left + 'px';
    tooltipEl.style.top = top + 'px';
  }

  // Track the cursor while it is over any chart so the tooltip follows it
  // smoothly even between Chart.js hover updates (Chart.js only re-renders
  // the tooltip when the hovered data index changes).
  document.addEventListener('mousemove', (e) => {
    const t = e.target;
    if (!t || !t.closest || !t.closest('.chart-wrap')) return;
    tooltipMouse.x = e.clientX;
    tooltipMouse.y = e.clientY;
    if (tooltipShown) positionExternalTooltip();
  });

  // One tooltip line: colored dot + "Label: value GP(N Minutes Ago)".
  function tooltipRow(label, info, suffix, color) {
    return (
      '<div style="display:flex;align-items:center;gap:8px;line-height:1.6">' +
      '<span style="width:9px;height:9px;border-radius:50%;flex:0 0 auto;' +
      'display:inline-block;background:' + color + '"></span>' +
      '<span style="color:#333333">' + escapeHtml(label) + ': ' +
      escapeHtml(fmtNum(info.value)) + suffix + escapeHtml(info.note) + '</span>' +
      '</div>'
    );
  }

  // Chart.js external tooltip handler (set via tooltip.external).
  function renderExternalTooltip(context) {
    const { chart, tooltip } = context;
    if (!tooltip.opacity || !tooltip.dataPoints || !tooltip.dataPoints.length) {
      tooltipEl.style.opacity = '0';
      tooltipShown = false;
      return;
    }
    const S = chart.$series;
    if (!S) return;
    const i = tooltip.dataPoints[0].dataIndex;
    if (tooltipEl._chart !== chart || tooltipEl._index !== i) {
      tooltipEl._chart = chart;
      tooltipEl._index = i;
      tooltipEl.innerHTML =
        '<div style="color:#1a1a1a;font-size:14px;font-weight:600;margin-bottom:6px;' +
        'padding-bottom:6px;border-bottom:1px solid rgba(0,0,0,0.1)">' +
        escapeHtml(estTime(S.timestamps[i])) + '</div>' +
        tooltipRow('Buy Price', actualValue(S, S.highReal, S.highPrice, i), ' GP', COLORS.green) +
        tooltipRow('Sell Price', actualValue(S, S.lowReal, S.lowPrice, i), ' GP', COLORS.orange) +
        tooltipRow('Buy Vol', actualValue(S, S.highVolReal, S.highVolume, i), '', COLORS.green) +
        tooltipRow('Sell Vol', actualValue(S, S.lowVolReal, S.lowVolume, i), '', COLORS.orange);
    }
    tooltipEl.style.opacity = '1';
    tooltipShown = true;
    positionExternalTooltip();
  }

  /* Shared x-axis config for both price and volume charts.
     - min snaps to the start of the hour before the first data point
     - max = 1.5% padding beyond the last data point, rounded up to the
       next full hour (hour-snapped so stepSize yields exact-hour ticks)
     - ticks are generated at exact 1-hour intervals within the range
     - Both charts use IDENTICAL min/max so their time axes line up */
  function getXScale(S) {
    const firstTs = S.timestamps[0];
    const lastTs = S.timestamps[S.timestamps.length - 1];
    const span = lastTs - firstTs;
    const min = Math.floor(firstTs / 3600000) * 3600000; // snap to hour start
    // 1.5% right padding, rounded UP to the next full hour. Chart.js v4 only
    // honors stepSize when (max - min) is an exact multiple of it (see
    // generateTicks: almostWhole((max-min)/step, spacing/1000)); otherwise it
    // redistributes evenly and ticks land at odd times (21:28, 2:40, …).
    // Because min is hour-snapped, an hour-snapped max guarantees hourly ticks.
    const max = Math.ceil((lastTs + span * 0.015) / 3600000) * 3600000;

    return {
      type: 'linear',
      min,
      max,
      // offset:false on BOTH charts (the bar controller would default the
      // volume chart to offset:true) so the value→pixel mapping is
      // identical — required for the crosshair to line up across charts.
      offset: false,
      ticks: {
        color: COLORS.text,
        callback: (v) => estTimeHour(v),
        font: { size: 13 },
        maxRotation: 0,
        autoSkip: true,
        maxTicksLimit: 30, // high enough that hourly ticks are never skipped
        stepSize: 3600000, // 1 hour in ms → ticks land on exact hours
      },
      grid: { color: COLORS.grid },
      border: { color: COLORS.text },
      title: {
        display: true,
        text: `Time (${getTzAbbr(currentTz)})`,
        color: COLORS.text,
        font: { size: 13 },
      },
    };
  }
  /* Both charts must reserve the SAME y-axis width so their plot areas
     (and therefore the crosshair's pixel x) line up exactly. Chart.js
     computes each axis's "natural" width from its tick labels, and the
     price/volume labels differ in width. We fix this ONCE after creation
     by overriding the scale's fit() method to a fixed width. Because
     chart.draw() (used for hover repaints) never calls fit(), there is no
     frame-to-frame width mutation and no ghosting. */

  function fixAxisWidth(chart, width) {
    const scale = chart.scales.y;
    const orig = scale.fit.bind(scale);
    scale.fit = function () {
      orig();
      this.width = width;
    };
  }

  const yScale = {
    ticks: { color: COLORS.text, callback: (v) => fmtNum(v), font: { size: 13 } },
    grid: { color: COLORS.grid },
    border: { color: COLORS.text },
  };

  const legendOpts = (pointStyle) => ({
    labels: {
      color: COLORS.text,
      usePointStyle: true,
      pointStyle,
      boxWidth: 24,
      font: { size: 13 },
      filter: (item, chartData) => !chartData.datasets[item.datasetIndex].markerOnly,
    },
  });

  const indexInteraction = { mode: 'index', intersect: false };

  /* ── Price chart ──────────────────────────────────────────────────── */

  function buildPriceChart(canvas, data) {
    const S = data.series;

    const buyLine = {
      label: 'Buy Price',
      data: xy(S, S.highPrice),
      borderColor: COLORS.green,
      backgroundColor: COLORS.green,
      borderWidth: 2,
      tension: 0,
      pointRadius: 0,
      pointHoverRadius: 0,
      order: 2,
    };
    const sellLine = {
      label: 'Sell Price',
      data: xy(S, S.lowPrice),
      borderColor: COLORS.orange,
      backgroundColor: COLORS.orange,
      borderWidth: 2,
      tension: 0,
      pointRadius: 0,
      pointHoverRadius: 0,
      order: 2,
    };

    // Markers ONLY where real API data exists (radius 0 elsewhere).
    // 2.5px radius = 5px diameter (+0.5px border each side ≈ 6px visual) —
    // the user asked for ~5px dots, not the old 5px RADIUS (10px diameter).
    const mk = (color, values, real) => ({
      markerOnly: true,
      data: xy(S, values),
      showLine: false,
      pointRadius: real.map((v) => (v ? 2.5 : 0)),
      pointHoverRadius: real.map((v) => (v ? 4 : 0)),
      pointBackgroundColor: color,
      pointBorderColor: COLORS.bg,
      pointBorderWidth: 0.5,
      order: 1,
    });

    const chart = new Chart(canvas, {
      type: 'line',
      data: {
        datasets: [buyLine, sellLine, mk(COLORS.green, S.highPrice, S.highReal), mk(COLORS.orange, S.lowPrice, S.lowReal)],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: indexInteraction,
        scales: { x: getXScale(S), y: { ...yScale } },
        plugins: {
          crosshair: {},
          legend: legendOpts('line'),
          tooltip: {
            ...tooltipStyle,
            enabled: false,
            position: 'nearest',
            external: renderExternalTooltip,
          },
        },
      },
    });
    chart.$series = S;
    return chart;
  }

  /* ── Volume chart ─────────────────────────────────────────────────── */

  function buildVolumeChart(canvas, data) {
    const S = data.series;

    // Bar width is VISUAL ONLY: fixed 4px thickness (appears ~6px with
    // aliasing) so each 5-minute bar is exactly 4px wide. With 288 data
    // points × 4px ≈ 1,152px of bar content plus margins, the chart spans
    // ~1,400px — matching the original matplotlib layout. The x-axis range
    // is shared with the price chart (getXScale) so both charts' time axes
    // line up exactly.
    const barThickness = 4;

    const chart = new Chart(canvas, {
      type: 'bar',
      data: {
        datasets: [
          {
            label: 'Buy Volume',
            data: xy(S, S.highVolume),
            backgroundColor: COLORS.green,
            borderColor: COLORS.bg,
            borderWidth: 0.8,
            barThickness: barThickness,
            order: 2,
          },
          {
            label: 'Sell Volume',
            data: xy(S, S.lowVolume.map((v) => -v)),
            backgroundColor: COLORS.orange,
            borderColor: COLORS.bg,
            borderWidth: 0.8,
            barThickness: barThickness,
            order: 1,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: indexInteraction,
        // grouped:false centers each bar EXACTLY on its data-value pixel
        // (scale.getPixelForValue), so bars line up with the price-chart
        // dots and the crosshair. The bar default (grouped:true) treats the
        // two volume datasets as a 2-bar group and offsets each by ±half
        // the group's chunk (2px with 4px bars), leaving a 4px gap between
        // the two bars and the crosshair running down the middle of that
        // gap. With grouped:false both bars share the same center (buy
        // drawn on top, order 2 > 1) — the intended matplotlib-style
        // single column.
        grouped: false,
        scales: {
          x: getXScale(S),
          // Sell bars are stored as negative values (to render below the
          // zero line) but the y-axis must show positive magnitudes, so
          // tick labels use the absolute value.
          y: {
            ...yScale,
            ticks: {
              ...yScale.ticks,
              callback: (v) => fmtNum(Math.abs(v)),
            },
          },
        },
        plugins: {
          crosshair: {},
          legend: legendOpts('rect'),
          tooltip: {
            ...tooltipStyle,
            enabled: false,
            position: 'nearest',
            external: renderExternalTooltip,
          },
        },
      },
    });
    chart.$series = S;
    return chart;
  }

  /* ─────────────────────────────────────────────────────────────────────
     Watch page
     ───────────────────────────────────────────────────────────────────── */

  const watchState = {
    marketId: null,
    markets: [],
    dataCache: new Map(), // item_id -> fetched payload
    charts: new Map(),    // item_id -> [priceChart, volumeChart]
    jobTimer: null,
  };

  const observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      const card = entry.target;
      if (entry.isIntersecting) renderCard(card);
      else destroyCardCharts(card);
    }
  }, { rootMargin: '300px 0px' });

  function showEmpty(html) {
    const el = document.getElementById('charts');
    el.innerHTML = `<div class="empty">${html}</div>`;
  }

  function makeCard(item) {
    const card = document.createElement('article');
    card.className = 'card';
    card.dataset.itemId = item.item_id;
    card.innerHTML =
      '<header class="card-head">' +
      `  <img class="card-icon" src="/icons/${encodeURIComponent(item.icon_name || '')}" ` +
      '       alt="" loading="lazy" onerror="this.style.display=\'none\'">' +
      `  <h2 class="card-title">${escapeHtml(item.item_name || 'Item ' + item.item_id)}</h2>` +
      '</header>' +
      '<div class="stats">' +
      '  <span class="stat buy"><span class="lbl">Buy</span> <b class="val" data-stat="buy">—</b></span>' +
      '  <span class="stat sell"><span class="lbl">Sell</span> <b class="val" data-stat="sell">—</b></span>' +
      '  <span class="stat vol"><span class="lbl">Vol (24h)</span> <b class="val" data-stat="vol">—</b></span>' +
      '</div>' +
      '<div class="chart-wrap price"><canvas></canvas></div>' +
      '<div class="chart-wrap volume"><canvas></canvas></div>' +
      '<div class="loading">Loading data…</div>';
    return card;
  }

  async function renderCard(card) {
    const itemId = Number(card.dataset.itemId);
    if (card.dataset.loading === '1') return;
    if (watchState.dataCache.has(itemId)) {
      paintCard(card, watchState.dataCache.get(itemId));
      return;
    }
    card.dataset.loading = '1';
    const loading = card.querySelector('.loading');
    try {
      const payload = await api(`/api/markets/${watchState.marketId}/items/${itemId}/data`);
      watchState.dataCache.set(itemId, payload);
      paintCard(card, payload);
    } catch (err) {
      if (loading) loading.textContent = 'Load failed: ' + err.message;
    } finally {
      card.dataset.loading = '0';
    }
  }

  function paintCard(card, data) {
    const key = Number(card.dataset.itemId);
    const existing = watchState.charts.get(key);
    if (existing) {
      existing.forEach((c) => c.destroy());
      watchState.charts.delete(key);
    }
    const loading = card.querySelector('.loading');
    if (loading) loading.remove();

    const S = data.series;
    if (!S.timestamps.length) {
      const msg = document.createElement('div');
      msg.className = 'loading';
      msg.textContent = 'No data yet — click Refresh.';
      card.appendChild(msg);
      return;
    }

    card.querySelector('[data-stat="buy"]').textContent = fmtNum(data.stats.latest_buy);
    card.querySelector('[data-stat="sell"]').textContent = fmtNum(data.stats.latest_sell);
    card.querySelector('[data-stat="vol"]').textContent = fmtNum(data.stats.total_volume);

    const priceCanvas = card.querySelector('.chart-wrap.price canvas');
    const volCanvas = card.querySelector('.chart-wrap.volume canvas');
    const priceChart = buildPriceChart(priceCanvas, data);
    const volChart = buildVolumeChart(volCanvas, data);

    // Let Chart.js compute natural widths once, then pin both axes to the
    // wider of the two so their plot areas align perfectly.
    priceChart.update('none');
    volChart.update('none');
    const targetWidth = Math.max(priceChart.scales.y.width, volChart.scales.y.width, 64);
    fixAxisWidth(priceChart, targetWidth);
    fixAxisWidth(volChart, targetWidth);
    priceChart.update('none');
    volChart.update('none');

    watchState.charts.set(key, [priceChart, volChart]);
  }

  function destroyCardCharts(card) {
    const key = Number(card.dataset.itemId);
    const charts = watchState.charts.get(key);
    if (charts) {
      charts.forEach((c) => c.destroy());
      watchState.charts.delete(key);
    }
    // Drop the ENTIRE crosshair entry (not just ts) so a stale chart
    // reference can never survive the scroll-away / scroll-back cycle and
    // draw ghost lines onto a dead or recycled canvas. This, combined with
    // the disposed-chart pruning in scheduleCrosshairRepaint, prevents both
    // the ghosting that accumulates after scrolling and the 'crosshair
    // vanishes from the bottom chart' symptom.
    crosshairByCard.delete(card);
    const sched = crosshairSched.get(card);
    if (sched) {
      if (sched.raf) cancelAnimationFrame(sched.raf);
      crosshairSched.delete(card);
    }
  }

  function destroyAllCharts() {
    watchState.charts.forEach((charts) => charts.forEach((c) => c.destroy()));
    watchState.charts.clear();
  }

  /* Rebuild all visible charts after the timezone changes. Chart.js linear
     axes can't swap their tick formatter in place across every card, so the
     simplest reliable approach is clearing the data cache and re-selecting
     the current market, which destroys and recreates all charts (and the
     x-axis title) with the new timezone. */
  function reloadChartsWithNewTz() {
    if (watchState.marketId == null) return;
    watchState.dataCache.clear();
    selectMarket(watchState.marketId).catch(() => {});
  }

  async function selectMarket(id) {
    watchState.marketId = id;
    watchState.dataCache.clear();
    destroyAllCharts();

    const chartsEl = document.getElementById('charts');
    chartsEl.innerHTML = '';
    document.getElementById('refresh-status').textContent = '';
    const btn = document.getElementById('refresh-btn');
    btn.disabled = false;
    btn.textContent = '⟳ Refresh';

    const m = watchState.markets.find((x) => x.id === id);
    const metaEl = document.getElementById('market-meta');
    metaEl.textContent = m
      ? `${m.item_count} item${m.item_count === 1 ? '' : 's'} · last refreshed: ` +
        (m.last_refresh ? new Date(m.last_refresh).toLocaleString() : 'never')
      : '';

    const items = await api(`/api/markets/${id}/items`);
    if (!items.length) {
      showEmpty('No items in this market. <a href="/admin">Add items in the Admin panel</a>.');
      return;
    }
    for (const item of items) {
      const card = makeCard(item);
      chartsEl.appendChild(card);
      observer.observe(card);
    }
  }

  async function reloadAfterRefresh() {
    watchState.markets = await api('/api/markets');
    watchState.dataCache.clear();
    await selectMarket(watchState.marketId);
  }

  async function refreshMarket() {
    if (!watchState.marketId) return;
    const btn = document.getElementById('refresh-btn');
    const statusEl = document.getElementById('refresh-status');
    btn.disabled = true;
    btn.textContent = '⟳ Refreshing…';
    statusEl.textContent = 'Starting…';

    let jobId;
    try {
      const j = await api(`/api/markets/${watchState.marketId}/refresh`, { method: 'POST' });
      jobId = j.job_id;
    } catch (err) {
      statusEl.textContent = 'Refresh failed: ' + err.message;
      btn.disabled = false;
      btn.textContent = '⟳ Refresh';
      return;
    }

    watchState.jobTimer = setInterval(async () => {
      try {
        const st = await api(`/api/jobs/${jobId}`);
        if (st.state === 'running') {
          statusEl.textContent = `Fetching ${st.done}/${st.total}: ${st.current || '…'}`;
          return;
        }
        clearInterval(watchState.jobTimer);
        watchState.jobTimer = null;
        btn.disabled = false;
        btn.textContent = '⟳ Refresh';
        if (st.state === 'error') {
          statusEl.textContent = 'Refresh error: ' + st.message;
        } else {
          statusEl.textContent = '';
          await reloadAfterRefresh();
        }
      } catch (err) {
        clearInterval(watchState.jobTimer);
        watchState.jobTimer = null;
        btn.disabled = false;
        btn.textContent = '⟳ Refresh';
        statusEl.textContent = 'Progress poll failed: ' + err.message;
      }
    }, 800);
  }

  async function initWatch() {
    watchState.markets = await api('/api/markets');
    const sel = document.getElementById('market-select');
    sel.innerHTML = '';
    for (const m of watchState.markets) {
      const opt = document.createElement('option');
      opt.value = m.id;
      opt.textContent = `${m.name} (${m.item_count})`;
      sel.appendChild(opt);
    }

    // Restore the last-selected timezone (rs3graph_tz) and market
    // (rs3graph_market) from localStorage so a page refresh keeps them.
    const tzSel = document.getElementById('tz-select');
    const savedTz = localStorage.getItem('rs3graph_tz');
    if (savedTz && [...tzSel.options].some((o) => o.value === savedTz)) {
      currentTz = savedTz;
      tzSel.value = savedTz;
    }

    sel.addEventListener('change', () => {
      localStorage.setItem('rs3graph_market', String(sel.value));
      selectMarket(Number(sel.value));
    });
    tzSel.addEventListener('change', (e) => {
      currentTz = e.target.value;
      localStorage.setItem('rs3graph_tz', currentTz);
      reloadChartsWithNewTz();
    });
    document.getElementById('refresh-btn').addEventListener('click', refreshMarket);

    if (watchState.markets.length) {
      // Saved market wins if it still exists; otherwise fall back to the
      // first market in the list.
      const savedMarket = localStorage.getItem('rs3graph_market');
      const target = watchState.markets.find((m) => String(m.id) === savedMarket)
        || watchState.markets[0];
      sel.value = target.id;
      await selectMarket(target.id);
    } else {
      showEmpty('No markets yet. <a href="/admin">Create one in the Admin panel</a>.');
    }
  }

  /* ─────────────────────────────────────────────────────────────────────
     Admin page
     ───────────────────────────────────────────────────────────────────── */

  const adminState = {
    markets: [],
    currentMarketId: null,
    pendingAdd: null,
  };

  /* Import a .txt file (one item ID per line) into the current market.
     Order in the file is preserved as display order. */
  async function importItemsFile(ev) {
    ev.preventDefault();
    const input = document.getElementById('import-file');
    const statusEl = document.getElementById('import-status');
    statusEl.textContent = '';
    if (!adminState.currentMarketId) {
      statusEl.textContent = 'Select a market first.';
      return;
    }
    if (!input.files || !input.files.length) {
      statusEl.textContent = 'Choose a .txt file first.';
      return;
    }

    let text;
    try {
      text = await input.files[0].text();
    } catch (err) {
      statusEl.textContent = 'Could not read file: ' + err.message;
      return;
    }

    const ids = [];
    for (const line of text.split(/[\r\n]+/)) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const n = Number(trimmed.replace(/[^0-9]/g, ''));
      if (!Number.isInteger(n) || n <= 0) continue;
      ids.push(n);
    }
    if (!ids.length) {
      statusEl.textContent = 'No valid item IDs found in the file.';
      return;
    }

    try {
      const res = await api(`/api/markets/${adminState.currentMarketId}/import`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_ids: ids }),
      });
      const parts = [];
      if (res.added.length) parts.push(`added ${res.added.length}`);
      if (res.skipped.length) parts.push(`skipped ${res.skipped.length} (already present)`);
      if (res.not_found.length) parts.push(`not found ${res.not_found.length}`);
      statusEl.textContent = 'Import done: ' + (parts.join(', ') || 'nothing to do');
      input.value = '';
      adminState.markets = await api('/api/markets');
      renderMarketList();
      await reloadItemsTable();
    } catch (err) {
      statusEl.textContent = 'Import failed: ' + err.message;
    }
  }

  function mkIconBtn(label, title, onClick) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'icon-btn';
    b.title = title;
    b.textContent = label;
    b.addEventListener('click', onClick);
    return b;
  }

  function renderMarketList() {
    const ul = document.getElementById('market-list');
    ul.innerHTML = '';
    for (const m of adminState.markets) {
      const li = document.createElement('li');
      li.className = 'market-row' + (m.id === adminState.currentMarketId ? ' active' : '');

      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'market-select';
      btn.innerHTML = `${escapeHtml(m.name)} <span class="muted">(${m.item_count})</span>`;
      btn.addEventListener('click', () => selectAdminMarket(m.id));

      const ren = mkIconBtn('✎', 'Rename market', () => renameMarket(m));
      const del = mkIconBtn('🗑', 'Delete market', () => deleteMarket(m));
      del.classList.add('danger');

      li.append(btn, ren, del);
      ul.appendChild(li);
    }
  }

  async function reloadItemsTable() {
    const tbody = document.querySelector('#items-table tbody');
    tbody.innerHTML = '';
    if (!adminState.currentMarketId) return;

    const m = adminState.markets.find((x) => x.id === adminState.currentMarketId);
    document.getElementById('panel-title').textContent = m ? `${m.name} — Items` : 'Select a market';

    const items = await api(`/api/markets/${adminState.currentMarketId}/items`);
    if (!items.length) {
      tbody.innerHTML = '<tr><td colspan="4" class="muted">No items yet — add one above.</td></tr>';
      return;
    }
    for (const it of items) {
      const tr = document.createElement('tr');

      const tdIcon = document.createElement('td');
      const img = document.createElement('img');
      img.className = 'mini-icon';
      img.src = `/icons/${encodeURIComponent(it.icon_name || '')}`;
      img.alt = '';
      img.onerror = () => { img.style.display = 'none'; };
      tdIcon.appendChild(img);

      const tdName = document.createElement('td');
      tdName.textContent = it.item_name || `Item ${it.item_id}`;

      const tdId = document.createElement('td');
      tdId.className = 'muted';
      tdId.textContent = it.item_id;

      const tdAct = document.createElement('td');
      const rm = mkIconBtn('✕', 'Remove item', () => removeItem(it.item_id));
      rm.classList.add('danger');
      tdAct.appendChild(rm);

      tr.append(tdIcon, tdName, tdId, tdAct);
      tbody.appendChild(tr);
    }
  }

  async function selectAdminMarket(id) {
    adminState.currentMarketId = id;
    adminState.pendingAdd = null;
    document.getElementById('lookup-preview').hidden = true;
    document.getElementById('lookup-error').textContent = '';
    renderMarketList();
    await reloadItemsTable();
  }

  async function createMarket(ev) {
    ev.preventDefault();
    const input = document.getElementById('new-market-name');
    const name = input.value.trim();
    const errEl = document.getElementById('lookup-error');
    if (!name) return;
    try {
      const created = await api('/api/markets', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      });
      input.value = '';
      adminState.markets = await api('/api/markets');
      errEl.textContent = '';
      await selectAdminMarket(created.id);
    } catch (err) {
      errEl.textContent = 'Create failed: ' + err.message;
    }
  }

  async function renameMarket(m) {
    const name = prompt('New name for market:', m.name);
    if (!name || !name.trim() || name.trim() === m.name) return;
    try {
      await api(`/api/markets/${m.id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name.trim() }),
      });
      adminState.markets = await api('/api/markets');
      renderMarketList();
      document.getElementById('panel-title').textContent = `${name.trim()} — Items`;
    } catch (err) {
      alert('Rename failed: ' + err.message);
    }
  }

  async function deleteMarket(m) {
    if (!confirm(`Delete market "${m.name}" and all of its price data?`)) return;
    try {
      await api(`/api/markets/${m.id}`, { method: 'DELETE' });
      adminState.markets = await api('/api/markets');
      adminState.currentMarketId = null;
      renderMarketList();
      document.getElementById('panel-title').textContent = 'Select a market';
      document.querySelector('#items-table tbody').innerHTML = '';
      if (adminState.markets.length) await selectAdminMarket(adminState.markets[0].id);
    } catch (err) {
      alert('Delete failed: ' + err.message);
    }
  }

  async function removeItem(itemId) {
    if (!adminState.currentMarketId) return;
    try {
      await api(`/api/markets/${adminState.currentMarketId}/items/${itemId}`, { method: 'DELETE' });
      adminState.markets = await api('/api/markets');
      renderMarketList();
      await reloadItemsTable();
    } catch (err) {
      alert('Remove failed: ' + err.message);
    }
  }

  async function lookupItem() {
    const input = document.getElementById('add-item-id');
    const id = Number(input.value);
    const errEl = document.getElementById('lookup-error');
    const preview = document.getElementById('lookup-preview');
    errEl.textContent = '';
    if (!Number.isInteger(id) || id <= 0) {
      errEl.textContent = 'Enter a valid item ID.';
      preview.hidden = true;
      return;
    }
    try {
      const info = await api(`/api/lookup/${id}`);
      document.getElementById('lookup-icon').src = `/icons/${encodeURIComponent(info.icon || '')}`;
      document.getElementById('lookup-name').textContent = `${info.name} (ID ${info.item_id})`;
      adminState.pendingAdd = id;
      preview.hidden = false;
    } catch (err) {
      preview.hidden = true;
      errEl.textContent = 'Lookup failed: ' + err.message;
    }
  }

  async function addPendingItem() {
    const errEl = document.getElementById('lookup-error');
    if (!adminState.currentMarketId || !adminState.pendingAdd) return;
    try {
      await api(`/api/markets/${adminState.currentMarketId}/items`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ item_id: adminState.pendingAdd }),
      });
      document.getElementById('lookup-preview').hidden = true;
      document.getElementById('add-item-id').value = '';
      adminState.pendingAdd = null;
      errEl.textContent = '';
      adminState.markets = await api('/api/markets');
      renderMarketList();
      await reloadItemsTable();
    } catch (err) {
      errEl.textContent = 'Add failed: ' + err.message;
    }
  }

  async function initAdmin() {
    adminState.markets = await api('/api/markets');
    renderMarketList();

    document.getElementById('new-market-form').addEventListener('submit', createMarket);
    document.getElementById('lookup-btn').addEventListener('click', lookupItem);
    document.getElementById('add-item-id').addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); lookupItem(); }
    });
    document.getElementById('confirm-add-btn').addEventListener('click', addPendingItem);
    document.getElementById('import-form').addEventListener('submit', importItemsFile);

    if (adminState.markets.length) {
      await selectAdminMarket(adminState.markets[0].id);
    } else {
      document.getElementById('panel-title').textContent = 'No markets yet — create one!';
      document.querySelector('#items-table tbody').innerHTML =
        '<tr><td colspan="4" class="muted">Create a market to get started.</td></tr>';
    }
  }

  /* ── Boot ─────────────────────────────────────────────────────────── */

  document.addEventListener('DOMContentLoaded', () => {
    if (document.body.dataset.page === 'watch') {
      initWatch().catch((err) => showEmpty('Error: ' + escapeHtml(err.message)));
    } else if (document.body.dataset.page === 'admin') {
      initAdmin().catch((err) => alert('Error: ' + err.message));
    }
  });
})();
