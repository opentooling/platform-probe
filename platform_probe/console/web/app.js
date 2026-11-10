"use strict";

// The console: a sidebar of categories, an overview of what needs attention, one category at a
// time with its own Run button, and the experiments. Routes are URL hashes: #/ (overview),
// #/<category> (e.g. #/storage) and #/experiments.

const $ = (id) => document.getElementById(id);
const LABEL = { pass: "Pass", fail: "Fail", skip: "Skip", pending: "–", running: "…", none: "–" };
let cfg = null;
let checks = [];
let categories = [];
let report = null;
let experiments = [];
let current = null;
let polling = null;
const open = new Set();
const filters = {};

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children) if (c != null && c !== false) e.append(c);
  return e;
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok && r.status !== 409) {
    let why = "";
    try { why = (await r.json()).error || ""; } catch (_) { /* not JSON */ }
    throw new Error(`${path}: ${r.status}${why ? ` ${why}` : ""}`);
  }
  return r.json();
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function duration(ms) {
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

// --- state derived from the report ----------------------------------------------------------

// Each check's status: its latest result from any run, or, during a run that covers it and
// hasn't reached it yet, pending (the next one is running).
function statuses() {
  const latest = new Map((report?.latest || []).map((r) => [r.id, r]));
  const done = new Set((report?.results || []).map((r) => r.id));
  const inRun = (c) => report?.running && (!report.categories?.length || report.categories.includes(c.category));
  let nextMarked = false;
  const out = new Map();
  for (const c of checks) {
    if (inRun(c) && !done.has(c.id)) {
      out.set(c.id, { status: nextMarked ? "pending" : "running" });
      nextMarked = true;
      continue;
    }
    const r = latest.get(c.id);
    out.set(c.id, r || { status: "none" });
  }
  return out;
}

function stats(list, st) {
  const s = { pass: 0, fail: 0, skip: 0, none: 0, running: 0, total: list.length, at: "" };
  for (const c of list) {
    const r = st.get(c.id);
    if (r.status === "pending" || r.status === "running") s.running++;
    else s[r.status] = (s[r.status] || 0) + 1;
    if (r.at && r.at > s.at) s.at = r.at;
  }
  return s;
}

function dotClass(s) {
  if (s.running) return "running";
  if (s.fail) return "fail";
  if (s.none === s.total) return "";
  if (s.skip) return "skip";
  return "pass";
}

// --- rendering ------------------------------------------------------------------------------

function route() {
  const h = location.hash.replace(/^#\/?/, "");
  if (h === "experiments") return { view: "experiments" };
  if (categories.some((c) => c.id === h)) return { view: "category", id: h };
  return { view: "overview" };
}

function render() {
  if (!cfg) return;
  const st = statuses();
  const r = route();
  renderNav(st, r);
  renderHeader(st);
  $("view-overview").hidden = r.view !== "overview";
  $("view-category").hidden = r.view !== "category";
  $("view-experiments").hidden = r.view !== "experiments";
  if (r.view === "overview") renderOverview(st);
  if (r.view === "category") renderCategory(st, r.id);
}

function renderNav(st, r) {
  const all = stats(checks, st);
  $("nav-overview").setAttribute("aria-current", r.view === "overview" ? "page" : "false");
  $("nav-experiments").setAttribute("aria-current", r.view === "experiments" ? "page" : "false");
  const oc = $("nav-overview-count");
  oc.textContent = all.fail ? `${all.fail} failed` : "";
  oc.className = "count" + (all.fail ? " bad" : "");
  $("nav-cats").replaceChildren(...categories.map((cat) => {
    const s = stats(checks.filter((c) => c.category === cat.id), st);
    let count = `${s.total}`;
    if (s.running) count = "running";
    else if (s.fail) count = `${s.fail} failed`;
    else if (s.none < s.total) count = `${s.pass}/${s.total}`;
    return el("a", { href: `#/${cat.id}`, class: "nav-item", "aria-current": r.id === cat.id ? "page" : "false" },
      el("span", { class: `dot ${dotClass(s)}`, "aria-hidden": "true" }),
      el("span", { class: "label" }, cat.name),
      el("span", { class: "count" + (s.fail && !s.running ? " bad" : "") }, count));
  }));
}

function renderHeader(st) {
  const running = !!report?.running;
  $("run").disabled = running;
  $("run").textContent = running ? "Running…" : `Run all ${checks.length}`;
  $("cat-run").disabled = running;
  if (running) {
    const scope = report.categories?.length ? report.categories.map(catName).join(", ") : "all";
    $("last-run").textContent = `Running ${scope}: ${report.results.length} / ${report.total}`;
  } else if (report?.finishedAt) {
    $("last-run").textContent = `Last run ${ago(report.finishedAt)} · ${duration(report.durationMs)}`;
  } else {
    $("last-run").textContent = "Not run yet";
  }
}

function catName(id) {
  return categories.find((c) => c.id === id)?.name || id;
}

function renderOverview(st) {
  const all = stats(checks, st);
  const seen = all.total - all.none - all.running;
  $("m-pass").textContent = seen ? all.pass : "–";
  $("m-fail").textContent = seen ? all.fail : "–";
  $("m-skip").textContent = seen ? all.skip : "–";
  $("m-time-label").textContent = report?.running ? "Running" : "Last run";
  $("m-time").textContent = report?.running ? `${report.results.length} / ${report.total}` : (all.at ? ago(all.at) : "–");

  const failing = checks.filter((c) => st.get(c.id).status === "fail");
  const box = $("attention");
  box.classList.toggle("has-failures", failing.length > 0);
  $("att-title").textContent = failing.length ? `Needs attention · ${failing.length} failed` : "Needs attention";
  if (!failing.length) {
    $("att-list").replaceChildren(el("p", { class: "empty" },
      seen ? "Nothing failed in the latest results." : "No results yet. Run all, or pick a category on the left and run it."));
    return;
  }
  $("att-list").replaceChildren(...failing.map((c) => {
    const r = st.get(c.id);
    return el("a", { class: "att-row", href: `#/${c.category}`, onclick: () => open.add(c.id) },
      el("span", { class: "cat" }, catName(c.category)),
      el("span", { class: "name" }, c.name),
      r.detail ? el("span", { class: "detail" }, r.detail) : null,
      r.hint ? el("span", { class: "hint" }, r.hint) : null);
  }));
}

function renderCategory(st, id) {
  const list = checks.filter((c) => c.category === id);
  const s = stats(list, st);
  const name = catName(id);
  $("cat-title").textContent = name;
  const parts = [];
  if (s.running) parts.push(`running, ${s.total - s.running} of ${s.total} done`);
  else if (s.none === s.total) parts.push(`${s.total} checks, not run yet`);
  else {
    parts.push(`${s.pass} passed`, `${s.fail} failed`, `${s.skip} skipped`);
    if (s.at) parts.push(`ran ${ago(s.at)}`);
  }
  $("cat-summary").textContent = parts.join(" · ");
  $("cat-run").textContent = `Run ${name}`;
  $("cat-run").dataset.category = id;
  $("cat-cli").textContent = `helm test ${cfg.release} -n ${cfg.namespace} --filter name=${cfg.testPodPrefix || "<release>-test-"}${id}`;

  const filter = filters[id] || "all";
  for (const b of $("cat-filter").querySelectorAll("button")) {
    const f = b.dataset.filter;
    const n = f === "all" ? s.total : s[f];
    b.textContent = { all: "All", fail: "Failed", skip: "Skipped" }[f] + ` ${n}`;
    b.setAttribute("aria-pressed", String(f === filter));
  }
  // Failures first, then the run order.
  const shown = list
    .filter((c) => filter === "all" || st.get(c.id).status === filter)
    .sort((a, b) => (st.get(b.id).status === "fail") - (st.get(a.id).status === "fail"));
  if (!shown.length) {
    $("cat-checks").replaceChildren(el("p", { class: "empty" }, filter === "all" ? "No checks." : `No ${filter === "fail" ? "failed" : "skipped"} checks.`));
    return;
  }
  $("cat-checks").replaceChildren(...shown.map((c) => {
    const r = st.get(c.id);
    const isOpen = open.has(c.id) || r.status === "fail";
    const toggle = () => { open.has(c.id) ? open.delete(c.id) : open.add(c.id); render(); };
    return el("button", { class: `row${isOpen ? " open" : ""}${r.status === "fail" ? " is-fail" : ""}`, "aria-expanded": String(isOpen), onclick: toggle },
      el("span", { class: `badge ${r.status}` }, LABEL[r.status]),
      el("span", { class: "name" }, c.name),
      el("span", { class: "time" }, r.durationMs != null ? duration(r.durationMs) : ""),
      r.detail ? el("span", { class: "detail" }, r.detail) : null,
      r.hint ? el("span", { class: "hint" }, r.hint) : null);
  }));
}

// --- running --------------------------------------------------------------------------------

async function refresh() {
  report = await api("api/report");
  render();
  if (!report.running && polling) { clearInterval(polling); polling = null; }
  if (report.running && !polling) polling = setInterval(() => refresh().catch(console.error), 1000);
  return report;
}

async function run(groups) {
  $("run").disabled = true;
  $("cat-run").disabled = true;
  const q = groups ? `?groups=${encodeURIComponent(groups)}` : "";
  await api(`api/run${q}`, { method: "POST" });
  await refresh();
  revealCurrent();
}

// --- experiments ----------------------------------------------------------------------------

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

// --- start ----------------------------------------------------------------------------------

async function init() {
  cfg = await api("api/config");
  checks = cfg.checks;
  categories = cfg.categories;
  $("target").textContent = `gateway ${cfg.gateway.namespace}/${cfg.gateway.name} · ${cfg.gateway.host} · namespace ${cfg.namespace}`;
  $("t-v1").textContent = `${100 - cfg.split.mesh}%`;
  $("t-v2").textContent = `${cfg.split.mesh}% · x-version`;

  experiments = await api("api/experiments");
  $("exp-list").replaceChildren(...experiments.map((e) =>
    el("button", { "data-name": e.name, role: "tab", onclick: () => selectExperiment(e.name) }, e.title)));
  if (experiments.length) selectExperiment(experiments[0].name);

  $("run").addEventListener("click", () => run().catch(showError));
  $("cat-run").addEventListener("click", (e) => run(e.currentTarget.dataset.category).catch(showError));
  for (const b of $("cat-filter").querySelectorAll("button")) {
    b.addEventListener("click", () => { filters[route().id] = b.dataset.filter; render(); });
  }
  $("exp-go").addEventListener("click", runExperiment);
  $("exp-n").addEventListener("input", (e) => { $("exp-n-out").textContent = e.target.value; });
  window.addEventListener("hashchange", () => { render(); revealCurrent(); });
  // Keep "4 min ago" current without a run.
  setInterval(render, 30000);
  await refresh();
}

// On a narrow screen the sidebar is one sideways-scrolling row: bring the current view into it.
function revealCurrent() {
  if (!window.matchMedia("(max-width: 860px)").matches) return;
  document.querySelector('.nav-item[aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function showError(e) {
  console.error(e);
  $("last-run").textContent = `Couldn't start the run: ${e.message}`;
  refresh().catch(console.error);
}

init().catch((e) => { $("target").textContent = `Couldn't load the console: ${e.message}`; });
