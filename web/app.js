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
function validateSnapshot(data) {
  if (!data || data.schema_version !== 2 || !Array.isArray(data.rows) || !Array.isArray(data.capabilities)
      || data.snapshot_id !== document.body.dataset.snapshot) throw new Error("Incompatible snapshot");
  const ids = new Set(data.rows.filter(r => r.tier === "screen").map(r => r.model_id));
  const seen = new Set(), validScore = n => Number.isFinite(n) && n >= 0 && n <= 100;
  for (const result of data.capabilities) {
    const key = `${result.model_id}/${result.category}`;
    if (!ids.has(result.model_id) || !["reasoning", "coding"].includes(result.category) || seen.has(key)
        || (result.score != null && !validScore(result.score))
        || (result.interval != null && (!Array.isArray(result.interval) || result.interval.length !== 2
          || !result.interval.every(validScore) || result.interval[0] > result.interval[1]))) throw new Error("Invalid result");
    seen.add(key);
  }
  return data;
}
function eligibilityFresh() {
  const age = Date.now() - Date.parse(report.discovery_at);
  return report.discovery_ok && Number.isFinite(age) && age >= -86400000 && age <= 7 * 86400000;
}
function currentModels() {
  return report.rows.filter(r => r.tier === "screen" && !["removed", "excluded", "paid", "unsupported"].includes(r.availability)
    && (!["authentication_failed", "client_access_restricted", "model_unavailable"].includes(r.availability)
      || (report.capabilities || []).some(c => c.model_id === r.model_id && c.score != null)));
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
  const community = (report.community_results || []).filter(r => r.scores[category] != null && !report.rows.some(m =>
    m.model_id === r.model_id && ["removed", "paid", "excluded"].includes(m.availability))).map(r => {
    const benchmarks = category === "coding" ? ["livecodebench"] : ["gpqa", "livebench"].filter(b => r.sample_counts[b]);
    return {...r, category, score:r.scores[category], interval:r.intervals[category], interval_note:r.interval_notes[category], rank:null,
      name:report.rows.find(m => m.model_id === r.model_id)?.name || r.model_id, availability:"community",
      sample_counts:Object.fromEntries(benchmarks.map(b => [b, r.sample_counts[b]])),
      stale:Date.now() - Date.parse(r.started_at) >= 28 * 86400000};
  });
  return [...(report.capabilities || []).filter(r => r.category === category && r.score != null && visible.has(r.model_id)), ...community]
    .sort((a, b) => Number(b.rank != null) - Number(a.rank != null) || b.score - a.score || a.model_id.localeCompare(b.model_id));
}
function modelFor(result) { return result.submission_id ? result : currentModels().find(m => m.model_id === result.model_id); }
function provenance(result) {
  if (!result.submission_id) return node("span", "Unranked", "sub");
  const link = node("a", `Community · @${result.contributor} · unranked`, "sub");
  link.href = `https://github.com/${report.repository}/blob/main/results/${result.submission_id}.json`;
  return link;
}
function categoryChart(category) {
  const card = node("article", null, "chart-card"), heading = node("div", null, "chart-heading");
  heading.append(node("h3", category === "reasoning" ? "Reasoning" : "Coding"), node("span", "Score / 100", "fine"));
  card.append(heading);
  for (const result of categoryResults(category)) {
    const model = modelFor(result);
    const row = node("div", null, "chart-row"), title = node("div", null, "chart-row-title"), name = node("div");
    name.append(node("strong", model.name), node("span", sampleLabel(result), "sub"));
    if (result.rank == null) name.append(provenance(result));
    if (result.stale) name.append(node("span", "Refresh overdue", "sub"));
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
    const model = modelFor(result);
    const row = node("tr"), name = node("td"), evidence = node("td"), date = node("td");
    name.append(node("strong", model.name), node("span", `Reasoning: ${reasoningSetting(model)}`, "sub"));
    evidence.append(node("span", sampleLabel(result)));
    if (result.rank == null) evidence.append(provenance(result));
    if (model.availability !== "eligible" && !result.submission_id) evidence.append(node("span", statusLabel(model.availability), "sub"));
    const interval = result.interval ? `${result.interval[0].toFixed(1)}–${result.interval[1].toFixed(1)}` : result.interval_note ? "Unresolved" : "—";
    date.append(dateNode(result.evaluated_at));
    if (result.stale) date.append(node("span", "Refresh overdue", "sub"));
    else if (result.refresh_pending) date.append(node("span", "Refresh in progress", "sub"));
    row.append(node("td", result.rank == null ? "—" : String(result.rank)), name, node("td", result.score.toFixed(1), "score"), node("td", interval), evidence, date);
    body.append(row);
  }
  if (!rows.length) { const row = node("tr"), cell = node("td", "Complete results are pending.", "empty"); cell.colSpan = 6; row.append(cell); body.append(row); }
  byId("score-heading").textContent = `${view === "reasoning" ? "Reasoning" : "Coding"} / 100`;
}
function renderPicks() {
  const list = byId("picks"); list.replaceChildren();
  for (const category of ["reasoning", "coding"]) {
    const leaders = categoryResults(category).filter(r => r.rank === 1 && !r.stale && currentModels().find(m => m.model_id === r.model_id)?.availability === "eligible");
    if (!eligibilityFresh() || !leaders.length) continue;
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
  byId("pick-note").hidden = !list.children.length;
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
  const controller = new AbortController(), timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(`snapshot.json?snapshot=${encodeURIComponent(document.body.dataset.snapshot)}`, {cache:"no-store", signal:controller.signal});
    if (!response.ok) throw new Error("Snapshot unavailable");
    report = validateSnapshot(await response.json());
    byId("snapshot-date").textContent = report.published_at ? `Updated ${fmtDate(report.published_at)}` : "Local preview";
    byId("catalog-date").textContent = `Catalog checked ${fmtDate(report.discovery_at)}`;
    const messages = [];
    if (!report.discovery_ok) messages.push("Free eligibility could not be reverified.");
    else if (!eligibilityFresh()) messages.push("Free pricing needs a fresh check; results retain their evaluation dates.");
    if (report.capabilities.some(r => r.stale)) messages.push("Some results are overdue for refresh.");
    const notice = byId("notice"); notice.hidden = !messages.length; notice.textContent = [...new Set(messages)].join(" ");
    byId("benchmark-list").replaceChildren(categoryChart("reasoning"), categoryChart("coding"));
    renderPicks(); renderRankings(); renderAvailability();
    for (const button of document.querySelectorAll("[data-view]")) button.disabled = false;
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  } catch (error) {
    report = null;
    for (const id of ["picks", "ranking", "benchmark-list"]) byId(id).replaceChildren();
    byId("pick-note").hidden = true; byId("supporting").hidden = true;
    byId("notice").hidden = false; byId("notice").setAttribute("role", "alert");
    byId("notice").textContent = "Results could not be loaded. Refresh the page or use the JSON / CSV exports.";
  } finally {
    clearTimeout(timeout); byId("results-table").setAttribute("aria-busy", "false");
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
