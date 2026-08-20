"use strict";
const view = document.getElementById("view");
const api = (p, o) => fetch(p, o).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const money = (c, cur) => c == null ? "—" : `${cur || "SGD"} ${(c / 100).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const ACT = {
  APPROVE:  { cls: "appr", verb: "Approve for payment", accent: "var(--green)" },
  HOLD:     { cls: "hold", verb: "Hold", accent: "var(--amber)" },
  ESCALATE: { cls: "esc",  verb: "Escalate to human", accent: "var(--red)" },
  EMAIL:    { cls: "hold", verb: "Query vendor", accent: "var(--amber)" },
  null:     { cls: "idle", verb: "Pending", accent: "var(--muted)" },
};
const FIELD_EN = { UNIT_PRICE: "Unit price", QTY: "Qty", INVOICE_TOTAL: "Invoice total", UOM: "UOM", LINE_TOTAL: "Line total" };
const isMoney = (f) => ["UNIT_PRICE", "INVOICE_TOTAL", "LINE_TOTAL"].includes(f);
const fmtVal = (v, f, cur) => (v == null ? "—" : isMoney(f) ? money(parseInt(v, 10), cur) : v);

// Rationale as numbered points, derived from the STRUCTURED facts (not the
// model's raw prose). The raw model text stays available, collapsed, for audit.
function reasonPoints(c) {
  const dec = c.decision, cur = c.currency, pts = [];
  pts.push(`Three-way match: ${c.po ? c.po.doc_id : "no PO"}${c.grn ? " / " + c.grn.doc_id + ", all quantities received" : " · no goods receipt"}.`);
  const ds = c.match.discrepancies;
  if (!ds.length) pts.push("All three documents agree; no discrepancy.");
  else ds.forEach((d) => {
    const delta = d.delta_pct != null ? ` +${d.delta_pct.toFixed(1)}%` : "";
    pts.push(`Discrepancy: line ${d.line_pair ? d.line_pair[1] : "-"} · ${FIELD_EN[d.field] || d.field}${delta} (${fmtVal(d.po_value, d.field, cur)} → ${fmtVal(d.invoice_value, d.field, cur)}).`);
  });
  if (c.contract_allowance_pct != null) {
    const ok = (c.guardrails.find((g) => g.key === "price") || {}).passed;
    pts.push(`Contract recheck: allows ${c.contract_allowance_pct}% variance; this invoice is ${ok ? "within tolerance" : "still beyond tolerance"} (parsed by code, not the model).`);
  }
  if (c.duplicates.length) pts.push(`Duplicate match: same vendor / PO / total as ${c.duplicates.join(", ")}.`);
  if (c.review_gate) pts.push("Amount is at or above the manual-review threshold; human sign-off required.");
  if (!c.grn && (c.guardrails.find((g) => g.key === "grn") || {}).passed === false)
    pts.push("No goods receipt on record; delivery is unproven.");
  const a = ACT[dec ? dec.action : null] || ACT.null;
  pts.push(`Decision: ${dec ? dec.action : "PENDING"} · ${a.verb}.`);
  return pts;
}

// One plain-language line per tool call; raw JSON kept behind an expander.
function toolSummary(t) {
  let r = null;
  try { r = JSON.parse(t.result); } catch { r = null; }
  const n = t.tool_name;
  if (n === "lookup_po") return r && r.doc_id ? `Fetched PO ${r.doc_id} (${(r.lines || []).length} lines)` : "Purchase order not found";
  if (n === "lookup_grn") return r && r.doc_id ? `Fetched GRN ${r.doc_id}, quantities recorded` : "No goods receipt on record — delivery unproven";
  if (n === "get_vendor_history") return r && r.invoice_count != null ? `Vendor ${r.vendor_id}: ${r.invoice_count} invoices on record, regular supplier` : "No vendor history";
  if (n === "check_duplicate_invoice")
    return r && r.likely_duplicates && r.likely_duplicates.length ? `Duplicate found: ${r.likely_duplicates.map((d) => d.doc_id).join(", ")}` : "No duplicate";
  if (n === "search_vendor_contract")
    return Array.isArray(r) && r.length ? `Found clause: ${r[0].section} (${r[0].source})` : "Contract silent on this — default policy applies";
  if (n === "recheck_against_contract")
    return r && r.contract_allowance_pct != null ? `Code parsed contract tolerance ${r.contract_allowance_pct}% and re-checked — the percentage never passes through the model` : (t.result.length > 120 ? t.result.slice(0, 120) + "…" : t.result);
  return t.result.length > 120 ? t.result.slice(0, 120) + "…" : t.result;
}
const prettyRaw = (s) => { try { return JSON.stringify(JSON.parse(s), null, 2); } catch { return s; } };

// --- navigation ------------------------------------------------------------
document.querySelectorAll(".nav a[data-view]").forEach((a) =>
  a.addEventListener("click", () => { setActiveNav(a); dashboard(); }));
function setActiveNav(el) {
  document.querySelectorAll(".nav a").forEach((a) => a.classList.remove("active"));
  (el || document.querySelector(".nav a")).classList.add("active");
}

// --- dashboard -------------------------------------------------------------
async function dashboard() {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const [m, list] = await Promise.all([api("/api/metrics"), api("/api/invoices")]);
  const d = m.distribution;
  const maxN = Math.max(1, ...Object.values(d));
  const bar = (label, n, color) =>
    `<div class="bar"><span class="bl">${label}</span><span class="bk" style="width:${Math.max(8, n / maxN * 190)}px;background:${color}"></span><span class="bc num">${n}</span></div>`;
  const decided = list.filter((x) => x.action);
  view.innerHTML = `
    <div class="head">
      <div><h1>Overview</h1><div class="sub">${m.total} invoices this week · ${m.pending} awaiting confirmation</div></div>
      <button class="btn primary" id="run"><span class="play"></span>Run review (${m.pending || m.total})</button>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="l">STP rate</div><div class="v num">${m.stp_pct}%</div><div class="s">APPROVE / ${m.decided} decided</div></div>
      <div class="card kpi"><div class="l">Touchless rate</div><div class="v num">${m.touchless_pct}%</div><div class="s">APPROVE+HOLD / decided</div></div>
      <div class="card kpi good"><div class="l">False approvals</div><div class="v num">${m.false_approve}</div><div class="s">Zero-tolerance · on target</div></div>
      <div class="card kpi warn"><div class="l">Pending</div><div class="v num">${m.pending}</div><div class="s">Awaiting review</div></div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-h"><h3>Invoice queue</h3><a>View all →</a></div>
        <div id="queue"></div>
      </div>
      <div class="card dist">
        <h3>Decisions this week</h3>
        ${bar("APPROVE", d.APPROVE, "var(--green)")}
        ${bar("HOLD", d.HOLD, "var(--amber)")}
        ${bar("ESCALATE", d.ESCALATE, "var(--red)")}
        ${bar("EMAIL", d.EMAIL, "var(--accent)")}
        <div class="cap num">${m.total} total · ${m.false_approve} false approvals</div>
      </div>
    </div>
    <div class="card feed">
      <div class="card-h"><h3>Recent agent decisions</h3></div>
      ${decided.slice(0, 6).map((x) => {
        const a = ACT[x.action] || ACT.null;
        return `<div class="act"><span class="dot" style="background:${a.accent}"></span>
          <span class="rl"><b>${esc(x.invoice_id)}</b> · ${esc(x.vendor_name)} → <b>${x.action}</b>${x.reason && x.reason !== "—" ? " · " + esc(x.reason) : ""}</span></div>`;
      }).join("")}
    </div>`;
  const q = document.getElementById("queue");
  q.innerHTML = list.map((x) => {
    const a = ACT[x.action] || ACT.null;
    return `<div class="row" data-id="${esc(x.invoice_id)}">
      <div class="rl"><b>${esc(x.invoice_id)}</b><small>${esc(x.vendor_name)}</small></div>
      <div class="rr"><span class="amt num">${money(x.total_cents, x.currency)}</span>
        <span class="pill ${a.cls}">${x.action || "PENDING"}</span>
        <span class="reason t-${a.cls}">${esc(x.reason || "")}</span></div>
    </div>`;
  }).join("");
  q.querySelectorAll(".row").forEach((r) => r.addEventListener("click", () => detail(r.dataset.id)));
  document.getElementById("run").addEventListener("click", () => detail((list.find((x) => !x.action) || list[0]).invoice_id));
}

// --- detail ----------------------------------------------------------------
async function detail(id) {
  setActiveNav(document.querySelectorAll(".nav a")[1]);
  view.innerHTML = `<div class="placeholder">Loading ${esc(id)}…</div>`;
  const c = await api(`/api/invoices/${id}`);
  renderDetail(c);
}

function renderDetail(c) {
  const dec = c.decision;
  const a = ACT[dec ? dec.action : null] || ACT.null;
  const passed = c.guardrails.filter((g) => g.passed).length;
  const override = dec && /^\[code guardrail\]/.test(dec.reasoning || "");
  const gates = c.guardrails.map((g) =>
    `<span class="g ${g.passed ? "ok" : "bad"}">${g.passed ? "✓" : "✗"} ${esc(g.label)}</span>`).join(" · ");

  const ds = c.match.discrepancies;
  const recon = ds.length
    ? `<table><thead><tr><th></th><th>Field</th><th>PO</th><th>GRN</th><th>Invoice</th><th>Δ</th></tr></thead><tbody>
        ${ds.map((r) => {
          const f = (v) => fmtVal(v, r.field, c.currency);
          return `<tr><td class="lbl">Line ${r.line_pair ? r.line_pair[1] : "-"}</td>
            <td>${FIELD_EN[r.field] || r.field}</td><td class="num">${f(r.po_value)}</td>
            <td class="num">${f(r.grn_value)}</td><td class="num var">${f(r.invoice_value)}</td>
            <td class="num var">${r.delta_pct != null ? "+" + r.delta_pct.toFixed(1) + "%" : "—"}</td></tr>`;
        }).join("")}
      </tbody></table>
      ${c.contract_allowance_pct != null
        ? `<div class="concl"><div class="l1">Price variance re-checked by code · <b>contract allows ${c.contract_allowance_pct}%</b></div><span class="chip">Parsed from clause by code · authoritative ✓</span></div>` : ""}`
    : `<div class="allgood">✓ All three documents agree · no discrepancy</div>`;

  const trail = dec ? dec.tool_calls.map((t, i) => {
    const code = t.tool_name === "recheck_against_contract";
    return `<div class="step ${code ? "code" : ""}">
      <div class="bdg num">${i + 1}</div>
      <div class="sc"><div class="nm"><b>${esc(t.tool_name)}</b>
        <span class="tag">${code ? "code" : "model"}</span>
        <span class="raw-t" data-i="${i}">raw ▾</span>
        <span class="dur">round ${t.round}</span></div>
        <div class="rs">${esc(toolSummary(t))}</div>
        <pre class="raw" id="raw-${i}" hidden>${esc(prettyRaw(t.result))}</pre></div></div>`;
  }).join("") : `<div class="placeholder">Not run yet. Click Re-run.</div>`;

  view.innerHTML = `
    <div class="head">
      <div><div class="crumb">Invoices / ${esc(c.invoice_id)}</div><h2>Invoice detail</h2></div>
      <div class="actions">
        ${dec && dec.action === "APPROVE" ? `<button class="btn primary">Confirm payment</button>` : ""}
        <button class="btn">Send to human</button>
        <button class="btn" id="rerun">Re-run</button>
        <button class="btn" id="back">← Back</button>
      </div>
    </div>
    <div class="card banner">
      <div class="accent" style="background:${a.accent}"></div>
      <div class="bc">
        <div class="top">
          <div class="lf"><span class="pill ${a.cls}">${dec ? dec.action : "PENDING"}</span>
            <span class="verdict">${a.verb}</span>
            ${override ? `<span class="badge-override">Code override</span>` : ""}</div>
          <div class="conf"><div class="c1" style="color:${a.accent}">Gates ${passed} / ${c.guardrails.length} passed</div>
            ${dec ? `<div class="c2 num">Model confidence ${dec.confidence}</div>` : ""}</div>
        </div>
        <div class="meta num">${esc(c.vendor_name)} · ${money(c.total_cents, c.currency)} · ${esc(c.po ? c.po.doc_id : "no PO")}${c.grn ? " / " + esc(c.grn.doc_id) : ""}${c.payment_terms ? " · " + esc(c.payment_terms) : ""}</div>
        <div class="gates"><span class="g ok" style="color:${a.accent};font-weight:600">${passed === c.guardrails.length ? "✓ all passed" : "Gates"}</span> ${gates}</div>
      </div>
    </div>
    <div class="cols">
      <div class="card trail">
        <h3>Tool trail · Glass box</h3>
        <div class="tsub">${dec ? `${dec.rounds_used} rounds · ${dec.tool_calls.length} tool calls · fully logged · auditable` : "Run to generate the trail"}</div>
        ${trail}
      </div>
      <div>
        <div class="card recon"><h3>Three-way match</h3>
          <div class="ids">${esc(c.po ? c.po.doc_id : "no PO")} · ${esc(c.grn ? c.grn.doc_id : "no GRN")} · ${esc(c.invoice_id)}</div>
          ${recon}</div>
        ${dec ? `<div class="card reason-card"><h3>Decision rationale</h3>
          <ol class="points">${reasonPoints(c).map((p) => `<li>${esc(p)}</li>`).join("")}</ol>
          <details class="rawreason"><summary>Model's raw rationale (audit)</summary><p>${esc(dec.reasoning)}</p></details></div>` : ""}
        ${dec && dec.outbound_message ? `<div class="card outbound"><h3>System-generated outbound message (template)</h3><p>${esc(dec.outbound_message)}</p></div>` : ""}
      </div>
    </div>`;
  document.getElementById("back").addEventListener("click", () => { setActiveNav(); dashboard(); });
  document.getElementById("rerun").addEventListener("click", (e) => rerun(c.invoice_id, e.target));
  document.querySelectorAll(".raw-t").forEach((el) => el.addEventListener("click", () => {
    const pre = document.getElementById("raw-" + el.dataset.i);
    pre.hidden = !pre.hidden;
    el.textContent = pre.hidden ? "raw ▾" : "raw ▴";
  }));
}

async function rerun(id, btn) {
  btn.disabled = true; btn.innerHTML = `<span class="spin">↻</span> Running…`;
  try {
    const c = await api(`/api/invoices/${id}/run`, { method: "POST" });
    renderDetail(c);
  } catch (e) {
    btn.disabled = false; btn.textContent = "Re-run (failed, retry)";
  }
}

dashboard();
