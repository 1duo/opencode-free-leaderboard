"use strict";
let report, view = "reasoning";
const byId = id => document.getElementById(id);
const num = value => value == null ? "—" : value.toLocaleString();
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
function modelName(id) { return report.rows.find(r => r.model_id === id)?.name || id; }
function reasoningSetting(row) {
  const variant = row.reasoning_variant;
  const label = variant === "xhigh" ? "Extra high (xhigh)" : variant ? variant[0].toUpperCase() + variant.slice(1) : "";
  return variant ? `${label} · highest exposed` : row.reasoning_setting;
}
function total(values) { return Object.values(values || {}).reduce((a, b) => a + b, 0); }
function currentModels() {
  return report.rows.filter(r => r.tier === "screen" && !["removed", "excluded", "paid"].includes(r.availability));
}
function currentPublicRun(run) {
  return currentModels().some(m => m.model_id === run.model_id && m.epoch === run.epoch);
}
function coverage(model) {
  const run = (report.public_screens || []).find(r => r.model_id === model.model_id && r.epoch === model.epoch);
  const quick = (report.lightweight || []).find(r => r.model_id === model.model_id && r.epoch === model.epoch && r.status === "complete" && r.cap_verified && model.cap_verified);
  const pilot = (report.pilots || []).find(r => r.model_id === model.model_id && r.epoch === model.epoch);
  const full = Boolean(model.scores);
  const complete = full || run?.status === "complete" || Boolean(quick);
  const source = full ? model : run?.status === "complete" ? run : quick || (model.cycle_started_at ? model : run);
  const progress = source?.progress;
  let state = complete ? (full ? "Full screen complete" : run?.status === "complete" ? "Public screen complete" : "Quick check complete") : statusLabel(model.availability);
  let note = "";
  if (model.cap_failure) note = `${num(model.cap_failure.observed)} reported output tokens / ${num(model.cap_failure.limit)} cap`;
  else if (model.availability === "unsupported") note = model.model_id.startsWith("jev-")
    ? "Structured-decision endpoint; no text or code generation" : "Unsupported benchmark endpoint";
  else if (!complete && pilot?.status !== "complete" && pilot) {
    const progress = `Pilot ${total(pilot.progress)}/${total(pilot.expected)}`;
    if (["eligible", "provider_error", "protocol_violation"].includes(model.availability)) state = progress;
    note = [state === progress ? "" : progress, pilot.unanswered_retries_used ? `${pilot.unanswered_retries_used} unanswered after manual retries` : pendingText(pilot.pending_reasons)].filter(Boolean).join(" · ");
  } else if (!complete && run) note = pendingText(run.pending_reasons);
  const counts = {};
  for (const [benchmark, expected] of Object.entries({gpqa:20, livebench:20, livecodebench:40})) {
    counts[benchmark] = model.availability === "unsupported" ? "Not applicable"
      : benchmark === "gpqa" && model.public_only_reason ? "Withheld"
      : progress && source.expected[benchmark] ? `${progress[benchmark] || 0}/${source.expected[benchmark]}` : "Not started";
  }
  return {complete, state, note, counts};
}
function recoveryLabel(run) {
  return run?.manual_retry_count ? `${run.manual_retry_count} logged manual ${run.manual_retry_count === 1 ? "retry" : "retries"} · ${run.manual_recovered_questions} recovered` : "";
}
function facts(entries) {
  const dl = node("dl", null, "facts");
  for (const [label, value] of entries) {
    const dd = node("dd");
    dd.append(value instanceof Node ? value : document.createTextNode(String(value ?? "—")));
    dl.append(node("dt", label), dd);
  }
  return dl;
}
function pendingText(reasons) {
  return Object.entries(reasons || {}).map(([s, n]) => s === "ambiguous"
    ? `${n} unknown outcome${n === 1 ? "" : "s"}`
    : s === "failed" ? `${n} unscored request${n === 1 ? "" : "s"}`
    : s === "grading_blocked" ? `${n} saved answer${n === 1 ? "" : "s"} awaiting grading`
      : `${n} ${statusLabel(s).toLowerCase()}`).join(" · ");
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
function capabilitySource(result) {
  return [...report.rows, ...(report.public_screens || []), ...(report.lightweight || [])].find(r => r.model_id === result.model_id && r.epoch === result.epoch && r.season === result.season && r.tier === result.source_tier);
}
function categoryResults(category) {
  return (report.capabilities || []).filter(r => r.category === category && r.score != null)
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
    name.append(node("strong", model.name), node("span", `Reasoning effort: ${reasoningSetting(model)} · v${model.client_version}`, "sub"));
    evidence.append(node("span", sampleLabel(result)), node("span", result.rank == null ? "Unranked" : "Comparable full screen", "sub"));
    if (model.availability !== "eligible") evidence.append(node("span", statusLabel(model.availability), "sub"));
    if (recoveryLabel(capabilitySource(result))) evidence.append(node("span", recoveryLabel(capabilitySource(result)), "sub"));
    const interval = result.interval ? `${result.interval[0].toFixed(1)}–${result.interval[1].toFixed(1)}` : result.interval_note ? "Unresolved" : "—";
    date.append(dateNode(result.evaluated_at));
    row.append(node("td", result.rank == null ? "—" : String(result.rank)), name, node("td", result.score.toFixed(1), "score"), node("td", interval), evidence, date);
    body.append(row);
  }
  if (!rows.length) { const row = node("tr"), cell = node("td", "Complete results are pending.", "empty"); cell.colSpan = 6; row.append(cell); body.append(row); }
  byId("score-heading").textContent = `${view === "reasoning" ? "Reasoning" : "Coding"} / 100`;
  const list = byId("comparison-list"); list.replaceChildren();
  const ranked = new Set(rows.filter(r => r.rank != null).map(r => r.model_id));
  const comparisons = report.comparisons.filter(c => c.tier === "screen" && c.view === view && ranked.has(c.a) && ranked.has(c.b));
  byId("comparisons").hidden = !comparisons.length;
  for (const c of comparisons) list.append(node("p", `${modelName(c.a)} − ${modelName(c.b)}: ${c.difference.toFixed(1)} points · 95% interval ${c.interval[0].toFixed(1)}–${c.interval[1].toFixed(1)} · ${c.unresolved ? "Unresolved" : "Difference detected"}`, "fine"));
}
function renderArchive() {
  const entries = report.history.filter(r => r.scores).map(r => ({...r, values:r.scores, counts:r.progress}));
  for (const run of [...(report.public_screens || []), ...(report.lightweight || [])]) {
    const model = report.rows.find(m => m.tier === "screen" && m.model_id === run.model_id);
    if (model?.availability === "excluded") continue;
    if ((report.capabilities || []).some(r => r.model_id === run.model_id && r.epoch === run.epoch && r.source_tier === run.tier && r.season === run.season && r.score != null)) continue;
    entries.push({...run, name:modelName(run.model_id), values:{reasoning:run.benchmark_scores.livebench, coding:run.benchmark_scores.livecodebench}, counts:run.progress});
  }
  byId("archive").hidden = !entries.length;
  const list = byId("history-list"); list.replaceChildren();
  for (const run of entries.sort((a, b) => (b.evaluated_at || "").localeCompare(a.evaluated_at || ""))) {
    const reasoningN = (run.counts.gpqa || 0) + (run.counts.livebench || 0), codingN = run.counts.livecodebench || 0;
    list.append(node("p", `${run.name} · Reasoning ${pct(run.values.reasoning)} (${reasoningN} graded) · Coding ${pct(run.values.coding)} (${codingN} graded) · ${run.tier} · OpenCode ${run.client_version || "unrecorded"} · ${fmtDate(run.evaluated_at)}`, "fine"));
  }
}
function renderSupporting() {
  byId("supporting").hidden = false;
  const models = currentModels();
  const completed = models.filter(m => coverage(m).complete).length;
  const unsupported = models.filter(m => m.availability === "unsupported").length;
  byId("availability-summary").textContent = `Model coverage · ${models.length} models · ${completed} complete · ${models.length - completed - unsupported} incomplete${unsupported ? ` · ${unsupported} unsupported` : ""}`;
  byId("discovery-date").textContent = `Catalog checked ${fmtDate(report.discovery_at)}${report.discovery_ok ? "" : " · verification failed"}. Free pricing does not guarantee access.`;
  const list = byId("availability-list"); list.replaceChildren();
  for (const model of models) {
    const result = coverage(model);
    const row = node("tr"), name = node("td");
    name.append(node("strong", model.name), node("span", `Reasoning: ${reasoningSetting(model)}`, "sub"), node("span", model.model_id, "sub"));
    const availability = node("td", result.state);
    if (result.note) availability.append(node("span", result.note, "sub"));
    if (model.next_retry_at) availability.append(node("span", `Retry after ${new Date(model.next_retry_at).toLocaleString()}`, "sub"));
    const counts = ["gpqa", "livebench", "livecodebench"].map(b => {
      const cell = node("td", result.counts[b], "coverage-count");
      cell.dataset.label = {gpqa:"GPQA", livebench:"LiveBench", livecodebench:"Coding"}[b];
      return cell;
    });
    row.append(name, ...counts, availability); list.append(row);
  }
  byId("execution-summary").textContent = `Execution details · week of ${fmtDate(`${report.budget.week}T12:00:00`)}`;
  byId("budget").replaceChildren(...facts([["Recorded client attempts", num(report.budget.opencode_attempts)], ["Discovery requests", num(report.budget.discovery_attempts)], ["Client accounted / recorded tokens", `${num(report.budget.opencode_accounted_tokens)} / ${num(report.budget.opencode_reported_tokens)}`], ["Dispatches with estimated usage", num(report.budget.opencode_estimated_attempts)], ["Total attempts / limit", `${num(report.budget.attempts_used)} / ${num(report.budget.attempts_limit)}`], ["Total accounted tokens / limit", `${num(report.budget.accounted_tokens)} / ${num(report.budget.tokens_limit)}`], ["Planned requests / tokens", `${num(report.budget.planned_attempts)} / ${num(report.budget.planned_tokens)}`], ["Queued OpenCode jobs (includes blocked)", num(report.queue_size)], ["Missed refresh deadlines", num(report.missed_deadlines)]]).childNodes);
  byId("prior-budget").hidden = !report.budget.prior_attempts;
  byId("prior-budget").textContent = `Budget totals retain ${num(report.budget.prior_attempts)} prior experiment attempts and ${num(report.budget.prior_accounted_tokens)} accounted tokens. These are excluded from current results.`;
  const pilots = (report.pilots || []).filter(p => currentPublicRun(p)
    && (p.status === "complete" || total(p.progress) > 0 || p.health_graded > 0));
  byId("pilots").hidden = !pilots.length;
  const pilotList = byId("pilot-list"); pilotList.replaceChildren();
  for (const pilot of pilots) {
    const card = node("article", null, "run-card");
    card.append(node("h3", modelName(pilot.model_id)));
    card.append(facts([["Reasoning", pilot.reasoning_setting], ["Health graded", `${pilot.health_graded}/6`], ["Benchmark graded", `${total(pilot.progress)}/${total(pilot.expected)}`], ["Cap probe", pilot.cap_probe_verified ? "Verified" : "Unverified"], ["Evaluated", dateNode(pilot.evaluated_at)]]));
    for (const [b, s] of Object.entries(pilot.benchmark_scores)) card.append(node("p", `${{gpqa:"GPQA Diamond", livebench:"LiveBench", livecodebench:"LiveCodeBench"}[b]}: ${pct(s)} · ${pilot.expected[b]} questions`, "fine"));
    if (recoveryLabel(pilot)) card.append(node("p", recoveryLabel(pilot), "fine"));
    pilotList.append(card);
  }
  renderArchive();
}
async function load() {
  try {
    const response = await fetch("snapshot.json", {cache:"no-store"});
    if (!response.ok) throw new Error("Snapshot unavailable");
    report = await response.json();
    byId("evaluated-models").textContent = currentModels().length;
    byId("completed-runs").textContent = currentModels().filter(m => ["reasoning", "coding"].every(c => (report.capabilities || []).some(r => r.model_id === m.model_id && r.category === c && r.score != null))).length;
    byId("graded-answers").textContent = num((report.capabilities || []).filter(r => r.score != null).reduce((n, r) => n + total(r.sample_counts), 0));
    byId("published").replaceChildren(report.published_at ? dateNode(report.published_at) : node("span", "Local preview"));
    byId("snapshot-date").textContent = `Snapshot ${fmtDate(report.generated_at)}`;
    const messages = [...report.blockers];
    if (!report.discovery_ok) messages.push("Free eligibility could not be reverified.");
    const notice = byId("notice"); notice.hidden = !messages.length; notice.textContent = [...new Set(messages)].join(" ");
    byId("benchmark-list").replaceChildren(categoryChart("reasoning"), categoryChart("coding"));
    renderRankings(); renderSupporting();
    if (location.hash) document.getElementById(location.hash.slice(1))?.scrollIntoView();
  } catch (error) {
    byId("notice").hidden = false; byId("notice").setAttribute("role", "alert");
    byId("notice").textContent = "Results could not be loaded. Refresh the page or use the JSON / CSV exports.";
  }
}
for (const button of document.querySelectorAll("[data-view]")) button.addEventListener("click", () => {
  view = button.dataset.view;
  for (const b of document.querySelectorAll("[data-view]")) { b.classList.toggle("selected", b === button); b.setAttribute("aria-pressed", String(b === button)); }
  if (report) renderRankings();
});
load();
