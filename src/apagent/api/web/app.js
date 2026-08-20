"use strict";
const view = document.getElementById("view");
const api = (p, o) => fetch(p, o).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const money = (c, cur) => c == null ? "—" : `${cur || "SGD"} ${(c / 100).toLocaleString("en", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const ACT = {
  APPROVE: { cls: "appr", zh: "付款", verb: "建议付款", accent: "var(--green)" },
  HOLD:    { cls: "hold", zh: "押单", verb: "建议挂起", accent: "var(--amber)" },
  ESCALATE:{ cls: "esc",  zh: "上报", verb: "转人工上报", accent: "var(--red)" },
  EMAIL:   { cls: "hold", zh: "问询", verb: "去函询问", accent: "var(--amber)" },
  null:    { cls: "idle", zh: "待处理", verb: "待处理", accent: "var(--muted)" },
};
const FIELD_ZH = { UNIT_PRICE: "单价", QTY: "数量", INVOICE_TOTAL: "发票总额", UOM: "单位", LINE_TOTAL: "行金额" };
const isMoney = (f) => ["UNIT_PRICE", "INVOICE_TOTAL", "LINE_TOTAL"].includes(f);

// --- navigation ------------------------------------------------------------
document.querySelectorAll(".nav a[data-view]").forEach((a) =>
  a.addEventListener("click", () => { setActiveNav(a); dashboard(); }));
function setActiveNav(el) {
  document.querySelectorAll(".nav a").forEach((a) => a.classList.remove("active"));
  (el || document.querySelector(".nav a")).classList.add("active");
}

// --- dashboard -------------------------------------------------------------
async function dashboard() {
  view.innerHTML = `<div class="placeholder">加载中…</div>`;
  const [m, list] = await Promise.all([api("/api/metrics"), api("/api/invoices")]);
  const d = m.distribution;
  const maxN = Math.max(1, ...Object.values(d));
  const bar = (label, n, color) =>
    `<div class="bar"><span class="bl">${label}</span><span class="bk" style="width:${Math.max(8, n / maxN * 190)}px;background:${color}"></span><span class="bc num">${n}</span></div>`;
  const decided = list.filter((x) => x.action);
  view.innerHTML = `
    <div class="head">
      <div><h1>概览</h1><div class="sub">本周处理 ${m.total} 张发票 · ${m.pending} 张待确认</div></div>
      <button class="btn primary" id="run"><span class="play"></span>运行核对 (${m.pending || m.total})</button>
    </div>
    <div class="kpis">
      <div class="card kpi"><div class="l">直通率 STP</div><div class="v num">${m.stp_pct}%</div><div class="s">APPROVE / 已判 ${m.decided}</div></div>
      <div class="card kpi"><div class="l">自动决策率</div><div class="v num">${m.touchless_pct}%</div><div class="s">APPROVE+HOLD / 已判</div></div>
      <div class="card kpi good"><div class="l">错误批准</div><div class="v num">${m.false_approve}</div><div class="s">零容忍 · 达标</div></div>
      <div class="card kpi warn"><div class="l">待处理</div><div class="v num">${m.pending}</div><div class="s">待人工确认</div></div>
    </div>
    <div class="grid">
      <div class="card">
        <div class="card-h"><h3>发票队列</h3><a>查看全部 →</a></div>
        <div id="queue"></div>
      </div>
      <div class="card dist">
        <h3>本周决策分布</h3>
        ${bar("APPROVE", d.APPROVE, "var(--green)")}
        ${bar("HOLD", d.HOLD, "var(--amber)")}
        ${bar("ESCALATE", d.ESCALATE, "var(--red)")}
        ${bar("EMAIL", d.EMAIL, "var(--accent)")}
        <div class="cap num">共 ${m.total} 张 · 错误批准 ${m.false_approve}</div>
      </div>
    </div>
    <div class="card feed">
      <div class="card-h"><h3>最近代理决策</h3></div>
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
        <span class="pill ${a.cls}">${x.action || "待处理"}</span>
        <span class="reason t-${a.cls}">${esc(x.reason || "")}</span></div>
    </div>`;
  }).join("");
  q.querySelectorAll(".row").forEach((r) => r.addEventListener("click", () => detail(r.dataset.id)));
  document.getElementById("run").addEventListener("click", () => detail((list.find((x) => !x.action) || list[0]).invoice_id));
}

// --- detail ----------------------------------------------------------------
async function detail(id) {
  setActiveNav(document.querySelectorAll(".nav a")[1]);
  view.innerHTML = `<div class="placeholder">加载 ${esc(id)}…</div>`;
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

  const priceRows = c.match.discrepancies;
  const recon = priceRows.length
    ? `<table><thead><tr><th></th><th>字段</th><th>PO</th><th>GRN</th><th>发票</th><th>Δ</th></tr></thead><tbody>
        ${priceRows.map((r) => {
          const f = (v) => v == null ? "—" : isMoney(r.field) ? money(parseInt(v), c.currency) : v;
          return `<tr><td class="lbl">行 ${r.line_pair ? r.line_pair[1] : "-"}</td>
            <td>${FIELD_ZH[r.field] || r.field}</td><td class="num">${f(r.po_value)}</td>
            <td class="num">${f(r.grn_value)}</td><td class="num var">${f(r.invoice_value)}</td>
            <td class="num var">${r.delta_pct != null ? "+" + r.delta_pct.toFixed(1) + "%" : "—"}</td></tr>`;
        }).join("")}
      </tbody></table>
      ${c.contract_allowance_pct != null
        ? `<div class="concl"><div class="l1">价格差异经代码复核，<b>合同容差 ${c.contract_allowance_pct}%</b></div><span class="chip">代码解析条款 · 结论权威 ✓</span></div>` : ""}`
    : `<div class="allgood">✓ 三方一致，无差异</div>`;

  const trail = dec ? dec.tool_calls.map((t, i) => {
    const code = t.tool_name === "recheck_against_contract";
    let res = t.result || "";
    if (res.length > 190) res = res.slice(0, 190) + "…";
    return `<div class="step ${code ? "code" : ""}">
      <div class="bdg num">${i + 1}</div>
      <div class="sc"><div class="nm"><b>${esc(t.tool_name)}</b>
        <span class="tag">${code ? "代码执行" : "模型"}</span><span class="dur">第 ${t.round} 轮</span></div>
        <div class="rs">${esc(res)}</div></div></div>`;
  }).join("") : `<div class="placeholder">尚未运行。点击「运行核对」。</div>`;

  view.innerHTML = `
    <div class="head">
      <div><div class="crumb">发票队列 / ${esc(c.invoice_id)}</div><h2>发票详情</h2></div>
      <div class="actions">
        ${dec && dec.action === "APPROVE" ? `<button class="btn primary">确认付款</button>` : ""}
        <button class="btn">转人工复核</button>
        <button class="btn" id="rerun">重新运行</button>
        <button class="btn" id="back">← 返回</button>
      </div>
    </div>
    <div class="card banner">
      <div class="accent" style="background:${a.accent}"></div>
      <div class="bc">
        <div class="top">
          <div class="lf"><span class="pill ${a.cls}">${dec ? dec.action : "待处理"}</span>
            <span class="verdict">${a.verb}</span>
            ${override ? `<span class="badge-override">代码改判</span>` : ""}</div>
          <div class="conf"><div class="c1" style="color:${a.accent}">守门 ${passed} / ${c.guardrails.length} 通过</div>
            ${dec ? `<div class="c2 num">模型置信度 ${dec.confidence}</div>` : ""}</div>
        </div>
        <div class="meta num">${esc(c.vendor_name)} · ${money(c.total_cents, c.currency)} · ${esc(c.po ? c.po.doc_id : "无 PO")}${c.grn ? " / " + esc(c.grn.doc_id) : ""}${c.payment_terms ? " · " + esc(c.payment_terms) : ""}</div>
        <div class="gates"><span class="g ok" style="color:${a.accent};font-weight:600">${passed === c.guardrails.length ? "✓ 全部通过" : "守门"}</span> ${gates}</div>
      </div>
    </div>
    <div class="cols">
      <div class="card trail">
        <h3>工具轨迹 · 玻璃盒子</h3>
        <div class="tsub">${dec ? `${dec.rounds_used} 轮 · ${dec.tool_calls.length} 次工具调用 · 全程留痕 · 可审计` : "点击运行以生成轨迹"}</div>
        ${trail}
      </div>
      <div>
        <div class="card recon"><h3>三方核对</h3>
          <div class="ids">${esc(c.po ? c.po.doc_id : "无 PO")} · ${esc(c.grn ? c.grn.doc_id : "无 GRN")} · ${esc(c.invoice_id)}</div>
          ${recon}</div>
        ${dec ? `<div class="card reason-card"><h3>判决理由</h3><p>${esc(dec.reasoning)}</p></div>` : ""}
        ${dec && dec.outbound_message ? `<div class="card outbound"><h3>系统生成的对外消息（模板）</h3><p>${esc(dec.outbound_message)}</p></div>` : ""}
      </div>
    </div>`;
  document.getElementById("back").addEventListener("click", () => { setActiveNav(); dashboard(); });
  document.getElementById("rerun").addEventListener("click", (e) => rerun(c.invoice_id, e.target));
}

async function rerun(id, btn) {
  btn.disabled = true; btn.innerHTML = `<span class="spin">↻</span> 运行中…`;
  try {
    const c = await api(`/api/invoices/${id}/run`, { method: "POST" });
    renderDetail(c);
  } catch (e) {
    btn.disabled = false; btn.textContent = "重新运行（失败，重试）";
  }
}

dashboard();
