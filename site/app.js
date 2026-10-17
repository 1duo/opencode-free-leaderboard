"use strict";
let report, view = "reasoning";
const byId = id => document.getElementById(id);
const pct = value => value == null ? "—" : `${value.toFixed(1)}%`;
const fmtDate = value => value ? new Date(value).toLocaleDateString(undefined, {year:"numeric", month:"short", day:"numeric"}) : "—";
const statusLabel = value => ({eligible:"Runnable", cap_unverified:"Cap unverified", verification_unavailable:"Eligibility unverified", pricing_unknown:"Pricing unverified", quota_limited:"Quota limited", authentication_failed:"Access unavailable", model_unavailable:"Model unavailable", configuration_error:"Configuration unsupported", client_access_restricted:"Free-tier access restricted", provider_error:"Request interrupted", ambiguous:"Unknown outcome", dispatching:"Running", provider_overloaded:"Provider overloaded", protocol_violation:"Single-turn check failed", cap_violation:"Cap verification failed", excluded:"Excluded", unsupported:"Unsupported protocol", removed:"Removed", paid:"Now paid", complete:"Complete", pending:"Pending", stale:"Stale"})[value] || value.replaceAll("_", " ");
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text != null) element.textContent = text;
  if (className) element.className = className;
  return element;
}
function dateNode(value) {
  const element = node("time", fmtDate(value));
  if (value) { element.dateTime = value; element.title = new Date(value).toLocaleString(); }
  return element;
}
function reasoningSetting(row) {
  const variant = row.reasoning_variant;
  const label = variant === "xhigh" ? "Extra high (xhigh)" : variant ? variant[0].toUpperCase() + variant.slice(1) : "";
  return variant ? `${label} · highest exposed` : row.reasoning_setting;
}
function total(values) { return Object.values(values || {}).reduce((a, b) => a + b, 0); }
function currentModels() {
  return report.rows.filter(r => r.tier === "screen" && !["removed", "excluded", "paid", "unsupported"].includes(r.availability));
}
function intervalPlot(value, interval) {
  const plot = node("div", null, "interval-plot");
  plot.setAttribute("role", "img");
  plot.setAttribute("aria-label", `Score ${pct(value)}${interval ? `; 95% interval ${pct(interval[0])} to ${pct(interval[1])}` : "; interval unavailable"}`);
  if (interval) {
    const range = node("span", null, "interval-range");
    range.style.left = `${Math.max(0, interval[0])}%`;
    range.style.width = `${Math.max(0, Math.min(100, interval[1]) - Math.max(0, interval[0]))}%`;
    plot.append(range);
  }
  const point = node("span", null, "interval-point");
  point.style.left = `${Math.min(100, Math.max(0, value))}%`;
  plot.append(point);
  return plot;
}
function basis(result) {
  return Object.keys(result.sample_counts).map(b => ({gpqa:"GPQA", livebench:"LiveBench", livecodebench:"LiveCodeBench"})[b]).join(" + ");
}
function sampleLabel(result) {
  return `${basis(result)} · ${total(result.sample_counts)} questions`;
}
function categoryResults(category) {
  const visible = new Set(currentModels().map(m => m.model_id));
  return (report.capabilities || []).filter(r => r.category === category && r.score != null && visible.has(r.model_id))
    .sort((a, b) => Number(b.rank != null) - Number(a.rank != null) || b.score - a.score || a.model_id.localeCompare(b.model_id));
}
function categoryChart(category) {
  const card = node("article", null, "chart-card"), heading = node("div", null, "chart-heading");
  heading.append(node("h3", category === "reasoning" ? "Reasoning" : "Coding"), node("span", "Score / 100", "fine"));
  card.append(heading);
  for (const result of categoryResults(category)) {
    const model = currentModels().find(m => m.model_id === result.model_id);
    const row = node("div", null, "chart-row"), title = node("div", null, "chart-row-title"), name = node("div");
    name.append(node("strong", model.name), node("span", sampleLabel(result), "sub"));
    if (result.rank == null) name.append(node("span", "Unranked", "sub"));
    title.append(name, node("strong", pct(result.score), "chart-score"));
    row.append(title, intervalPlot(result.score, result.interval));
    const meta = node("p", null, "chart-meta");
    meta.append(node("span", result.interval ? `95% interval ${result.interval[0].toFixed(1)}–${result.interval[1].toFixed(1)}%` : result.interval_note ? "Uncertainty unresolved" : "Interval unavailable"), dateNode(result.evaluated_at));
    row.append(meta); card.append(row);
  }
  if (!categoryResults(category).length) card.append(node("p", "Complete results are pending.", "fine"));
  const axis = node("div", null, "chart-axis"); axis.setAttribute("aria-hidden", "true");
  for (const value of [0, 25, 50, 75, 100]) axis.append(node("span", String(value)));
  card.append(axis); return card;
}
function renderRankings() {
  const rows = categoryResults(view), body = byId("ranking"); body.replaceChildren();
  for (const result of rows) {
    const model = currentModels().find(m => m.model_id === result.model_id);
    const row = node("tr"), name = node("td"), evidence = node("td"), date = node("td");
    name.append(node("strong", model.name), node("span", `Reasoning: ${reasoningSetting(model)}`, "sub"));
    evidence.append(node("span", sampleLabel(result)));
    if (result.rank == null) evidence.append(node("span", "Unranked", "sub"));
    if (model.availability !== "eligible") evidence.append(node("span", statusLabel(model.availability), "sub"));
    const interval = result.interval ? `${result.interval[0].toFixed(1)}–${result.interval[1].toFixed(1)}` : result.interval_note ? "Unresolved" : "—";
    date.append(dateNode(result.evaluated_at));
    if (model.status === "stale") date.append(node("span", "Stale", "sub"));
    row.append(node("td", result.rank == null ? "—" : String(result.rank)), name, node("td", result.score.toFixed(1), "score"), node("td", interval), evidence, date);
    body.append(row);
  }
  if (!rows.length) { const row = node("tr"), cell = node("td", "Complete results are pending.", "empty"); cell.colSpan = 6; row.append(cell); body.append(row); }
  byId("score-heading").textContent = `${view === "reasoning" ? "Reasoning" : "Coding"} / 100`;
}
function renderPicks() {
  const list = byId("picks"); list.replaceChildren();
  for (const category of ["reasoning", "coding"]) {
    const leaders = categoryResults(category).filter(r => r.rank === 1 && currentModels().find(m => m.model_id === r.model_id)?.availability === "eligible");
    if (!report.discovery_ok || !leaders.length) continue;
    const card = node("article", null, "pick-card");
    card.append(node("p", `Top ${category} score${leaders.length > 1 ? " · tied" : ""}`, "eyebrow"));
    for (const result of leaders) {
      const model = currentModels().find(m => m.model_id === result.model_id);
      const heading = node("h3"), link = node("a", model.name);
      link.href = "#headline"; link.addEventListener("click", () => selectView(category));
      heading.append(link);
      card.append(heading, node("code", model.model_id, "model-id"), node("p", `Reasoning: ${reasoningSetting(model)}`, "fine"));
    }
    card.append(node("strong", `${leaders[0].score.toFixed(1)} / 100`, "pick-score"));
    list.append(card);
  }
}
function renderAvailability() {
  const models = currentModels().filter(m => !["reasoning", "coding"].every(c => (report.capabilities || []).some(r => r.model_id === m.model_id && r.category === c && r.score != null)));
  byId("supporting").hidden = !models.length;
  byId("availability-summary").textContent = `Other catalog models · ${models.length} without complete results`;
  const list = byId("availability-list"); list.replaceChildren();
  for (const model of models) {
    const row = node("p", null, "availability-item");
    const label = model.availability === "cap_violation" ? "No validated benchmark score"
      : model.availability === "unsupported" ? "Not a supported chat / code endpoint"
      : model.availability === "eligible" ? "Evaluation pending" : statusLabel(model.availability);
    row.append(node("strong", model.name), node("span", label, "sub"));
    list.append(row);
  }
}
async function load() {
  try {
    const response = await fetch("snapshot.json", {cache:"no-store"});
    if (!response.ok) throw new Error("Snapshot unavailable");
    report = await response.json();
    byId("snapshot-date").textContent = report.published_at ? `Updated ${fmtDate(report.published_at)}` : "Local preview";
    byId("catalog-date").textContent = `Free pricing checked ${fmtDate(report.discovery_at)}`;
    const messages = [];
    if (!report.discovery_ok) messages.push("Free eligibility could not be reverified.");
    const notice = byId("notice"); notice.hidden = !messages.length; notice.textContent = [...new Set(messages)].join(" ");
    byId("benchmark-list").replaceChildren(categoryChart("reasoning"), categoryChart("coding"));
    renderPicks(); renderRankings(); renderAvailability();
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  } catch (error) {
    byId("notice").hidden = false; byId("notice").setAttribute("role", "alert");
    byId("notice").textContent = "Results could not be loaded. Refresh the page or use the JSON / CSV exports.";
  }
}
function selectView(category) {
  view = category;
  for (const button of document.querySelectorAll("[data-view]")) {
    const selected = button.dataset.view === category;
    button.classList.toggle("selected", selected); button.setAttribute("aria-pressed", String(selected));
  }
  if (report) renderRankings();
}
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => selectView(button.dataset.view));
load();
