// Front local : lit uniquement l'API /api/* (base canonique DuckDB).
const $ = (s) => document.querySelector(s);
const api = async (url, opts) => {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const SERIES = () => [1, 2, 3, 4, 5].map((i) => css(`--series-${i}`));
const fmt = (v, d = 1) => (v == null || Number.isNaN(v) ? "–" :
  v.toLocaleString("fr-FR", { minimumFractionDigits: d, maximumFractionDigits: d }));
// Nombre de décimales adapté à l'ordre de grandeur (graduations d'axes).
const tick = (v) => fmt(v, Math.abs(v) >= 100 || v === 0 ? 0 : Math.abs(v) >= 1 ? 1 : 2);
const md = (meur) => (meur == null ? null : meur / 1000);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) e.append(k instanceof Node ? k : document.createTextNode(k ?? ""));
  return e;
};
const badge = (rig) => (rig ? el("span", { class: `badge ${rig.niveau}`, title: rig.raison },
  rig.niveau === "fixe" ? "⚠ non pilotable" : "⚠ peu pilotable") : "");

// --- graphiques ---------------------------------------------------------------
const charts = {};
function chart(id) {
  const node = document.getElementById(id);
  if (!charts[id]) charts[id] = echarts.init(node, null, { renderer: "svg" });
  return charts[id];
}
function empty(id, msg) {
  charts[id]?.dispose(); delete charts[id];
  const n = document.getElementById(id);
  n.replaceChildren(el("div", { class: "empty" }, msg));
}
window.addEventListener("resize", () => Object.values(charts).forEach((c) => c.resize()));

function lineChart(id, years, series, { unit = "", zero = false } = {}) {
  const has = series.some((s) => s.data.some((v) => v != null));
  if (!years.length || !has) return empty(id, "Pas de donnée pour cette série (voir l'onglet Données).");
  const colors = SERIES();
  const c = chart(id);
  c.setOption({
    color: series.map((s, i) => s.color || colors[i % colors.length]),
    textStyle: { color: css("--text-secondary") },
    grid: { left: 56, right: 16, top: series.length > 1 ? 32 : 12, bottom: 28 },
    legend: series.length > 1 ? { top: 0, textStyle: { color: css("--text-secondary") } } : undefined,
    tooltip: { trigger: "axis", valueFormatter: (v) => `${fmt(v)} ${unit}` },
    xAxis: { type: "category", data: years, axisLine: { lineStyle: { color: css("--border") } } },
    yAxis: { type: "value", scale: !zero, splitLine: { lineStyle: { color: css("--grid") } },
             axisLabel: { formatter: tick } },
    series: series.map((s) => ({ name: s.name, type: "line", data: s.data, symbol: "circle", symbolSize: 6,
      lineStyle: { width: 2, type: s.dashed ? "dashed" : "solid" }, connectNulls: false,
      markLine: s.zeroLine ? { silent: true, symbol: "none", data: [{ yAxis: 0 }],
        lineStyle: { color: css("--text-muted"), width: 1 } } : undefined })),
  }, true);
}

// --- onglets ------------------------------------------------------------------
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.id === b.dataset.tab));
  Object.values(charts).forEach((c) => c.resize());
}));

let STATUS, SERIESDATA;

// --- évolution ----------------------------------------------------------------
function renderEvolution() {
  const e = SERIESDATA.etat;
  const pib = $("#evo-unit").value === "pib";
  const conv = (arr) => arr.map((v, i) => (v == null ? null : pib ? (e.pib[i] ? (v / e.pib[i]) * 100 : null) : md(v)));
  const unit = pib ? "% PIB" : "Md€";
  if (pib && !e.pib.some((v) => v != null)) {
    ["c-recdep", "c-solde", "c-dette", "c-charge"].forEach((id) => empty(id, "PIB nominal absent de la base (source Eurostat non ingérée)."));
  } else {
    lineChart("c-recdep", e.years, [{ name: "Recettes nettes", data: conv(e.recettes_nettes) },
                                     { name: "Dépenses", data: conv(e.depenses) }], { unit });
    lineChart("c-solde", e.years, [{ name: "Solde", data: conv(e.solde), zeroLine: true }], { unit });
    lineChart("c-dette", e.years, [{ name: "Dette de l'État", data: conv(e.dette_etat) }], { unit, zero: true });
    lineChart("c-charge", e.years, [{ name: "Charge de la dette", data: conv(e.charge_dette) }], { unit, zero: true });
  }
  const a = SERIESDATA.apu;
  const apuConv = (d) => a.years.map((y) => {
    const v = d?.[y]; const g = a.pib_nominal?.[y];
    return v == null ? null : pib ? (g ? (v / g) * 100 : null) : md(v);
  });
  lineChart("c-apu-dette", a.years, [{ name: "Dette APU", data: apuConv(a.dette_maastricht) }], { unit, zero: true });
  lineChart("c-apu-solde", a.years, [{ name: "Solde APU", data: apuConv(a.solde_maastricht), zeroLine: true }], { unit });
}
$("#evo-unit").addEventListener("change", renderEvolution);

// --- sankey -------------------------------------------------------------------
async function renderSankey() {
  const y = $("#sk-year").value;
  if (!y) return empty("c-sankey", "Aucun exercice en base.");
  const d = await api(`/api/sankey/${y}`);
  const colors = SERIES();
  const kindColor = { recette: colors[2], bg: colors[0], mission: colors[0], programme: colors[0],
                      prelevement: colors[3], deficit: css("--critical") };
  chart("c-sankey").setOption({
    textStyle: { color: css("--text-secondary") },
    tooltip: { formatter: (p) => p.dataType === "edge"
      ? `${label(d, p.data.source)} → ${label(d, p.data.target)}<br>${fmt(md(p.data.value))} Md€`
      : `${label(d, p.name)}<br>${fmt(md(p.value))} Md€` },
    series: [{
      type: "sankey", left: 8, right: 220, nodeGap: 4, nodeWidth: 10, draggable: false, layoutIterations: 32,
      emphasis: { focus: "adjacency" },
      data: d.nodes.map((n) => ({ name: n.name, itemStyle: { color: kindColor[n.kind] || colors[0] },
        label: { formatter: () => (n.rigidite ? "⚠ " : "") + (n.label || n.name), color: css("--text-primary"),
                 fontSize: n.kind === "programme" ? 10 : 12 } })),
      links: d.links,
      lineStyle: { color: "source", opacity: 0.25, curveness: 0.5 },
    }],
  }, true);
}
const label = (d, name) => (d.nodes.find((n) => n.name === name) || {}).label || name;
$("#sk-year").addEventListener("change", renderSankey);

// --- détail (drill-down) --------------------------------------------------------
const dd = { mission: null, programme: null, programmeLabel: null };
async function renderDrill() {
  const y = $("#dd-year").value;
  if (!y) { empty("c-drill", "Aucun exercice en base."); $("#dd-table").replaceChildren(); return; }
  const q = new URLSearchParams();
  if (dd.mission) q.set("mission", dd.mission);
  if (dd.programme) q.set("programme", dd.programme);
  const d = await api(`/api/drill/${y}?${q}`);
  const crumbs = [el("a", { onclick: () => { Object.assign(dd, { mission: null, programme: null }); renderDrill(); } }, "Toutes missions")];
  if (dd.mission) crumbs.push(" › ", el("a", { onclick: () => { dd.programme = null; renderDrill(); } }, dd.mission));
  if (dd.programme) crumbs.push(" › ", dd.programmeLabel);
  $("#dd-crumbs").replaceChildren(...crumbs);
  const lvl = { mission: "missions", programme: "programmes", titre: "titres" }[d.level];
  $("#dd-title").firstChild.textContent = `Dépenses par ${lvl.slice(0, -1)} `;
  const items = d.items;
  if (!items.length) { empty("c-drill", "Pas de dépense à ce niveau."); }
  else {
    const top = items.slice(0, 25).reverse();
    chart("c-drill").setOption({
      textStyle: { color: css("--text-secondary") },
      grid: { left: 260, right: 60, top: 8, bottom: 24 },
      tooltip: { trigger: "item", formatter: (p) => {
        const it = top[p.dataIndex];
        return `${it.label}<br>${fmt(md(it.montant_meur))} Md€ · ${fmt(it.pct_total)} % du total` +
               (it.pct_pib != null ? ` · ${fmt(it.pct_pib, 2)} % du PIB` : "");
      } },
      xAxis: { type: "value", splitLine: { lineStyle: { color: css("--grid") } }, axisLabel: { formatter: (v) => `${tick(v)} Md€` } },
      yAxis: { type: "category", data: top.map((i) => (i.rigidite ? "⚠ " : "") + trunc(i.label, 40)) },
      series: [{ type: "bar", data: top.map((i) => md(i.montant_meur)), barMaxWidth: 16,
                 itemStyle: { color: SERIES()[0], borderRadius: [0, 4, 4, 0] } }],
    }, true);
    document.getElementById("c-drill").style.height = `${Math.max(200, top.length * 24 + 40)}px`;
    chart("c-drill").resize();
    chart("c-drill").off("click");
    chart("c-drill").on("click", (p) => drillInto(d.level, top[p.dataIndex]));
  }
  const head = el("tr", {}, el("th", {}, d.level), el("th", {}, ""), el("th", { class: "num" }, "Md€"),
    el("th", { class: "num" }, "% du total"), el("th", { class: "num" }, "% du PIB"));
  const rows = items.map((it) => el("tr", { class: d.level === "titre" ? "" : "click", onclick: () => drillInto(d.level, it) },
    el("td", {}, it.code && d.level !== "mission" ? `${it.code} – ${it.label}` : it.label), el("td", {}, badge(it.rigidite)),
    el("td", { class: "num" }, fmt(md(it.montant_meur), 2)), el("td", { class: "num" }, fmt(it.pct_total)),
    el("td", { class: "num" }, it.pct_pib == null ? "PIB absent" : fmt(it.pct_pib, 2))));
  rows.push(el("tr", {}, el("th", {}, "Total"), el("th", {}), el("th", { class: "num" }, fmt(md(items.reduce((s, i) => s + i.montant_meur, 0)), 2)),
    el("th", {}), el("th", {})));
  $("#dd-table").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
}
function drillInto(level, it) {
  if (level === "mission") { dd.mission = it.label; renderDrill(); }
  else if (level === "programme") { dd.programme = it.code; dd.programmeLabel = it.label; renderDrill(); }
}
const trunc = (s, n) => (s && s.length > n ? s.slice(0, n - 1) + "…" : s);
$("#dd-year").addEventListener("change", () => { Object.assign(dd, { mission: null, programme: null }); renderDrill(); });

// --- simulateur ---------------------------------------------------------------
let BASE = null;
const measures = [];

function renderBaseForm() {
  const f = (k, lbl) => {
    const inp = el("input", { type: "number", id: `b-${k}`, step: "any", value: BASE[k] ?? "" });
    if (BASE[k] == null) { inp.classList.add("missing"); inp.title = "Absent de la base : à saisir"; }
    return el("label", {}, lbl, inp);
  };
  const sp = BASE.spending.reduce((s, l) => s + l.amount, 0);
  const rv = BASE.revenue.reduce((s, l) => s + l.amount, 0);
  $("#sim-base").replaceChildren(
    el("label", {}, "Exercice", el("strong", {}, BASE.exercice ?? "–")),
    f("gdp", "PIB nominal (M€)"), f("debt", "Dette négociable de l'État fin d'année (M€)"), f("interest", "Charge d'intérêts (M€)"),
    el("label", {}, "Dépenses hors intérêts (M€)", el("span", {}, fmt(sp, 0))),
    el("label", {}, "Recettes nettes (M€)", el("span", {}, fmt(rv, 0))),
    BASE.missing.length ? el("p", { class: "error" }, `Manquant en base : ${BASE.missing.join(", ")}.` +
      (BASE.spending.length ? "" : " Le simulateur a besoin des lignes de dépenses et de recettes ingérées.")) : "",
  );
}

function targetOptions(side) {
  const lines = side === "spending" ? BASE.spending : BASE.revenue;
  const groups = new Map();
  for (const l of lines) {
    if (!groups.has(l.group)) groups.set(l.group, { label: l.group_label, amount: 0, rig: null, lines: [] });
    const g = groups.get(l.group); g.amount += l.amount; g.lines.push(l); g.rig = g.rig || l.rigidite;
  }
  const opts = [];
  for (const [key, g] of groups) {
    const og = el("optgroup", { label: g.label });
    og.append(el("option", { value: key }, `▸ ${side === "spending" ? "Mission" : "Catégorie"} entière : ${g.label} (${fmt(md(g.amount))} Md€)`));
    for (const l of g.lines) og.append(el("option", { value: l.key }, `${l.rigidite ? "⚠ " : ""}${l.label} (${fmt(md(l.amount))} Md€)`));
    opts.push(og);
  }
  return opts;
}

function renderMeasures() {
  const head = el("tr", {}, ...["Côté", "Cible", "Mode", "Valeur", "Début", "Montée (ans)", ""].map((h) => el("th", {}, h)));
  const rows = measures.map((m, i) => {
    const side = el("select", { onchange: (e) => { m.side = e.target.value; m.target = ""; renderMeasures(); } },
      el("option", { value: "spending" }, "Dépense"), el("option", { value: "revenue" }, "Recette"));
    side.value = m.side;
    const tgt = el("select", { onchange: (e) => { m.target = e.target.value; renderMeasures(); } },
      el("option", { value: "" }, "— choisir —"), targetOptions(m.side));
    tgt.value = m.target;
    const line = [...BASE.spending, ...BASE.revenue].find((l) => l.key === m.target);
    const mode = el("select", { onchange: (e) => { m.mode = e.target.value; } },
      el("option", { value: "pct" }, "%"), el("option", { value: "meur" }, "M€"));
    mode.value = m.mode;
    const num = (k, step) => el("input", { type: "number", step, value: m[k], style: "width:90px",
                                           onchange: (e) => { m[k] = Number(e.target.value); } });
    return el("tr", {}, el("td", {}, side), el("td", { class: "target" }, tgt, " ", badge(line?.rigidite)), el("td", {}, mode),
      el("td", {}, num("value", "any")), el("td", {}, num("start_year", 1)), el("td", {}, num("ramp_years", 1)),
      el("td", {}, el("button", { onclick: () => { measures.splice(i, 1); renderMeasures(); } }, "✕")));
  });
  $("#sim-measures").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
}
$("#m-add").addEventListener("click", () => {
  measures.push({ side: "spending", target: "", mode: "pct", value: -5, start_year: (BASE.exercice || 2025) + 1, ramp_years: 3 });
  renderMeasures();
});

const num = (id) => Number($(id).value);
function currentAssumptions() {
  return { horizon: num("#h-horizon"), gdp_growth: num("#h-g") / 100, market_rate: num("#h-r") / 100,
           refinancing_share: num("#h-rho") / 100, spending_trend: num("#h-s") / 100,
           revenue_elasticity: num("#h-eps"), other_debt_flows: num("#h-other"),
           multiplier_enabled: $("#h-mult").checked, spending_multiplier: num("#h-md"), revenue_multiplier: num("#h-mr") };
}
function setAssumptions(a) {
  const set = (id, v) => { if (v != null) $(id).value = v; };
  set("#h-horizon", a.horizon); set("#h-g", a.gdp_growth * 100); set("#h-r", a.market_rate * 100);
  set("#h-rho", a.refinancing_share * 100); set("#h-s", a.spending_trend * 100); set("#h-eps", a.revenue_elasticity);
  set("#h-other", a.other_debt_flows); $("#h-mult").checked = !!a.multiplier_enabled;
  set("#h-md", a.spending_multiplier); set("#h-mr", a.revenue_multiplier);
}
function currentRequest() {
  const v = (k) => { const x = $(`#b-${k}`).value; return x === "" ? null : Number(x); };
  const line = (l) => ({ key: l.key, amount: l.amount, group: l.group });
  return {
    base: { year: BASE.exercice, gdp: v("gdp"), debt: v("debt"), interest: v("interest"),
            spending: BASE.spending.map(line), revenue: BASE.revenue.map(line) },
    assumptions: currentAssumptions(),
    measures: measures.filter((m) => m.target).map((m) => ({ ...m })),
  };
}

async function runSim() {
  $("#sim-error").hidden = true;
  const req = currentRequest();
  const missing = ["gdp", "debt", "interest"].filter((k) => req.base[k] == null);
  if (!req.base.year || missing.length || !req.base.spending.length) {
    $("#sim-error").textContent = `Impossible de simuler : renseigner ${missing.join(", ") || "les données de base (ingestion)"}.`;
    $("#sim-error").hidden = false; return;
  }
  try {
    const main = await api("/api/sim/run", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req) });
    const others = [];
    for (const name of [...$("#sc-compare").selectedOptions].map((o) => o.value)) {
      const sc = await api(`/api/scenarios/${encodeURIComponent(name)}`);
      // Un scénario sauvegardé est rejoué sur la base courante, avec ses hypothèses et ses mesures.
      const r = await api("/api/sim/run", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...sc, base: req.base }) });
      others.push({ name, res: r });
    }
    renderSim(main, others);
  } catch (e) { $("#sim-error").textContent = e.message; $("#sim-error").hidden = false; }
}
$("#sim-run").addEventListener("click", runSim);

function renderSim(main, others) {
  const years = main.reference.map((r) => r.year);
  const name = $("#sc-name").value || "Scénario courant";
  const mk = (k, f = (x) => x) => [
    { name: "Référence (sans mesure)", data: main.reference.map((r) => f(r[k])), color: css("--text-muted"), dashed: true },
    { name, data: main.scenario.map((r) => f(r[k])), color: SERIES()[0] },
    ...others.map((o, i) => ({ name: o.name, data: o.res.scenario.map((r) => f(r[k])), color: SERIES()[i + 1] })),
  ];
  lineChart("s-deficit", years, mk("deficit"), { unit: "M€" });
  lineChart("s-dette", years, mk("debt"), { unit: "M€", zero: true });
  lineChart("s-ratio", years, mk("debt_to_gdp", (x) => x * 100), { unit: "%" });
  lineChart("s-interet", years, mk("interest"), { unit: "M€", zero: true });
  const head = el("tr", {}, ...["Année", "PIB", "Déficit réf.", "Déficit scén.", "Δ déficit", "Dette scén.", "Δ dette",
    "Dette/PIB scén.", "Δ pts", "Intérêts scén.", "Δ intérêts", "Taux apparent"].map((h) => el("th", { class: "num" }, h)));
  const rows = main.scenario.map((s, i) => {
    const r = main.reference[i], d = main.diff[i];
    return el("tr", {}, ...[s.year, fmt(s.gdp, 0), fmt(r.deficit, 0), fmt(s.deficit, 0), fmt(d.d_deficit, 0), fmt(s.debt, 0),
      fmt(d.d_debt, 0), fmt(s.debt_to_gdp * 100), fmt(d.d_debt_to_gdp_pts, 2), fmt(s.interest, 0), fmt(d.d_interest, 0),
      `${fmt(s.apparent_rate * 100, 2)} %`].map((v) => el("td", { class: "num" }, v)));
  });
  $("#sim-table").replaceChildren(el("thead", {}, head), el("tbody", {}, rows));
}

async function refreshScenarioList() {
  const names = await api("/api/scenarios");
  $("#sc-list").replaceChildren(el("option", { value: "" }, "Charger…"), names.map((n) => el("option", { value: n }, n)));
  $("#sc-compare").replaceChildren(...names.map((n) => el("option", { value: n }, n)));
}
$("#sc-save").addEventListener("click", async () => {
  const n = $("#sc-name").value.trim();
  if (!n) return alert("Donner un nom au scénario.");
  const req = currentRequest();
  await api(`/api/scenarios/${encodeURIComponent(n)}`, { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ assumptions: req.assumptions, measures: req.measures, base_year: req.base.year,
                           saved_at: new Date().toISOString() }) });
  refreshScenarioList();
});
$("#sc-list").addEventListener("change", async (e) => {
  if (!e.target.value) return;
  const sc = await api(`/api/scenarios/${encodeURIComponent(e.target.value)}`);
  $("#sc-name").value = e.target.value;
  setAssumptions(sc.assumptions || {});
  measures.splice(0, measures.length, ...(sc.measures || []));
  renderMeasures();
});
$("#sc-del").addEventListener("click", async () => {
  const n = $("#sc-list").value;
  if (n && confirm(`Supprimer « ${n} » ?`)) { await api(`/api/scenarios/${encodeURIComponent(n)}`, { method: "DELETE" }); refreshScenarioList(); }
});

// --- données ------------------------------------------------------------------
function renderStatus() {
  const c = STATUS.counts;
  $("#st-summary").replaceChildren(
    el("p", {}, `Base : ${STATUS.db}${STATUS.db_exists ? "" : " (absente)"}`),
    el("p", {}, `Exercices avec dépenses exécutées : ${STATUS.years.join(", ") || "aucun"}`),
    el("p", {}, Object.entries(c).map(([k, v]) => `${k} : ${v}`).join(" · ")),
    el("p", {}, "Nomenclature : " + (Object.entries(STATUS.nomenclature).map(([k, v]) => `${k} ${v}`).join(", ") || "–") +
      " (détail dans data/NON_RAPPROCHES.md)"));
  $("#st-sources").replaceChildren(el("thead", {}, el("tr", {}, ...["Fichier", "Parseur", "Récupéré le", "URL"].map((h) => el("th", {}, h)))),
    el("tbody", {}, STATUS.sources.map((s) => el("tr", {}, el("td", {}, s.path), el("td", {}, s.parser),
      el("td", {}, s.fetched_at || "–"), el("td", {}, s.url || "–")))));
}

// --- démarrage ------------------------------------------------------------------
async function init() {
  [STATUS, SERIESDATA, BASE] = await Promise.all([api("/api/status"), api("/api/series"), api("/api/sim/base")]);
  if (!STATUS.years.length) {
    $("#banner").hidden = false;
    $("#banner").textContent = "Aucune dépense exécutée en base. Lancer `budget fetch`, `budget inspect` puis `budget ingest` " +
      "(les parseurs DGFiP s'écrivent à partir de data/INSPECTION.md). Les champs du simulateur restent saisissables à la main.";
  }
  for (const id of ["#sk-year", "#dd-year"]) {
    $(id).replaceChildren(...[...STATUS.years].reverse().map((y) => el("option", { value: y }, y)));
  }
  renderStatus();
  renderEvolution();
  renderSankey();
  renderDrill();
  renderBaseForm();
  renderMeasures();
  refreshScenarioList();
}
init().catch((e) => { $("#banner").hidden = false; $("#banner").textContent = `Erreur : ${e.message}`; });
