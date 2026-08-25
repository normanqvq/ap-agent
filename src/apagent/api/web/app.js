"use strict";
const view = document.getElementById("view");
// 401 anywhere means the session is gone — drop to the sign-in screen.
const api = (p, o) => fetch(p, o).then((r) => {
  if (r.status === 401) { showLogin(); throw new Error("401"); }
  if (!r.ok) throw new Error(r.status);
  return r.json();
});
// Escapes quotes too — attacker-authored strings (invoice ids, vendor
// names) land inside double-quoted attributes like data-id="...".
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// Currency comes from attacker-authored invoices — escape it like any
// other supplier string before it reaches innerHTML.
const money = (c, cur) => c == null ? "—" : `${esc(cur || "SGD")} ${(c / 100).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

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

// --- auth ------------------------------------------------------------------
let loginLayer = null;
function showLogin() {
  if (!loginLayer) {
    loginLayer = document.createElement("div");
    loginLayer.className = "login-layer";
    document.body.appendChild(loginLayer);
  }
  loginLayer.innerHTML = `
    <div class="login-card">
      <img src="/logo.svg" width="46" height="46" alt="" />
      <h2>AP Agent</h2>
      <p>Sign in to the review console</p>
      <input id="login-name" placeholder="Your name" maxlength="40" autocomplete="name" />
      <button class="btn primary" id="login-btn">Sign in</button>
      <div class="login-err" id="login-err"></div>
      <small>Demo sign-in — no password. Sessions are in-memory and cleared on restart.</small>
    </div>`;
  const input = loginLayer.querySelector("#login-name");
  const err = loginLayer.querySelector("#login-err");
  const go = async () => {
    const name = input.value.trim();
    if (!name) { input.focus(); return; }
    let r;
    try {
      r = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
    } catch {
      err.textContent = "Server unreachable — is uvicorn running?";
      return;
    }
    if (!r.ok) {
      err.textContent = `Sign-in failed (${r.status})`;
      return;
    }
    const me = await r.json();
    // remove(), not hidden: the layer's own display rule would override
    // the hidden attribute and leave it covering the app.
    loginLayer.remove();
    loginLayer = null;
    renderUser(me.name);
    setActiveNav();
    dashboard();
  };
  loginLayer.querySelector("#login-btn").addEventListener("click", go);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  input.focus();
}

function renderUser(name) {
  const el = document.getElementById("user");
  el.innerHTML = `<div class="av">${esc(name[0].toUpperCase())}</div>
    <div class="ud"><b>${esc(name)}</b><small>AP reviewer</small></div>
    <button class="iconbtn" id="logout" title="Sign out">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>
    </button>`;
  document.getElementById("logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    el.innerHTML = "";
    showLogin();
  });
}

// --- email composer + toast ------------------------------------------------
function toast(msg) {
  const t = document.createElement("div");
  t.className = "toast";
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

// The body is read-only on purpose: outbound text is rendered by code from
// a fixed template — the model (and the reviewer, here) cannot author it.
function composer({ to, subject, body, sendLabel, onSend }) {
  const layer = document.createElement("div");
  layer.className = "modal-layer";
  layer.innerHTML = `
    <div class="compose">
      <div class="compose-h"><h3>New message</h3><button class="iconbtn dark" data-x>✕</button></div>
      <div class="compose-f"><label>To</label><input value="${esc(to)}" readonly /></div>
      <div class="compose-f"><label>Subject</label><input value="${esc(subject)}" readonly /></div>
      <textarea readonly rows="9">${esc(body)}</textarea>
      <div class="compose-note">Rendered by code from a fixed template — the model cannot author outbound messages.</div>
      <div class="compose-a"><button class="btn" data-x>Cancel</button>
        <button class="btn primary" data-send>${sendLabel || "Send"}</button></div>
    </div>`;
  document.body.appendChild(layer);
  const close = () => layer.remove();
  layer.querySelectorAll("[data-x]").forEach((b) => b.addEventListener("click", close));
  layer.addEventListener("click", (e) => { if (e.target === layer) close(); });
  layer.querySelector("[data-send]").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await onSend(); close(); }
    catch { e.target.disabled = false; e.target.textContent = "Failed — retry"; }
  });
}

// --- navigation ------------------------------------------------------------
const VIEWS = { dashboard, invoices, payments, outbox, analytics, settings };
// A deferred "land on another page after the badge shows" timer. Any manual
// navigation cancels it, so a user who clicks away is never yanked back.
let pendingNav = null;
function cancelPendingNav() { if (pendingNav) { clearTimeout(pendingNav); pendingNav = null; } }
function navSoon(view, fn, ms) {
  cancelPendingNav();
  pendingNav = setTimeout(() => {
    pendingNav = null;
    setActiveNav(document.querySelector(`.nav a[data-view="${view}"]`));
    fn();
  }, ms);
}
document.querySelectorAll(".nav a[data-view]").forEach((a) =>
  a.addEventListener("click", () => { cancelPendingNav(); setActiveNav(a); (VIEWS[a.dataset.view] || dashboard)(); }));
function setActiveNav(el) {
  document.querySelectorAll(".nav a").forEach((a) => a.classList.remove("active"));
  (el || document.querySelector(".nav a")).classList.add("active");
}

// --- invoice rows (shared by Overview queue and the Invoices page) ----------
const invoiceRow = (x) => {
  const a = ACT[x.action] || ACT.null;
  return `<div class="row" data-id="${esc(x.invoice_id)}">
    <div class="rl"><b>${esc(x.invoice_id)}</b><small>${esc(x.vendor_name)}</small></div>
    <div class="rr"><span class="amt num">${money(x.total_cents, x.currency)}</span>
      ${x.human_review === "confirmed" ? `<span class="badge-human ok">Paid ✓</span>` : ""}
      ${x.human_review === "sent_to_human" ? `<span class="badge-human mid">With reviewer</span>` : ""}
      <span class="pill ${a.cls}">${x.action || "PENDING"}</span>
      <span class="reason t-${a.cls}">${esc(x.reason || "")}</span></div>
  </div>`;
};
const needsHumanFn = (x) => !x.action || (x.action !== "APPROVE" && !x.human_review);

// --- invoices (full list with filters) --------------------------------------
async function invoices() {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const list = await api("/api/invoices");
  const FILTERS = [
    ["all", "All", () => true],
    ["needs", "Needs human", needsHumanFn],
    ["approved", "Approved", (x) => x.action === "APPROVE"],
    ["paid", "Paid", (x) => x.human_review === "confirmed"],
  ];
  let active = "all";
  const render = () => {
    const filter = FILTERS.find(([k]) => k === active)[2];
    const rows = list.filter(filter);
    view.innerHTML = `
      <div class="head">
        <div><h1>Invoices</h1><div class="sub">${list.length} invoices · click a row for the full case</div></div>
      </div>
      <div class="chips">${FILTERS.map(([k, label, fn]) =>
        `<button class="chip ${k === active ? "on" : ""}" data-f="${k}">${label} (${list.filter(fn).length})</button>`).join("")}</div>
      <div class="card">${rows.map(invoiceRow).join("") || `<div class="placeholder">Nothing matches this filter.</div>`}</div>`;
    view.querySelectorAll(".chip").forEach((b) =>
      b.addEventListener("click", () => { active = b.dataset.f; render(); }));
    view.querySelectorAll(".row").forEach((r) =>
      r.addEventListener("click", () => detail(r.dataset.id)));
  };
  render();
}

// --- dashboard -------------------------------------------------------------
async function dashboard() {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const [m, list, r] = await Promise.all([api("/api/metrics"), api("/api/invoices"), api("/api/roi")]);
  const d = m.distribution;
  const maxN = Math.max(1, ...Object.values(d));
  const bar = (label, n, color) =>
    `<div class="bar"><span class="bl">${label}</span><span class="bk" style="width:${Math.max(8, n / maxN * 190)}px;background:${color}"></span><span class="bc num">${n}</span></div>`;
  const decided = list.filter((x) => x.action);
  // The reviewer's worklist: undecided invoices first, then decided ones
  // that still wait on a human (HOLD / ESCALATE / EMAIL, not yet routed).
  const needsHuman = list.filter(
    (x) => !x.action || (x.action !== "APPROVE" && !x.human_review)
  );
  view.innerHTML = `
    <div class="head">
      <div><h1>Overview</h1><div class="sub">${m.total} invoices this week · ${needsHuman.length} awaiting a human</div></div>
      <div class="actions">
        <button class="btn" id="upload">Upload invoice</button>
        <input type="file" id="upfile" accept="application/pdf" hidden />
        <button class="btn primary" id="run"><span class="play"></span>Review next (${needsHuman.length})</button>
      </div>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="l">STP rate</div><div class="v num">${m.stp_pct}%</div><div class="s">APPROVE / ${m.total} invoices</div></div>
      <div class="card kpi"><div class="l">Touchless rate</div><div class="v num">${m.touchless_pct}%</div><div class="s">APPROVE+HOLD / ${m.total} invoices</div></div>
      <div class="card kpi ${m.false_approve ? "warn" : "good"}"><div class="l">False approvals</div><div class="v num">${m.false_approve}</div><div class="s">${m.false_approve ? "Zero-tolerance · TARGET BROKEN" : "Zero-tolerance · on target"}</div></div>
      <div class="card kpi warn"><div class="l">Pending</div><div class="v num">${m.pending}</div><div class="s">Awaiting review</div></div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-h"><h3>Invoice queue</h3><a id="viewall">View all →</a></div>
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
    </div>
    <div class="card">
      <div class="card-h"><h3>What it saves</h3><span class="runtotal">manual cost: Ardent Partners 2025</span></div>
      <div class="perfgrid">
        <div class="perf"><div class="pl">Manual cost</div><div class="pv num">$${(r.manual_cost_cents / 100).toFixed(2)}</div><div class="ps">per invoice · $${(r.manual_batch_cents / 100).toFixed(2)} for ${r.invoices}</div></div>
        <div class="perf"><div class="pl">Agent cost</div><div class="pv num">${r.agent_cost_measured ? "~" + r.agent_avg_tokens.toLocaleString("en") + " tok" : "—"}</div><div class="ps">${r.agent_cost_measured ? "avg tokens / run" : "run precompute to measure"}</div></div>
        <div class="perf"><div class="pl">Rework avoided</div><div class="pv num">${r.rework_low_pct}–${r.rework_high_pct}%</div><div class="ps">manual rework rate · false approvals: ${r.false_approve}</div></div>
      </div>
    </div>`;
  const q = document.getElementById("queue");
  q.innerHTML = list.slice(0, 8).map(invoiceRow).join("");
  q.querySelectorAll(".row").forEach((r) => r.addEventListener("click", () => detail(r.dataset.id)));
  document.getElementById("viewall").addEventListener("click", () => {
    setActiveNav(document.querySelector('.nav a[data-view="invoices"]'));
    invoices();
  });
  document.getElementById("run").addEventListener("click", () => {
    if (!needsHuman.length) { toast("Queue clear — nothing awaiting review"); return; }
    detail(needsHuman[0].invoice_id);
  });
  const upBtn = document.getElementById("upload");
  const upFile = document.getElementById("upfile");
  upBtn.addEventListener("click", () => upFile.click());
  upFile.addEventListener("change", async () => {
    const f = upFile.files[0];
    if (!f) return;
    upBtn.disabled = true;
    upBtn.textContent = "Extracting… (LLM reading the PDF)";
    const fd = new FormData();
    fd.append("file", f);
    try {
      const r = await fetch("/api/invoices/upload", { method: "POST", body: fd });
      if (r.status === 401) { showLogin(); return; }
      const j = await r.json();
      if (!r.ok) { toast(j.detail || "Upload failed"); return; }
      toast(`Extracted ${j.invoice_id} — decision: ${j.decision.action}`);
      detail(j.invoice_id);
    } catch {
      toast("Upload failed — network error");
    } finally {
      upBtn.disabled = false;
      upBtn.textContent = "Upload invoice";
      upFile.value = "";
    }
  });
}

// --- payments --------------------------------------------------------------
// Plain-English gloss for each chat policy. Wording only — which one is in
// force, and what it does, is decided server-side.
const POLICY_EN = {
  OFF: "chat confirmations are not proof of delivery at all",
  EVIDENCE_ONLY: "always shown to a reviewer; never releases payment on its own",
  TIERED: "releases payment when an authorised receiver confirms it, under the ceiling",
  TRUSTED: "an authorised receiver's word releases payment at any amount",
};

const HOLD_EN = {
  AWAITING_GRN: "No goods receipt",
  AWAITING_DELIVERY: "Short delivery",
  PRICE_VARIANCE: "Price variance",
};

// Cents in different currencies are never added together — totals arrive
// as {currency: cents} and render side by side.
const fmtTotals = (t) => Object.entries(t).map(([c, n]) => money(n, c)).join(" · ") || "—";
const plural = (n) => (n === 1 ? "" : "s");

async function payments(highlightId) {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const plan = await api("/api/schedule");
  const s = plan.summary;
  const next = plan.runs[0];
  const runCard = (r) => `
    <div class="card run">
      <div class="card-h"><h3>Pay run · ${r.run_date}</h3>
        <span class="runtotal num">${r.invoice_count} invoice${plural(r.invoice_count)} · ${fmtTotals(r.totals)}</span></div>
      ${r.payments.map((p) => `
        <div class="row payrow">
          <div class="rl"><b>${esc(p.vendor_name)}</b><small>${p.invoices.length} invoice${plural(p.invoices.length)} · one ${esc(p.currency)} transfer</small></div>
          <div class="rr"><span class="amt num">${money(p.total_cents, p.currency)}</span></div>
        </div>
        ${p.invoices.map((i) => `
          <div class="subrow ${i.invoice_id === highlightId ? "flash" : ""}" data-id="${esc(i.invoice_id)}">
            <span><b>${esc(i.invoice_id)}</b> · ${i.due_date ? "due " + esc(i.due_date) : "no due date"}${i.late ? ` <span class="late-tag">late — past due</span>` : ""}${i.confirmed ? ` <span class="badge-human ok">Paid ✓</span>` : ""}</span>
            <span class="num">${money(i.total_cents, i.currency)}</span>
          </div>`).join("")}`).join("")}
    </div>`;

  view.innerHTML = `
    <div class="head">
      <div><h1>Payments</h1><div class="sub">Weekly pay runs, every Friday · pay late but never late · planned as of ${esc(plan.as_of)}</div></div>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="l">Scheduled</div><div class="v num">${s.scheduled_count}</div><div class="s">${fmtTotals(s.scheduled_totals)}</div></div>
      <div class="card kpi"><div class="l">Next run</div><div class="v num">${next ? next.run_date.slice(5) : "—"}</div><div class="s">${next ? `${next.invoice_count} invoice${plural(next.invoice_count)} · ${fmtTotals(next.totals)}` : "nothing due"}</div></div>
      <div class="card kpi ${s.late_count ? "warn" : "good"}"><div class="l">Past due</div><div class="v num">${s.late_count}</div><div class="s">released in the next run</div></div>
      <div class="card kpi"><div class="l">Withheld</div><div class="v num">${s.not_scheduled_count}</div><div class="s">${fmtTotals(s.not_scheduled_totals)} — money stays put</div></div>
    </div>
    ${plan.payment_record.length ? `
    <div class="card run">
      <div class="card-h"><h3>Payment record</h3><span class="runtotal">confirmed this session · cleared on restart</span></div>
      ${plan.payment_record.map((r) => `
        <div class="row" data-id="${esc(r.invoice_id)}">
          <div class="rl"><b>${esc(r.invoice_id)}</b><small>${esc(r.vendor_name)}</small></div>
          <div class="rr"><span class="amt num">${money(r.total_cents, r.currency)}</span>
            <span class="mailmeta num">by ${esc(r.confirmed_by)} · ${esc(r.confirmed_at.slice(11, 16))}</span>
            ${r.voided ? `<span class="badge-human mid">Voided · re-decided</span>` : `<span class="badge-human ok">Paid ✓</span>`}</div>
        </div>`).join("")}
    </div>` : ""}
    ${plan.runs.map(runCard).join("")}
    <div class="card">
      <div class="card-h"><h3>Not scheduled</h3><span class="runtotal">only APPROVE moves money</span></div>
      ${plan.not_scheduled.map((n) => {
        const a = ACT[n.action] || ACT.null;
        return `<div class="row" data-id="${esc(n.invoice_id)}">
          <div class="rl"><b>${esc(n.invoice_id)}</b><small>${esc(n.vendor_name)}</small></div>
          <div class="rr"><span class="amt num">${money(n.total_cents, n.currency)}</span>
            ${n.human_review === "sent_to_human" ? `<span class="badge-human mid">With reviewer</span>` : ""}
            <span class="pill ${a.cls}">${n.action || "PENDING"}</span>
            <span class="reason t-${a.cls}">${esc(HOLD_EN[n.hold_reason] || "")}</span></div>
        </div>`;
      }).join("")}
    </div>`;
  view.querySelectorAll("[data-id]").forEach((el) =>
    el.addEventListener("click", () => detail(el.dataset.id)));
}

// --- outbox ----------------------------------------------------------------
async function outbox() {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const list = await api("/api/outbox");
  view.innerHTML = `
    <div class="head">
      <div><h1>Outbox</h1><div class="sub">Messages the system sent this session — recorded here, not delivered (demo build, no SMTP). Bodies are code-templated; nobody types them.</div></div>
    </div>
    <div class="card">${list.length ? list.map((m) => `
      <details class="mail">
        <summary>
          <span class="pill ${m.kind === "vendor_query" ? "appr" : "hold"}">${m.kind === "vendor_query" ? "VENDOR" : "INTERNAL"}</span>
          <b>${esc(m.subject)}</b>
          <span class="mailmeta num">to ${esc(m.to)} · by ${esc(m.sent_by)} · ${esc(m.sent_at.slice(11, 16))}</span>
        </summary>
        <pre>${esc(m.body)}</pre>
      </details>`).join("") : `<div class="placeholder">Nothing sent yet — use Send to human or the vendor query on an invoice.</div>`}</div>`;
}

// --- analytics -------------------------------------------------------------
const DEFECT_EN = {
  price_variance: "Price variance beyond tolerance",
  price_variance_within_contract: "Price within contract allowance",
  missing_grn: "No goods receipt",
  prompt_injection: "Prompt injection",
  partial_delivery: "Partial delivery billed in full",
  duplicate: "Duplicate invoice",
  missing_po_ref: "Missing PO reference",
};
const VERDICT_EN = {
  pass: { cls: "ok", text: "✓ handled correctly" },
  false_approve: { cls: "bad", text: "✗ FALSE APPROVE" },
  friction: { cls: "mid", text: "friction — safe" },
  missing: { cls: "mid", text: "not run" },
};

// The six agent-performance metrics from the training deck, each measured
// from the decided runs — not asserted.
function perfPanel(p) {
  if (!p) return "";
  const cell = (label, value, sub) =>
    `<div class="perf"><div class="pl">${label}</div><div class="pv num">${value}</div><div class="ps">${sub}</div></div>`;
  const tokens = p.avg_tokens_per_run != null
    ? [`${p.avg_tokens_per_run.toLocaleString("en")}`, `avg tokens / run · ${p.token_runs_measured} measured`]
    : ["—", "captured on live runs"];
  return `
    <div class="card">
      <div class="card-h"><h3>Agent performance</h3><span class="runtotal">six metrics, measured over ${p.schema_pass.total} runs</span></div>
      <div class="perfgrid">
        ${cell("Schema-valid output", `${p.schema_pass.ok}/${p.schema_pass.total}`, "decisions that parsed")}
        ${cell("Tool-call success", p.tool_success_pct != null ? p.tool_success_pct + "%" : "—", `${p.tool_calls} calls served`)}
        ${cell("Task completion", p.completion_pct + "%", `${p.hit_cap} hit the round cap`)}
        ${cell("Token cost / run", tokens[0], tokens[1])}
        ${cell("Loop discipline", p.avg_rounds, `avg rounds · cap ${p.max_rounds}`)}
        ${cell("Answer fidelity", `${p.defects_handled}/${p.defects_total}`, `defects handled · ${p.false_approve} false approvals`)}
      </div>
    </div>`;
}

// Rules-only vs the agent, both scored by the same harness. The point: the
// agent lifts STP without lifting risk — false approvals stay zero on both.
function abPanel(b) {
  if (!b) return "";
  const stat = (label, value, sub) =>
    `<div class="perf"><div class="pl">${label}</div><div class="pv num">${value}</div><div class="ps">${sub}</div></div>`;
  const recovered = b.recovered.length
    ? `<div class="ab-rec"><div class="ab-rec-h">Recovered by the agent's judgement</div>${b.recovered.map((r) =>
        `<div class="row" data-id="${esc(r.invoice_id)}"><div class="rl"><b>${esc(r.invoice_id)}</b><small>${esc(r.vendor_name)}</small></div>
          <div class="rr"><span class="pill mid">rules-only: ${r.baseline_action}${r.baseline_reason && r.baseline_reason !== "—" ? " · " + esc(r.baseline_reason) : ""}</span><span class="pill ok">agent: APPROVE</span></div></div>`).join("")}</div>`
    : "";
  return `
    <div class="card">
      <div class="card-h"><h3>Agent vs rules-only</h3><span class="runtotal">same invoices, same eval harness</span></div>
      <div class="perfgrid">
        ${stat("STP rate", `${b.baseline.stp_pct}% → ${b.agent.stp_pct}%`, "rules-only → agent")}
        ${stat("False approvals", `${b.baseline.false_approve_count} · ${b.agent.false_approve_count}`, "zero on both — no added risk")}
        ${stat("Recovered", b.recovered.length, "held by rules, approved by the agent")}
      </div>
      ${recovered}
    </div>`;
}

async function analytics() {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const [a, b] = await Promise.all([api("/api/analytics"), api("/api/baseline")]);
  const m = a.metrics, d = a.distribution;
  const maxN = Math.max(1, ...Object.values(d));
  const bar = (label, n, color) =>
    `<div class="bar"><span class="bl">${label}</span><span class="bk" style="width:${Math.max(8, n / maxN * 190)}px;background:${color}"></span><span class="bc num">${n}</span></div>`;

  view.innerHTML = `
    <div class="head">
      <div><h1>Analytics</h1><div class="sub">Every number on this page is measured by the eval harness against the manifest ground truth — not asserted.</div></div>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="l">STP rate</div><div class="v num">${m.stp_pct}%</div><div class="s">APPROVE / ${m.total} invoices</div></div>
      <div class="card kpi"><div class="l">Touchless rate</div><div class="v num">${m.touchless_pct}%</div><div class="s">APPROVE + HOLD / ${m.total}</div></div>
      <div class="card kpi ${m.false_approve_count ? "warn" : "good"}"><div class="l">False approvals</div><div class="v num">${m.false_approve_count}</div><div class="s">${m.false_approve_count ? "Zero-tolerance · TARGET BROKEN" : "every planted defect blocked"}</div></div>
      <div class="card kpi"><div class="l">Friction</div><div class="v num">${m.friction_count}</div><div class="s">payable but held — safe, costs STP</div></div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-h"><h3>Planted-defect scorecard</h3><span class="runtotal">ground truth: manifest.json</span></div>
        ${a.defects.map((c) => {
          const act = ACT[c.action] || ACT.null;
          const v = VERDICT_EN[c.verdict] || VERDICT_EN.missing;
          return `<div class="row" data-id="${esc(c.invoice_id)}">
            <div class="rl"><b>${DEFECT_EN[c.defect] || esc(c.defect.replace(/_/g, " "))}</b><small>${esc(c.invoice_id)}</small></div>
            <div class="rr"><span class="pill ${act.cls}">${c.action || "PENDING"}</span>
              <span class="verdict-chip ${v.cls}">${v.text}</span></div>
          </div>`;
        }).join("")}
        <div class="row"><div class="rl"><b>Clean invoices (control group)</b><small>${a.clean_total} invoices with nothing planted</small></div>
          <div class="rr"><span class="verdict-chip ok">✓ ${a.clean_approved} approved straight through</span>
            ${a.clean_friction ? `<span class="verdict-chip mid">${a.clean_friction} friction</span>` : ""}</div></div>
      </div>
      <div class="card dist">
        <h3>Decision mix</h3>
        ${bar("APPROVE", d.APPROVE, "var(--green)")}
        ${bar("HOLD", d.HOLD, "var(--amber)")}
        ${bar("ESCALATE", d.ESCALATE, "var(--red)")}
        ${bar("EMAIL", d.EMAIL, "var(--accent)")}
        <div class="cap num">${m.decided} / ${m.total} ground-truth invoices decided${a.unexpected.length ? ` · +${a.unexpected.length} uploaded this session (no ground truth, unscored)` : ""}</div>
      </div>
    </div>
    ${perfPanel(a.performance)}
    ${abPanel(b)}
    <div class="card">
      <div class="card-h"><h3>By vendor</h3><span class="runtotal">billed vs approved for payment</span></div>
      ${a.vendors.map((v) => `
        <div class="row payrow">
          <div class="rl"><b>${esc(v.vendor_name)}</b><small>${esc(v.vendor_id)} · ${v.invoice_count} invoice${plural(v.invoice_count)}, ${v.approved_count} approved</small></div>
          <div class="rr"><span class="amt num">${fmtTotals(v.approved_totals)}</span>
            <span class="reason num" style="width:auto">of ${fmtTotals(v.billed_totals)}</span></div>
        </div>`).join("")}
    </div>`;
  view.querySelectorAll("[data-id]").forEach((el) =>
    el.addEventListener("click", () => detail(el.dataset.id)));
}

// --- settings --------------------------------------------------------------
async function settings() {
  view.innerHTML = `<div class="placeholder">Loading…</div>`;
  const c = await api("/api/config");
  const t = c.tolerances;
  const setting = (label, value, note) => `
    <div class="row payrow">
      <div class="rl"><b>${label}</b><small>${note}</small></div>
      <div class="rr"><span class="amt num">${value}</span><span class="code-chip">code</span></div>
    </div>`;

  view.innerHTML = `
    <div class="head">
      <div><h1>Settings</h1><div class="sub">Read-only. Every limit lives in version-controlled code — changing one is a reviewed commit, not a click.</div></div>
    </div>
    <div class="card">
      <div class="card-h"><h3>Decision policy</h3><span class="runtotal">enforced by the six code gates, never by the prompt</span></div>
      ${setting("Unit-price tolerance", `±${t.unit_price_pct}%`, "vs the PO price; a vendor contract can widen it (below)")}
      ${setting("Invoice-total tolerance", `${(t.total_abs_cents / 100).toFixed(2)} and ${t.total_pct}%`, "in the invoice's own currency; a total must sit inside both bounds")}
      ${setting("Quantity", t.qty_exact ? "exact match" : "tolerance", "quantity gaps are never noise — they mean goods did not arrive")}
      ${setting("Manual-review threshold", (t.manual_review_threshold_cents / 100).toLocaleString("en", { minimumFractionDigits: 2 }), "in the invoice's own currency; at or above it a human signs off, even on a clean match")}
      ${setting("Informal-receipt ceiling", (t.informal_grn_ceiling_cents / 100).toLocaleString("en", { minimumFractionDigits: 2 }), "the most we pay on a delivery confirmed in chat rather than recorded in the system")}
    </div>
    <div class="card">
      <div class="card-h"><h3>Chat-confirmed deliveries</h3><span class="runtotal">how much a colleague's word is worth here</span></div>
      <div class="policy">
        ${c.chat_grn.options.map((o) => `
          <div class="opt ${o === c.chat_grn.policy ? "on" : ""}">
            <b>${esc(o)}</b><span>${esc(POLICY_EN[o] || "")}</span>
          </div>`).join("")}
      </div>
      <div class="policy-note">Set in code and overridable per vendor, like every limit on this page.
        Two things no setting touches: whether the confirmed quantities cover what is billed
        (that is arithmetic, not policy), and the manual-review threshold above
        (a promise about large payments, not about proof of delivery).</div>
    </div>
    <div class="card">
      <div class="card-h"><h3>Contract allowances</h3><span class="runtotal">parsed from the contract text by code — the % never passes through the model</span></div>
      ${c.contract_allowances.map((v) => `
        <div class="row payrow">
          <div class="rl"><b>${esc(v.vendor_name)}</b><small>${v.source ? esc(v.source) : "no price-variance clause on file"}</small></div>
          <div class="rr"><span class="amt num">${v.allowance_pct != null ? "+" + v.allowance_pct + "%" : `default ±${t.unit_price_pct}%`}</span></div>
        </div>`).join("")}
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-h"><h3>Agent runtime</h3></div>
        ${setting("LLM provider", esc(c.provider), "judgement and extraction; swap with LLM_PROVIDER, the guardrails do not move")}
        <div class="row payrow">
          <div class="rl"><b>Actions</b><small>a four-value enum — the model cannot invent a fifth</small></div>
          <div class="rr">${c.actions.map((x) => `<span class="pill ${(ACT[x] || ACT.null).cls}">${x}</span>`).join(" ")}</div>
        </div>
      </div>
      <div class="card">
        <div class="card-h"><h3>Payment runs</h3></div>
        ${setting("Run day", c.schedule.run_day, "one batch per week, one transfer per vendor per currency")}
        ${setting("Planned as of", c.schedule.as_of, "fixed demo date so the plan is deterministic and offline")}
      </div>
    </div>`;
}

// --- detail ----------------------------------------------------------------
async function detail(id) {
  cancelPendingNav();  // a row click cancels any pending post-action navigation
  setActiveNav(document.querySelectorAll(".nav a")[1]);
  view.innerHTML = `<div class="placeholder">Loading ${esc(id)}…</div>`;
  const c = await api(`/api/invoices/${id}`);
  renderDetail(c);
}

// The conversation behind a chat-confirmed delivery.
//
// Every judgement shown here was made server-side (service._chat_grn_view):
// whether the speaker was allowed to confirm, which ordered lines went
// unconfirmed, which policy applied. This function only lays it out —
// CLAUDE.md keeps business logic out of the frontend, and "may this person
// release money" is the most business-logic question in the app.
//
// The messages are the one genuinely attacker-authored thing on the page,
// written by whoever is in that group. Every one goes through esc().
function chatEvidenceCard(c) {
  const ev = c.chat_grn;
  if (!ev) return "";
  const who = ev.authorised
    ? `<span class="chip ok">✓ ${esc(ev.confirmed_by)} · authorised receiver</span>`
    : `<span class="chip warn">⚠ not on the receiver list</span>`;
  const endorsed = ev.endorsed_by
    ? `<span class="chip ok">✓ accepted by ${esc(ev.endorsed_by)}</span>`
    : "";
  const said = ev.messages.length
    ? ev.messages.map((m) =>
        `<div class="said"><div class="said-h"><b>${esc(m.sender_name)}</b>
          <span class="num">${esc(m.sent_at.replace("T", " "))}</span></div>
          <div class="said-t">${esc(m.text)}</div></div>`).join("")
    : `<div class="said-none">The conversation is not in this session's buffer.</div>`;
  const read = ev.lines.map((l) =>
    `<li><span class="num">${l.qty}</span> × ${esc(l.sku || l.description)}</li>`).join("");
  const pending = ev.unconfirmed.length
    ? `<div class="pending"><b>Not confirmed:</b> ${ev.unconfirmed.map(esc).join(", ")}
        <div class="pending-n">Ordered but not mentioned as received, so the invoice
        still holds on those lines.</div></div>`
    : "";
  return `<div class="card chatev ${ev.authorised ? "" : "unauth"}">
      <h3>Delivery confirmed in chat</h3>
      <div class="chips">${who}${endorsed}
        <span class="chip">${esc(ev.receipt_id)}</span>
        <span class="chip">policy: ${esc(ev.policy)}</span></div>
      <div class="saidwrap">${said}</div>
      <div class="readas"><b>Recorded as received</b><ul>${read}</ul></div>
      ${pending}
      <p class="evnote">Chat text is third-party data. It is evidence for you to
      judge — the quantities above were matched to the purchase order in code,
      and no message can approve an invoice by itself.</p>
    </div>`;
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
        ${dec && dec.action === "APPROVE"
          ? (c.human_review === "confirmed"
            ? `<button class="btn primary" disabled>✓ Payment confirmed</button>`
            : `<button class="btn primary" id="confirm">Confirm payment</button>`)
          : ""}
        ${c.chat_grn && !c.chat_grn.endorsed_by && !(dec && dec.action === "APPROVE")
          ? `<button class="btn primary" id="accept-chat">Accept the chat confirmation</button>`
          : ""}
        ${c.human_review === "sent_to_human"
          ? `<button class="btn" disabled>✓ Sent to human</button>`
          : `<button class="btn" id="send">Send to human</button>`}
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
            ${override ? `<span class="badge-override">Code override</span>` : ""}
            ${c.human_review === "confirmed" ? `<span class="badge-human ok">✓ Confirmed by reviewer</span>` : ""}
            ${c.human_review === "sent_to_human" ? `<span class="badge-human mid">With human reviewer</span>` : ""}</div>
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
        ${chatEvidenceCard(c)}
        ${dec && dec.outbound_message ? `<div class="card outbound"><h3>System-generated outbound message (template)</h3><p>${esc(dec.outbound_message)}</p><button class="btn" id="email-vendor">Open in email composer</button></div>` : ""}
      </div>
    </div>`;
  document.getElementById("back").addEventListener("click", () => { cancelPendingNav(); setActiveNav(); dashboard(); });
  document.getElementById("rerun").addEventListener("click", (e) => rerun(c.invoice_id, e.target));
  // Accepting the confirmation re-runs the agent, so the answer comes back
  // from the pipeline rather than from this click. If code still refuses —
  // a short delivery, say — the invoice stays held and the button says so.
  const acceptBtn = document.getElementById("accept-chat");
  if (acceptBtn) acceptBtn.addEventListener("click", async (e) => {
    e.target.disabled = true;
    e.target.textContent = "Accepting…";
    try {
      const fresh = await api(`/api/invoices/${c.invoice_id}/accept-chat-grn`, { method: "POST" });
      renderDetail(fresh);
      toast(fresh.decision && fresh.decision.action === "APPROVE"
        ? `Confirmation accepted — ${c.invoice_id} released`
        : `Confirmation accepted — still held by code`);
    } catch { e.target.disabled = false; e.target.textContent = "Could not accept (retry)"; }
  });
  const confirmBtn = document.getElementById("confirm");
  if (confirmBtn) confirmBtn.addEventListener("click", async (e) => {
    e.target.disabled = true;
    try {
      renderDetail(await api(`/api/invoices/${c.invoice_id}/confirm`, { method: "POST" }));
      toast(`Payment confirmed — ${c.invoice_id}`);
      // A confirmed payment's natural next question is "when does it go
      // out?" — land on the pay-run plan with this invoice highlighted.
      navSoon('payments', () => payments(c.invoice_id), 800);
    } catch { e.target.disabled = false; e.target.textContent = "Refused by code (retry)"; }
  });
  // Both sends land on the Outbox — the visible answer to "where did it go".
  const goOutbox = () => navSoon('outbox', outbox, 700);
  const sendBtn = document.getElementById("send");
  if (sendBtn) sendBtn.addEventListener("click", () => composer({
    ...c.handoff_draft,
    onSend: async () => {
      renderDetail(await api(`/api/invoices/${c.invoice_id}/send-to-human`, { method: "POST" }));
      toast(`Sent to human reviewer — ${c.invoice_id}`);
      goOutbox();
    },
  }));
  const vendorBtn = document.getElementById("email-vendor");
  if (vendorBtn) vendorBtn.addEventListener("click", () => composer({
    to: c.outbound_to || "operations",
    subject: c.outbound_to && c.outbound_to.startsWith("billing@") ? `Query on invoice ${c.invoice_id}` : `[${c.invoice_id}] Action needed`,
    body: dec.outbound_message,
    onSend: async () => {
      renderDetail(await api(`/api/invoices/${c.invoice_id}/send-message`, { method: "POST" }));
      toast("Recorded in Outbox — demo build, no SMTP");
      goOutbox();
    },
  }));
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

// --- boot: restore the session or ask for a sign-in -------------------------
(async () => {
  const r = await fetch("/api/me");
  if (r.ok) {
    renderUser((await r.json()).name);
    dashboard();
  } else {
    showLogin();
  }
})();
