"use strict";
let report, view = "reasoning", tier = "screen", reasoningWeight = 0.4;
const byId = id => document.getElementById(id);
const num = value => value == null ? "—" : value.toLocaleString();
const pct = value => value == null ? "—" : `${value.toFixed(1)}%`;
const fmtDate = value => value ? new Date(value).toLocaleDateString(undefined, {year:"numeric", month:"short", day:"numeric"}) : "—";
const statusLabel = value => ({eligible:"Runnable", cap_unverified:"Cap unverified", verification_unavailable:"Eligibility unverified", pricing_unknown:"Pricing unverified", quota_limited:"Quota limited", authentication_failed:"Access unavailable", model_unavailable:"Model unavailable", configuration_error:"Configuration unsupported", client_access_restricted:"Free-tier access restricted", provider_error:"Provider error", cap_violation:"Output limit exceeded", excluded:"Excluded", unsupported:"Unsupported protocol", removed:"Removed", paid:"Now paid", complete:"Complete", pending:"Pending", stale:"Stale"})[value] || value.replaceAll("_", " ");
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
function total(values) { return Object.values(values || {}).reduce((a, b) => a + b, 0); }
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
  return Object.entries(reasons || {}).map(([s, n]) => s === "grading_blocked" ? `${n} saved answer awaiting grading` : `${n} ${statusLabel(s).toLowerCase()}`).join(" · ");
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
function renderPublicScreens() {
  const screens = report.public_screens || [];
  byId("results").hidden = !screens.length;
  const groups = new Map();
  for (const run of screens) {
    if (!groups.has(run.season)) groups.set(run.season, []);
    groups.get(run.season).push(run);
  }
  const list = byId("public-screen-list"); list.replaceChildren();
  for (const [season, runs] of groups) {
    const manifest = report.seasons.find(s => s.id === season);
    const group = node("div", null, "season-results");
    if (groups.size > 1) group.append(node("h3", season, "season-label"));
    const charts = node("div", null, "chart-grid");
    for (const [benchmark, label, release] of [["livebench", "LiveBench reasoning", manifest?.livebench_release], ["livecodebench", "LiveCodeBench coding", manifest?.livecodebench_release]]) {
      const card = node("article", null, "chart-card"), heading = node("div", null, "chart-heading");
      heading.append(node("h3", label), node("span", release || "Panel date unavailable", "fine"));
      card.append(heading);
      for (const run of runs) {
        const value = run.benchmark_scores[benchmark], ci = run.intervals[benchmark];
        const row = node("div", null, "chart-row"), title = node("div", null, "chart-row-title"), name = node("div");
        name.append(node("strong", modelName(run.model_id)));
        title.append(name, node("strong", pct(value), "chart-score")); row.append(title);
        if (value != null) {
          row.append(intervalPlot(value, ci));
          const info = node("p", null, "chart-meta");
          info.append(node("span", `${run.expected[benchmark]} questions · ${ci ? `95% interval ${ci[0].toFixed(1)}–${ci[1].toFixed(1)}%` : "Interval unavailable"}`), dateNode(run.benchmark_evaluated_at?.[benchmark] || run.evaluated_at));
          row.append(info);
        } else row.append(node("p", `${run.progress[benchmark] || 0}/${run.expected[benchmark]} graded · score pending`, "chart-pending"));
        card.append(row);
      }
      const axis = node("div", null, "chart-axis"); axis.setAttribute("aria-hidden", "true");
      for (const value of [0, 25, 50, 75, 100]) axis.append(node("span", String(value)));
      card.append(axis); charts.append(card);
    }
    group.append(charts); list.append(group);
  }
  const details = byId("run-detail-list"); details.replaceChildren();
  for (const run of screens) {
    const card = node("article", null, "run-card");
    card.append(node("h3", modelName(run.model_id)));
    card.append(facts([["Client", `OpenCode ${run.client_version}`], ["Status", statusLabel(run.status)], ["Graded", `${total(run.progress)}/${total(run.expected)} answers`], ["Output cap", run.cap_verified ? "Verified" : "Unverified"], ["Median latency", run.latency_seconds == null ? "—" : `${run.latency_seconds.toFixed(1)} s`], ["Accounted tokens", num(run.accounted_tokens)], ["Client-recorded tokens", num(run.reported_tokens)], ["Truncated", pct(run.truncation_rate == null ? null : run.truncation_rate * 100)], ["Evaluated", dateNode(run.evaluated_at)]]));
    if (pendingText(run.pending_reasons)) card.append(node("p", pendingText(run.pending_reasons), "fine"));
    card.append(node("p", `${run.model_id} · epoch ${run.epoch}`, "identifier"), node("p", `Season ${run.season}`, "identifier"));
    details.append(card);
  }
}
function score(row) {
  if (!row.scores) return null;
  return view === "overall" ? row.scores.reasoning * reasoningWeight + row.scores.coding * (1 - reasoningWeight) : row.scores[view];
}
function renderRankings() {
  const activeSeason = report.seasons.find(s => s.active)?.id;
  const headlineRows = report.rows.filter(r => r.season === activeSeason && r.season != null && (r.scores || r.cycle_started_at));
  byId("headline").hidden = !headlineRows.length;
  const rows = headlineRows.filter(r => r.tier === tier);
  const ranked = row => row.scores && row.availability === "eligible";
  rows.sort((a, b) => Number(Boolean(ranked(b))) - Number(Boolean(ranked(a))) || (score(b) ?? -1) - (score(a) ?? -1) || a.model_id.localeCompare(b.model_id));
  const body = byId("ranking"); body.replaceChildren();
  let position = 0, lastScore = null, lastRank = null;
  for (const row of rows) {
    if (ranked(row)) { position++; if (score(row) !== lastScore) { lastRank = position; lastScore = score(row); } }
    const tr = node("tr"), name = node("td");
    name.append(node("strong", row.name), node("span", row.model_id, "sub"));
    tr.append(node("td", ranked(row) ? String(lastRank) : "—"), name, node("td", score(row) == null ? "—" : score(row).toFixed(1), "score"));
    const ci = view === "overall" && reasoningWeight !== 0.4 ? null : row.intervals?.[view];
    tr.append(node("td", ci ? `${ci[0].toFixed(1)}–${ci[1].toFixed(1)}` : row.scores && view === "overall" ? "Custom weights" : "—"));
    const date = node("td"); date.append(dateNode(row.evaluated_at)); tr.append(date);
    const status = node("td");
    status.append(node("span", statusLabel(row.availability === "eligible" ? row.status : row.availability), "badge"), node("span", `${total(row.progress)}/${total(row.expected)} graded`, "sub"));
    if (pendingText(row.pending_reasons)) status.append(node("span", pendingText(row.pending_reasons), "sub"));
    if (row.deadline_missed) status.append(node("span", "Refresh deadline missed", "sub"));
    if (row.latency_seconds != null) status.append(node("span", `${row.latency_seconds.toFixed(1)} s · ${num(row.accounted_tokens)} tokens · ${pct(row.truncation_rate == null ? null : row.truncation_rate * 100)} truncated`, "sub"));
    tr.append(status); body.append(tr);
  }
  if (!rows.length) { const tr = node("tr"), td = node("td", "No evaluations in this panel yet.", "empty"); td.colSpan = 6; tr.append(td); body.append(tr); }
  byId("score-heading").textContent = `${view[0].toUpperCase() + view.slice(1)} / 100`;
  byId("weight-control").hidden = view !== "overall";
  byId("weight-value").textContent = `${Math.round(reasoningWeight * 100)}% reasoning / ${Math.round((1 - reasoningWeight) * 100)}% coding`;
  const comparisons = byId("comparison-list"); comparisons.replaceChildren();
  const relevant = report.comparisons.filter(c => c.tier === tier && c.view === view && c.season === activeSeason);
  byId("comparisons").hidden = !relevant.length;
  if (view === "overall" && reasoningWeight !== 0.4) comparisons.append(node("p", "Paired intervals use the default weights. Select a component or reset to 40% / 60%."));
  else for (const c of relevant) comparisons.append(node("p", `${modelName(c.a)} / ${modelName(c.b)}: ${c.difference.toFixed(1)} points · 95% difference interval ${c.interval[0].toFixed(1)}–${c.interval[1].toFixed(1)} · ${c.unresolved ? "Unresolved" : "Interval excludes zero"}`));
}
function renderSupporting() {
  byId("supporting").hidden = false;
  const models = report.rows.filter(r => r.tier === "screen");
  const runnable = models.filter(r => r.availability === "eligible" && r.cap_verified).length;
  const restricted = models.filter(r => r.free_eligible && r.protocol === "opencode" && !(r.availability === "eligible" && r.cap_verified)).length;
  byId("availability-summary").textContent = `Model availability · ${runnable} runnable${restricted ? `, ${restricted} awaiting access or verification` : ""}`;
  byId("discovery-date").textContent = `Catalog checked ${fmtDate(report.discovery_at)}${report.discovery_ok ? "" : " · verification failed"}. Free pricing does not guarantee access.`;
  const list = byId("availability-list"); list.replaceChildren();
  for (const model of models) {
    const row = node("tr"), name = node("td");
    name.append(node("strong", model.name), node("span", model.model_id, "sub"));
    const state = model.availability === "eligible" && !model.cap_verified ? "cap_unverified" : model.availability;
    row.append(name, node("td", statusLabel(state))); list.append(row);
  }
  byId("execution-summary").textContent = `Execution details · week of ${fmtDate(`${report.budget.week}T12:00:00`)}`;
  byId("budget").replaceChildren(...facts([["Recorded client attempts", num(report.budget.opencode_attempts)], ["Discovery requests", num(report.budget.discovery_attempts)], ["Client accounted / recorded tokens", `${num(report.budget.opencode_accounted_tokens)} / ${num(report.budget.opencode_reported_tokens)}`], ["Dispatches with estimated usage", num(report.budget.opencode_estimated_attempts)], ["Total attempts / limit", `${num(report.budget.attempts_used)} / ${num(report.budget.attempts_limit)}`], ["Total accounted tokens / limit", `${num(report.budget.accounted_tokens)} / ${num(report.budget.tokens_limit)}`], ["Planned requests / tokens", `${num(report.budget.planned_attempts)} / ${num(report.budget.planned_tokens)}`], ["Queued OpenCode jobs (includes blocked)", num(report.queue_size)], ["Missed refresh deadlines", num(report.missed_deadlines)]]).childNodes);
  byId("prior-budget").hidden = !report.budget.prior_attempts;
  byId("prior-budget").textContent = `Budget totals retain ${num(report.budget.prior_attempts)} prior experiment attempts and ${num(report.budget.prior_accounted_tokens)} accounted tokens. These are excluded from current results.`;
  const pilots = (report.pilots || []).filter(p => p.status === "complete" || total(p.progress) > 0 || p.health_graded > 0);
  byId("pilots").hidden = !pilots.length;
  const pilotList = byId("pilot-list"); pilotList.replaceChildren();
  for (const pilot of pilots) {
    const card = node("article", null, "run-card");
    card.append(node("h3", modelName(pilot.model_id)));
    card.append(facts([["Health graded", `${pilot.health_graded}/6`], ["Benchmark graded", `${total(pilot.progress)}/${total(pilot.expected)}`], ["Cap probe", pilot.cap_probe_verified ? "Verified" : "Unverified"], ["Evaluated", dateNode(pilot.evaluated_at)]]));
    for (const [b, s] of Object.entries(pilot.benchmark_scores)) card.append(node("p", `${b === "livebench" ? "LiveBench" : "LiveCodeBench"}: ${pct(s)} · ${pilot.expected[b]} questions`, "fine"));
    pilotList.append(card);
  }
  byId("archive").hidden = !report.history.length;
  for (const row of report.history) byId("history-list").append(node("p", `${row.name} · ${row.tier} · ${fmtDate(row.evaluated_at)} · reasoning ${row.scores.reasoning.toFixed(1)} / coding ${row.scores.coding.toFixed(1)} · ${statusLabel(row.availability)} · ${row.season}`));
}
async function load() {
  try {
    const response = await fetch("snapshot.json", {cache:"no-store"});
    if (!response.ok) throw new Error("Snapshot unavailable");
    report = await response.json();
    const runs = [...(report.public_screens || []), ...report.rows.filter(r => r.tier === "screen")];
    byId("evaluated-models").textContent = new Set(runs.filter(r => r.scores || Object.keys(r.benchmark_scores || {}).length).map(r => r.model_id)).size;
    byId("completed-runs").textContent = runs.filter(r => r.scores || r.status === "complete").length;
    byId("graded-answers").textContent = num(runs.reduce((n, r) => n + total(r.progress), 0));
    byId("published").replaceChildren(report.published_at ? dateNode(report.published_at) : node("span", "Local preview"));
    byId("snapshot-date").textContent = `Snapshot ${fmtDate(report.generated_at)}`;
    const messages = [];
    if (report.blockers.includes("Authenticated GPQA access is not configured")) messages.push("GPQA access pending. Full rankings are unavailable.");
    for (const blocker of report.blockers) if (blocker !== "Authenticated GPQA access is not configured" && !(messages.length && blocker === "Benchmark season awaits preparation and grader validation")) messages.push(blocker);
    if (!report.discovery_ok) messages.push("Free eligibility could not be reverified.");
    const notice = byId("notice"); notice.hidden = !messages.length; notice.textContent = [...new Set(messages)].join(" ");
    renderPublicScreens(); renderRankings(); renderSupporting();
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
byId("tier").addEventListener("change", () => { tier = byId("tier").value; if (report) renderRankings(); });
byId("weight").addEventListener("input", () => { reasoningWeight = Number(byId("weight").value) / 100; if (report) renderRankings(); });
load();
