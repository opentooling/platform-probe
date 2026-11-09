"use strict";

const $ = (id) => document.getElementById(id);
const GROUPS = ["Platform", "API gateway", "Service mesh"];
const LABEL = { pass: "Pass", fail: "Fail", skip: "Skip", pending: "–", running: "…" };
let checks = [];
let experiments = [];
let current = null;
let polling = null;
const open = new Set();

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c != null) e.append(c);
  return e;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok && r.status !== 409) throw new Error(`${path}: ${r.status}`);
  return r.json();
}

function renderGroups(report) {
  const results = new Map((report?.results || []).map((r) => [r.id, r]));
  const running = report?.running;
  let firstPending = true;
  const root = $("groups");
  root.replaceChildren();
  for (const g of GROUPS) {
    const rows = checks.filter((c) => c.group === g);
    const done = rows.map((c) => results.get(c.id)).filter(Boolean);
    const passed = done.filter((r) => r.status === "pass").length;
    const failed = done.filter((r) => r.status === "fail").length;
    const card = el("section", { class: "card group", "aria-label": g },
      el("h2", {}, g, el("span", { class: "mono muted" }, done.length ? `${passed}/${rows.length}${failed ? ` · ${failed} failing` : ""}` : `${rows.length} checks`)));
    for (const c of rows) {
      const r = results.get(c.id);
      let status = r ? r.status : "pending";
      if (!r && running && firstPending) { status = "running"; firstPending = false; }
      const row = el("div", { class: "row" + (open.has(c.id) ? " open" : ""), role: "button", tabindex: "0", "aria-expanded": String(open.has(c.id)) },
        el("span", { class: `badge ${status}` }, LABEL[status]),
        el("div", { class: "name" }, c.name,
          r?.detail ? el("span", { class: "detail" }, r.detail) : null,
          r?.hint ? el("span", { class: "hint" }, r.hint) : null));
      const toggle = () => { open.has(c.id) ? open.delete(c.id) : open.add(c.id); renderGroups(report); };
      row.addEventListener("click", toggle);
      row.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } });
      card.append(row);
    }
    root.append(card);
  }
}

function renderSummary(report) {
  const has = report && report.results && report.results.length;
  $("m-pass").textContent = has ? report.passed : "–";
  $("m-fail").textContent = has ? report.failed : "–";
  $("m-skip").textContent = has ? report.skipped : "–";
  if (report?.running) {
    $("m-time-label").textContent = "Running";
    $("m-time").textContent = `${report.results.length} / ${report.total}`;
  } else {
    $("m-time-label").textContent = "Last run";
    $("m-time").textContent = has ? `${Math.round(report.durationMs / 1000)}s` : "–";
  }
  $("run").disabled = !!report?.running;
  $("run").textContent = report?.running ? "Running…" : "Run all";
}

async function refresh() {
  const report = await api("api/report");
  renderSummary(report);
  renderGroups(report);
  if (!report.running && polling) { clearInterval(polling); polling = null; }
  return report;
}

async function runAll() {
  $("run").disabled = true;
  await api("api/run", { method: "POST" });
  if (!polling) polling = setInterval(() => refresh().catch(console.error), 1000);
  refresh();
}

function selectExperiment(name) {
  current = experiments.find((e) => e.name === name);
  for (const b of $("exp-list").children) b.setAttribute("aria-selected", String(b.dataset.name === name));
  $("exp-desc").textContent = current.description;
  $("exp-config").textContent = current.config;
  $("exp-samples").replaceChildren();
  $("exp-summary").textContent = "";
}

async function runExperiment() {
  if (!current) return;
  const btn = $("exp-go");
  btn.disabled = true;
  btn.textContent = "Sending…";
  $("exp-samples").replaceChildren();
  $("exp-summary").textContent = "";
  try {
    const res = await api(`api/experiments/${current.name}?n=${$("exp-n").value}`, { method: "POST" });
    $("exp-samples").replaceChildren(...res.samples.map((s) =>
      el("span", { class: `sample ${s.tone}`, title: `${s.status || "error"} in ${s.ms}ms` }, s.label)));
    $("exp-summary").textContent = res.summary;
  } catch (e) {
    $("exp-summary").textContent = `Couldn't run the experiment: ${e.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Send";
  }
}

async function init() {
  const cfg = await api("api/config");
  checks = cfg.checks;
  $("target").textContent = `gateway ${cfg.gateway.namespace}/${cfg.gateway.name} · ${cfg.gateway.host} · namespace ${cfg.namespace}`;
  $("t-v1").textContent = `${100 - cfg.split.mesh}%`;
  $("t-v2").textContent = `${cfg.split.mesh}% · x-version`;

  experiments = await api("api/experiments");
  $("exp-list").replaceChildren(...experiments.map((e) =>
    el("button", { "data-name": e.name, role: "tab", onclick: () => selectExperiment(e.name) }, e.title)));
  selectExperiment(experiments[0].name);

  $("run").addEventListener("click", () => runAll().catch(console.error));
  $("exp-go").addEventListener("click", runExperiment);
  $("exp-n").addEventListener("input", (e) => { $("exp-n-out").textContent = e.target.value; });

  const report = await refresh();
  if (report.running && !polling) polling = setInterval(() => refresh().catch(console.error), 1000);
}

init().catch((e) => { $("target").textContent = `Couldn't load the console: ${e.message}`; });
