"use strict";
let report, view = "reasoning", tier = "screen", reasoningWeight = 0.4;
const byId = (id) => document.getElementById(id);
const fmtDate = (value) => value ? new Date(value).toLocaleDateString(undefined, {year:"numeric",month:"short",day:"numeric"}) : "—";
const num = (value) => value == null ? "—" : value.toLocaleString();
const statusLabel = (value) => ({cap_unverified:"Awaiting pilot",verification_unavailable:"Eligibility unverified",pricing_unknown:"Pricing unverified",quota_limited:"Quota limited",authentication_failed:"Access unavailable",model_unavailable:"Model unavailable",configuration_error:"Configuration unsupported",provider_error:"Provider error",cap_violation:"Output limit exceeded",excluded:"Excluded",unsupported:"Unsupported",removed:"Removed",paid:"Now paid",complete:"Complete",pending:"Pending",stale:"Stale"})[value] || value;
function node(tag, text, className) { const element = document.createElement(tag); if(text != null) element.textContent = text; if(className) element.className=className; return element; }
function score(row) { if(!row.scores) return null; return view === "overall" ? row.scores.reasoning*reasoningWeight+row.scores.coding*(1-reasoningWeight) : row.scores[view]; }
function render() {
  if(!report) return;
  const activeSeason = report.seasons.find(s=>s.active)?.id;
  const rows = report.rows.filter(r=>r.tier===tier);
  rows.sort((a,b)=>(score(b)??-1)-(score(a)??-1)||a.model_id.localeCompare(b.model_id));
  const body = byId("ranking"); body.replaceChildren(); let position=0, lastScore=null, lastRank=null;
  for(const row of rows) {
    const tr=node("tr"); const ranked=row.scores&&row.availability==="eligible"&&row.season===activeSeason;
    if(ranked){position++;if(score(row)!==lastScore){lastRank=position;lastScore=score(row);}}
    tr.append(node("td",ranked?String(lastRank):"—"));
    const name=node("td"); name.append(node("div",row.name,"model"),node("span",row.model_id,"sub"));
    name.append(node("span",`${row.protocol||"unknown protocol"} · epoch ${row.epoch}`,"sub")); tr.append(name);
    tr.append(node("td",score(row)==null?"—":score(row).toFixed(1),"score"));
    const ci = view==="overall"&&reasoningWeight!==0.4 ? null : row.intervals?.[view];
    tr.append(node("td",ci?`${ci[0].toFixed(1)}–${ci[1].toFixed(1)}`:row.scores&&view==="overall"?"Custom weights; see components":"—"));
    tr.append(node("td",fmtDate(row.evaluated_at)));
    const status=node("td");status.append(node("span",statusLabel(row.availability==="eligible"?row.status:row.availability),"badge"));
    status.append(node("span",`${Object.values(row.progress).reduce((a,b)=>a+b,0)} / ${Object.values(row.expected).reduce((a,b)=>a+b,0)} graded`,"sub"));
    if(Object.keys(row.pending_reasons||{}).length)status.append(node("span",Object.entries(row.pending_reasons).map(([s,n])=>`${n} ${s.replaceAll("_"," ")}`).join(" · "),"sub"));
    if(row.deadline_missed) status.append(node("span","Refresh deadline missed","sub"));
    if(row.latency_seconds!=null) status.append(node("span",`${row.latency_seconds.toFixed(1)}s median · ${num(row.accounted_tokens)} accounted tokens · ${((row.truncation_rate||0)*100).toFixed(1)}% truncated`,"sub"));
    tr.append(status); body.append(tr);
  }
  if(!rows.length){const tr=node("tr"),td=node("td","No evaluated models yet. Setup and discovery status are shown above.","empty");td.colSpan=6;tr.append(td);body.append(tr);}
  byId("score-heading").textContent=`${view[0].toUpperCase()+view.slice(1)} / 100`;
  byId("weight-control").hidden=view!=="overall";
  byId("weight-value").textContent=`${Math.round(reasoningWeight*100)}% reasoning / ${Math.round((1-reasoningWeight)*100)}% coding`;
  const comparisons=byId("comparison-list");comparisons.replaceChildren();
  const relevant=report.comparisons.filter(c=>c.tier===tier&&c.view===view);
  if(view==="overall"&&reasoningWeight!==0.4) comparisons.append(node("p","Paired intervals are available for the default overall weights and the separate capability views."));
  else for(const c of relevant) comparisons.append(node("p",`${c.a} vs ${c.b}: ${c.difference.toFixed(1)} points; 95% difference interval ${c.interval[0].toFixed(1)} to ${c.interval[1].toFixed(1)} — ${c.unresolved?"unresolved":"interval excludes zero"}.`));
  if(!relevant.length) comparisons.append(node("p","Comparisons appear when two models finish matching panels."));
}
async function load(){
  try{
    const response=await fetch("snapshot.json",{cache:"no-store"});if(!response.ok)throw new Error("Snapshot unavailable");report=await response.json();
    byId("eligible").textContent=report.rows.filter(r=>r.tier==="screen"&&(r.free_eligible??r.availability==="eligible")).length;
    byId("completed").textContent=report.rows.filter(r=>r.tier==="screen"&&r.scores).length;
    byId("published").textContent=report.published_at?fmtDate(report.published_at):"Local snapshot";
    byId("snapshot-date").textContent=`Snapshot generated ${fmtDate(report.generated_at)}`;
    const notice=byId("notice");notice.replaceChildren(node("b",report.blockers.length?"Evaluations pending":"Evaluation status"));
    const messages=report.blockers.length?report.blockers:[`Discovery checked ${fmtDate(report.discovery_at)}. ${report.missed_deadlines} refresh deadlines missed.`];
    const list=node("ul");for(const text of messages)list.append(node("li",text));notice.append(list);
    const budget=byId("budget");budget.replaceChildren();
    for(const [label,value] of [["Recorded requests / native invocations",`${num(report.budget.attempts_used)} / ${num(report.budget.attempts_limit)}`],["Accounted tokens",`${num(report.budget.accounted_tokens)} / ${num(report.budget.tokens_limit)}`],["Reported / OpenCode-normalized tokens",num(report.budget.reported_tokens)],["Planned attempts / tokens",`${num(report.budget.planned_attempts)} / ${num(report.budget.planned_tokens)}`],["Jobs awaiting completion",num(report.queue_size)],["Missed refresh deadlines",num(report.missed_deadlines)]])budget.append(node("dt",label),node("dd",value));
    const history=byId("history-list");if(report.history.length){history.replaceChildren();for(const row of report.history)history.append(node("article",`${row.name} · ${row.tier} · ${row.season} · ${fmtDate(row.evaluated_at)} · reasoning ${row.scores.reasoning.toFixed(1)} / coding ${row.scores.coding.toFixed(1)} · ${row.availability}`));}
    const pilots=byId("pilot-list");if(report.pilots?.length){pilots.replaceChildren();for(const p of report.pilots){const scores=Object.entries(p.benchmark_scores).map(([b,s])=>`${b}: ${s.toFixed(1)}% (n=${p.expected[b]})`).join(" · ");pilots.append(node("article",`${p.model_id} · ${p.transport} · ${statusLabel(p.status)} · ${p.health_graded}/6 health answers graded · cap probe ${p.cap_probe_verified?"verified":"UNVERIFIED"} · ${Object.values(p.progress).reduce((a,b)=>a+b,0)}/${Object.values(p.expected).reduce((a,b)=>a+b,0)} benchmark questions graded${scores?" · "+scores:""} · evaluated ${fmtDate(p.evaluated_at)} · UNRANKED`));}}
    render();
  }catch(error){byId("ranking").replaceChildren();const tr=node("tr"),td=node("td","The snapshot could not be loaded. Download the JSON export or refresh the page.","empty");td.colSpan=6;tr.append(td);byId("ranking").append(tr);}
}
for(const button of document.querySelectorAll("[data-view]"))button.addEventListener("click",()=>{view=button.dataset.view;for(const b of document.querySelectorAll("[data-view]"))b.classList.toggle("selected",b===button);render();});
byId("tier").addEventListener("change",()=>{tier=byId("tier").value;render();});
byId("weight").addEventListener("input",()=>{reasoningWeight=Number(byId("weight").value)/100;render();});
load();
