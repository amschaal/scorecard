/* hpcusage chart helpers on top of Plotly. Colors come from the CSS tokens in style.css so the
   same chart code renders correctly in light and dark mode. Every chart card gets a "Table"
   toggle so no value is reachable only through color or hover. */
(function () {
  "use strict";

  const tok = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const palette = () => [1, 2, 3, 4, 5, 6, 7, 8].map((i) => tok(`--series-${i}`));
  const CONFIG = { displayModeBar: false, responsive: true, displaylogo: false };

  function baseLayout(nSeries, extra) {
    const surface = tok("--surface-1");
    return Object.assign({
      paper_bgcolor: surface, plot_bgcolor: surface,
      font: { family: tok("--font") || "system-ui, sans-serif", color: tok("--text-secondary"), size: 12 },
      margin: { l: 56, r: 16, t: 8, b: 44 },
      colorway: palette(),
      showlegend: nSeries > 1,
      legend: { orientation: "h", y: -0.18, x: 0, font: { color: tok("--text-secondary") } },
      hovermode: "x unified",
      hoverlabel: { bgcolor: surface, bordercolor: tok("--axis"), font: { color: tok("--text-primary"), size: 12 } },
      xaxis: { gridcolor: tok("--grid"), linecolor: tok("--axis"), zeroline: false, showgrid: false,
               tickfont: { color: tok("--text-muted") }, automargin: true },
      yaxis: { gridcolor: tok("--grid"), linecolor: tok("--axis"), zeroline: false, tickformat: ",.0f",
               tickfont: { color: tok("--text-muted") }, fixedrange: true, rangemode: "tozero", automargin: true },
      bargap: 0.35,
    }, extra || {});
  }

  async function getJSON(path, params) {
    const url = new URL(path, window.location.origin);
    Object.entries(params || {}).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, v); });
    const r = await fetch(url, { credentials: "same-origin" });
    if (r.status === 401) { window.location = "/auth/login?next=" + encodeURIComponent(location.pathname + location.search); return null; }
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    return r.json();
  }

  function empty(el, msg) {
    el.innerHTML = `<div class="empty">${msg || "No data for this window"}</div>`;
  }

  const esc = (v) => String(v == null ? "" : v).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  const fmt = {
    num: (v, d = 0) => (v == null ? "–" : Number(v).toLocaleString(undefined, { maximumFractionDigits: d })),
    pct: (v, d = 0) => (v == null ? "–" : (100 * v).toFixed(d) + "%"),
    compact: (v) => (v == null ? "–" : Number(v) >= 1e6 ? (v / 1e6).toFixed(1) + "M" : Number(v) >= 1e4 ? (v / 1e3).toFixed(1) + "K" : fmt.num(v)),
  };

  /* Attach a table view to the card containing `el`. rows: [{name, values[]}], cols: header strings. */
  function attachTable(el, cols, rows, opts) {
    const card = el.closest(".card"); if (!card) return;
    let holder = card.querySelector(".chart-table");
    if (!holder) { holder = document.createElement("div"); holder.className = "chart-table"; card.appendChild(holder); }
    const d = (opts && opts.digits) != null ? opts.digits : 1;
    const head = "<tr><th></th>" + cols.map((c) => `<th class="num">${esc(c)}</th>`).join("") + "</tr>";
    const body = rows.map((r) => `<tr><td>${esc(r.name)}</td>` + r.values.map((v) => `<td class="num">${fmt.num(v, d)}</td>`).join("") + "</tr>").join("");
    holder.innerHTML = `<table class="data">${head}${body}</table>`;
    let btn = card.querySelector(".table-toggle");
    if (!btn) {
      const h2 = card.querySelector("h2"); if (!h2) return;
      let tools = h2.querySelector(".tools");
      if (!tools) { tools = document.createElement("span"); tools.className = "tools"; h2.appendChild(tools); }
      btn = document.createElement("button"); btn.type = "button"; btn.className = "btn small table-toggle"; btn.textContent = "Table";
      btn.addEventListener("click", () => { card.classList.toggle("show-table"); btn.textContent = card.classList.contains("show-table") ? "Chart" : "Table"; });
      tools.appendChild(btn);
    }
  }

  /* Stacked columns: buckets on x, one trace per series. 2px surface gap between segments. */
  function stackedBars(el, buckets, series, opts) {
    opts = opts || {};
    if (!buckets.length || !series.length) return empty(el);
    const surface = tok("--surface-1");
    const traces = series.map((s) => ({
      type: "bar", name: s.name, x: buckets, y: s.values,
      marker: { line: { width: 2, color: surface } },
      hovertemplate: `%{y:,.${opts.digits == null ? 1 : opts.digits}f}${opts.unit ? " " + opts.unit : ""}<extra>%{fullData.name}</extra>`,
    }));
    const layout = baseLayout(series.length, { barmode: "stack", yaxis: Object.assign(baseLayout(1).yaxis, { title: { text: opts.yTitle || "", font: { size: 12 } } }) });
    if (opts.colors) traces.forEach((t, i) => { t.marker.color = opts.colors[i]; });
    Plotly.newPlot(el, traces, layout, CONFIG);
    attachTable(el, buckets, series, opts);
  }

  /* Lines: 2px, round joins, >=8px markers with a 2px surface ring. */
  function lines(el, x, series, opts) {
    opts = opts || {};
    if (!x.length || !series.length) return empty(el);
    const surface = tok("--surface-1");
    const traces = series.map((s) => ({
      type: "scatter", mode: x.length > 60 ? "lines" : "lines+markers", name: s.name, x, y: s.values,
      connectgaps: false,
      line: { width: 2, shape: "linear" },
      marker: { size: 8, line: { width: 2, color: surface } },
      hovertemplate: `%{y:,.${opts.digits == null ? 1 : opts.digits}f}${opts.unit ? " " + opts.unit : ""}<extra>%{fullData.name}</extra>`,
    }));
    const yaxis = Object.assign(baseLayout(1).yaxis, { title: { text: opts.yTitle || "", font: { size: 12 } } });
    if (opts.percent) { yaxis.range = [0, Math.max(100, ...series.flatMap((s) => s.values.filter((v) => v != null))) * 1.02]; yaxis.ticksuffix = "%"; }
    if (opts.yRange) yaxis.range = opts.yRange;
    Plotly.newPlot(el, traces, baseLayout(series.length, { yaxis }), CONFIG);
    attachTable(el, x, series, opts);
  }

  /* Horizontal bars for rankings. Single series -> no legend; value label at the tip. */
  function hbar(el, names, values, opts) {
    opts = opts || {};
    if (!names.length) return empty(el);
    const rev = names.slice().reverse(), vals = values.slice().reverse();
    const trace = {
      type: "bar", orientation: "h", x: vals, y: rev,
      marker: { color: opts.color || tok("--series-1"), line: { width: 2, color: tok("--surface-1") } },
      text: vals.map((v) => fmt.num(v, opts.digits == null ? 0 : opts.digits)), textposition: "outside", cliponaxis: false,
      textfont: { color: tok("--text-secondary") },
      hovertemplate: `%{x:,.1f}${opts.unit ? " " + opts.unit : ""}<extra>%{y}</extra>`,
    };
    const layout = baseLayout(1, {
      margin: { l: 8, r: 48, t: 4, b: 32 }, bargap: 0.3, hovermode: "closest",
      xaxis: Object.assign(baseLayout(1).xaxis, { showgrid: true, gridcolor: tok("--grid"), fixedrange: true }),
      yaxis: { automargin: true, tickfont: { color: tok("--text-primary") }, fixedrange: true },
      height: Math.max(180, 26 * names.length + 50),
    });
    Plotly.newPlot(el, [trace], layout, CONFIG);
    attachTable(el, [opts.label || "value"], names.map((n, i) => ({ name: n, values: [values[i]] })), opts);
  }

  /* Tiny sparkline for overview cards. */
  function sparkline(el, x, y) {
    if (!x.length) return empty(el, "");
    Plotly.newPlot(el, [{ type: "scatter", mode: "lines", x, y, line: { width: 2, color: tok("--series-1") },
      fill: "tozeroy", fillcolor: "rgba(42,120,214,0.10)", hovertemplate: "%{x}<br>%{y:,.0f} CPU-h<extra></extra>" }],
      { paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)", margin: { l: 0, r: 0, t: 0, b: 0 }, height: 48,
        xaxis: { visible: false, fixedrange: true }, yaxis: { visible: false, fixedrange: true, rangemode: "tozero" },
        showlegend: false, hovermode: "x", hoverlabel: { bgcolor: tok("--surface-1"), font: { color: tok("--text-primary") } } },
      CONFIG);
  }

  /* Scatter for efficiency views: single hue (all-pairs palette limits), size = jobs. */
  function scatter(el, points, opts) {
    opts = opts || {};
    if (!points.length) return empty(el);
    const trace = {
      type: "scatter", mode: "markers", x: points.map((p) => p.x), y: points.map((p) => p.y), text: points.map((p) => p.name),
      marker: { color: tok("--series-1"), size: points.map((p) => Math.max(8, Math.min(28, 6 + Math.sqrt(p.size || 1)))),
                opacity: 0.8, line: { width: 2, color: tok("--surface-1") } },
      hovertemplate: `<b>%{text}</b><br>${opts.xTitle || "x"}: %{x:,.0f}<br>${opts.yTitle || "y"}: %{y:.0%}<extra></extra>`,
    };
    const layout = baseLayout(1, {
      hovermode: "closest",
      xaxis: Object.assign(baseLayout(1).xaxis, { title: { text: opts.xTitle || "" }, showgrid: true, type: opts.logX ? "log" : "linear" }),
      yaxis: Object.assign(baseLayout(1).yaxis, { title: { text: opts.yTitle || "" }, tickformat: ".0%", range: [0, 1.05] }),
    });
    Plotly.newPlot(el, [trace], layout, CONFIG);
  }

  /* Re-render on theme change so token colors update. */
  const rerenders = [];
  function onRender(fn) { rerenders.push(fn); fn(); }
  function rerenderAll() { rerenders.forEach((fn) => { try { fn(); } catch (e) { console.error(e); } }); }
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", rerenderAll);

  /* Theme toggle in the top bar. */
  function initTheme() {
    const btn = document.getElementById("theme-toggle");
    const saved = (() => { try { return localStorage.getItem("hpcusage-theme"); } catch (e) { return null; } })();
    if (saved) document.documentElement.dataset.theme = saved;
    if (!btn) return;
    btn.addEventListener("click", () => {
      const cur = document.documentElement.dataset.theme || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      const next = cur === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("hpcusage-theme", next); } catch (e) { /* ignore */ }
      rerenderAll();
    });
  }
  initTheme();

  /* Date-range presets rewrite ?from=&to= on the current URL. */
  function initWindowControl() {
    const form = document.getElementById("window-form");
    if (!form) return;
    form.querySelectorAll("[data-days]").forEach((a) => {
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        const days = parseInt(a.dataset.days, 10);
        const to = new Date(); const from = new Date(); from.setDate(to.getDate() - days + 1);
        const url = new URL(location.href);
        url.searchParams.set("from", from.toISOString().slice(0, 10));
        url.searchParams.set("to", to.toISOString().slice(0, 10));
        location.href = url.toString();
      });
    });
  }
  initWindowControl();

  const linkTo = (kind, cluster, name) => `<a href="/c/${encodeURIComponent(cluster)}/${kind}/${encodeURIComponent(name)}">${esc(name)}</a>`;

  window.HU = { getJSON, stackedBars, lines, hbar, sparkline, scatter, empty, attachTable, fmt, esc, onRender, tok, palette, linkTo };
})();
