"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

const PALETTE = ["#3b6ef5", "#FF7043", "#42A5F5", "#AB47BC", "#26A69A", "#EC407A",
  "#66BB6A", "#FFA726", "#8D6E63", "#789262", "#5C6BC0", "#26C6DA",
  "#9CCC65", "#90A4AE", "#E57373", "#BA68C8"];

let CAT_COLORS = {};
let CAT_LIST = [];
let IMPORT_RESP = null;
let IMPORT_FILE = null;
let TX_PAGE = 1;
// 仪表盘当前区间的状态：供「分类明细下钻」计算占比
let DASH = { qs: "", catExp: [], catInc: [], totalExp: 0, totalInc: 0 };
// 流水页当前选中的 id 集合（用于批量删除）
let TX_SELECTED = new Set();
// 流水页当前页的数据，供「行内编辑」读取原始字段
let TX_ITEMS = {};

async function apiFetch(url, opts = {}) {
  const r = await fetch(url, opts);
  // /api/auth/* 上的 401 不是「会话过期」，而是「当前密码不对」这类业务错误，
  // 不能顺手弹回登录页——否则用户只会看到自己莫名被踢出去，看不到真正的提示。
  if (r.status === 401 && !url.startsWith("/api/auth/")) {
    showLogin();
    throw new Error("未登录或会话已过期");
  }
  return r;
}
// 安全读取错误信息：响应体不一定是 JSON（例如网关返回纯文本时）
async function readErr(r) {
  const text = await r.text().catch(() => "");
  if (!text) return `请求失败（HTTP ${r.status}）`;
  try {
    const d = JSON.parse(text);
    return d.detail || d.message || text;
  } catch (e) {
    return text.slice(0, 300);
  }
}
async function getJSON(url) {
  const r = await apiFetch(url);
  if (!r.ok) throw new Error(await readErr(r));
  return r.json();
}
async function postJSON(url, body) {
  const r = await apiFetch(url, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(await readErr(r));
  return r.json();
}
function toast(msg) {
  const t = $("#toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._t); t._t = setTimeout(() => t.classList.remove("show"), 2200);
}
function fmt(n) {
  const s = Math.abs(n).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return (n < 0 ? "-" : "") + "¥" + s;
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// ---------------- 导航 ----------------
$$(".nav button").forEach(b => b.addEventListener("click", () => {
  $$(".nav button").forEach(x => x.classList.remove("active"));
  $$(".tab").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
  $("#tab-" + b.dataset.tab).classList.add("active");
  if (b.dataset.tab === "dash") loadDash();
  if (b.dataset.tab === "tx") loadTx();
  if (b.dataset.tab === "cat") loadCat();
}));

// ---------------- 分类数据 ----------------
async function loadCategories() {
  CAT_LIST = await getJSON("/api/categories");
  CAT_COLORS = {};
  CAT_LIST.forEach((c, i) => { CAT_COLORS[c.name] = c.color || PALETTE[i % PALETTE.length]; });
  // 填充流水分类下拉
  const sel = $("#tx-cat");
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部分类</option>' +
    CAT_LIST.map(c => `<option value="${esc(c.name)}">${esc(c.name)}</option>`).join("");
  sel.value = cur;
}

// ---------------- 仪表盘 ----------------
const dashRangeEl = $("#dash-range");
dashRangeEl.addEventListener("change", () => {
  const isCustom = dashRangeEl.value === "custom";
  $("#dash-custom").style.display = isCustom ? "inline-flex" : "none";
  if (!isCustom) loadDash();
});
$("#dash-apply").addEventListener("click", loadDash);
$("#dash-start").addEventListener("change", loadDash);
$("#dash-end").addEventListener("change", loadDash);

// 组装查询串：普通时间段 -> range=key；自定义 -> start=&end=
function dashQuery() {
  if (dashRangeEl.value === "custom") {
    const s = $("#dash-start").value, e = $("#dash-end").value;
    if (!s && !e) return null;
    return `start=${s}&end=${e}`;
  }
  return `range=${dashRangeEl.value}`;
}

async function loadDash() {
  const qs = dashQuery();
  if (qs === null) { toast("请选择自定义区间的起止日期"); return; }
  const [sum, catExp, catInc, trend, stacked] = await Promise.all([
    getJSON(`/api/summary?${qs}`),
    getJSON(`/api/by_category?${qs}&direction=expense`),
    getJSON(`/api/by_category?${qs}&direction=income`),
    getJSON("/api/trend?months=12"),
    getJSON("/api/category_trend?months=6&direction=expense"),
  ]);
  $("#dash-period").textContent = sum.period;

  $("#dash-stats").innerHTML = `
    <div class="stat income"><div class="k">收入</div><div class="v">${fmt(sum.income)}</div></div>
    <div class="stat expense"><div class="k">支出</div><div class="v">${fmt(sum.expense)}</div></div>
    <div class="stat"><div class="k">结余</div><div class="v">${fmt(sum.balance)}</div></div>
    <div class="stat"><div class="k">笔数</div><div class="v">${sum.count}</div></div>`;

  renderDonut({ box: "#pie", legend: "#pie-legend", data: catExp,
               title: "总支出", empty: "本期暂无支出数据" });
  renderDonut({ box: "#pie-income", legend: "#pie-income-legend", data: catInc,
               title: "总收入", empty: "本期暂无收入数据" });
  renderTrend(trend);
  renderRank($("#rank"), catExp);
  renderStacked($("#stacked"), $("#stacked-legend"), stacked);

  // 记下当前区间，供「分类明细下钻」算占比
  DASH = {
    qs,
    catExp, catInc,
    totalExp: catExp.reduce((a, b) => a + b.total, 0),
    totalInc: catInc.reduce((a, b) => a + b.total, 0),
  };
  bindCatDrill();
}

// 给饼图 / 排行 / 图例绑定「点击看某分类逐笔明细」
function bindCatDrill() {
  $$("#pie .slice, #pie-legend .leg-item, #rank .rank-row").forEach(el => {
    el.onclick = () => openCatDetail(el.dataset.cat, "expense");
  });
  $$("#pie-income .slice, #pie-income-legend .leg-item").forEach(el => {
    el.onclick = () => openCatDetail(el.dataset.cat, "income");
  });
}

// 打开某分类在当前区间内的逐笔明细弹窗
async function openCatDetail(name, direction) {
  if (!name) return;
  const box = $("#cat-modal");
  box.classList.add("show");
  $("#cat-modal-title").textContent = "分类明细：" + name;
  $("#cat-modal-sub").textContent = "加载中…";
  const tb = $("#cat-detail-table tbody");
  tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">加载中…</td></tr>`;
  try {
    const data = await getJSON(
      `/api/transactions?${DASH.qs}&category=${encodeURIComponent(name)}&direction=${direction}&page_size=500`
    );
    const catArr = direction === "expense" ? DASH.catExp : DASH.catInc;
    const catSum = (catArr.find(c => c.category === name) || {}).total || 0;
    const grand = direction === "expense" ? DASH.totalExp : DASH.totalInc;
    const grandPct = grand ? ((catSum / grand) * 100).toFixed(1) : "0.0";
    $("#cat-modal-sub").textContent =
      `当前区间共 ${data.total} 笔，合计 ${fmt(catSum)}（占${direction === "expense" ? "总支出" : "总收入"} ${grandPct}%）`;
    if (!data.items.length) {
      tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">该分类本区间暂无数据</td></tr>`;
      return;
    }
    tb.innerHTML = data.items.map(r => {
      const amt = Math.abs(r.amount);
      const pCat = catSum ? (amt / catSum * 100).toFixed(1) : "0.0";
      const pAll = grand ? (amt / grand * 100).toFixed(1) : "0.0";
      return `<tr>
        <td>${esc(r.date)}</td>
        <td>${esc(r.category)}${r.subcategory ? '<span class="muted"> / ' + esc(r.subcategory) + "</span>" : ""}</td>
        <td class="amt ${r.direction}">${fmt(r.amount)}</td>
        <td style="text-align:right">${pCat}%</td>
        <td style="text-align:right">${pAll}%</td>
        <td>${esc(r.note || r.counterparty || "")}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">加载失败：${esc(e.message)}</td></tr>`;
  }
}
$("#cat-modal-close").addEventListener("click", () => $("#cat-modal").classList.remove("show"));
$("#cat-modal").addEventListener("click", e => { if (e.target.id === "cat-modal") $("#cat-modal").classList.remove("show"); });

// 给一组分类分配互不重复的颜色：优先用分类自身颜色，冲突时取调色板里尚未使用的颜色
function colorList(data) {
  const used = new Set();
  return data.map((d, i) => {
    let c = CAT_COLORS[d.category];
    if (!c || used.has(c)) {
      let k = 0;
      do { c = PALETTE[(i + k) % PALETTE.length]; k++; } while (used.has(c) && k <= PALETTE.length);
    }
    used.add(c);
    return c;
  });
}
function polar(cx, cy, r, deg) {
  const a = (deg - 90) * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
// 圆环扇区路径（外弧 + 内弧回切）
function ringSlice(cx, cy, rO, rI, a0, a1) {
  const [ox0, oy0] = polar(cx, cy, rO, a0);
  const [ox1, oy1] = polar(cx, cy, rO, a1);
  const [ix1, iy1] = polar(cx, cy, rI, a1);
  const [ix0, iy0] = polar(cx, cy, rI, a0);
  const large = (a1 - a0) > 180 ? 1 : 0;
  return `M ${ox0.toFixed(2)} ${oy0.toFixed(2)} A ${rO} ${rO} 0 ${large} 1 ${ox1.toFixed(2)} ${oy1.toFixed(2)} `
       + `L ${ix1.toFixed(2)} ${iy1.toFixed(2)} A ${rI} ${rI} 0 ${large} 0 ${ix0.toFixed(2)} ${iy0.toFixed(2)} Z`;
}

// 环形图：扇区百分比标签 + 悬停放大 + 图例（占比 / 金额）
function renderDonut({ box, legend, data, title = "总计", empty = "本期暂无数据", centerValue = null }) {
  const boxEl = $(box), legEl = legend ? $(legend) : null;
  if (!boxEl) return;
  if (!data || !data.length) {
    boxEl.innerHTML = `<p class="muted" style="text-align:center;padding:24px 0">${empty}</p>`;
    if (legEl) legEl.innerHTML = "";
    return;
  }
  const colors = colorList(data);
  const total = data.reduce((a, b) => a + b.total, 0);
  const cx = 110, cy = 110, rO = 92, rI = 58;
  let ang = 0;
  const slices = data.map((d, i) => {
    const frac = total ? d.total / total : 0;
    const a0 = ang;
    let a1 = ang + frac * 360;
    if (a1 - a0 > 359.99) a1 = a0 + 359.99;   // 单分类占满时避免弧线退化
    ang = a1;
    const [lx, ly] = polar(cx, cy, (rO + rI) / 2, (a0 + a1) / 2);
    const label = frac >= 0.06
      ? `<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" text-anchor="middle"
               font-size="10" font-weight="600" fill="#fff" pointer-events="none">${(frac * 100).toFixed(0)}%</text>`
      : "";
    return `<g class="slice" data-cat="${esc(d.category)}" style="cursor:pointer"><path d="${ringSlice(cx, cy, rO, rI, a0, a1)}" fill="${colors[i]}">
        <title>${esc(d.category)}：${fmt(d.total)}（${d.count} 笔 · 点击看明细）</title></path>${label}</g>`;
  }).join("");
  boxEl.innerHTML = `<svg class="donut" viewBox="0 0 220 220" width="220" height="220">
    ${slices}
    <text x="110" y="105" text-anchor="middle" font-size="12" fill="#6b7686">${esc(title)}</text>
    <text x="110" y="126" text-anchor="middle" font-size="16" font-weight="700" fill="#1f2733">${fmt(centerValue != null ? centerValue : total)}</text>
  </svg>`;
  if (legEl) legEl.innerHTML = data.map((d, i) => {
    const pct = total ? ((d.total / total) * 100).toFixed(1) : "0.0";
    return `<span class="leg-item" data-cat="${esc(d.category)}" style="cursor:pointer"><i style="background:${colors[i]}"></i>${esc(d.category)}
      <b>${pct}%</b><em>${fmt(d.total)}</em></span>`;
  }).join("");
}

// 横向条形排行
function renderRank(el, data, topN = 10) {
  if (!el) return;
  if (!data || !data.length) {
    el.innerHTML = `<p class="muted" style="text-align:center;padding:24px 0">本期暂无数据</p>`;
    return;
  }
  const colors = colorList(data);
  const top = data.slice(0, topN);
  const max = Math.max(...top.map(d => d.total), 1);
  const total = data.reduce((a, b) => a + b.total, 0) || 1;
  el.innerHTML = top.map((d, i) => {
    const w = Math.max(2, (d.total / max) * 100);
    const pct = ((d.total / total) * 100).toFixed(1);
    return `<div class="rank-row" data-cat="${esc(d.category)}" style="cursor:pointer">
      <span class="rank-name" title="${esc(d.category)}">${esc(d.category)}</span>
      <span class="rank-track"><span class="rank-bar" style="width:${w}%;background:${colors[i]}"></span></span>
      <span class="rank-val">${fmt(d.total)}</span>
      <span class="rank-pct">${pct}%</span>
    </div>`;
  }).join("");
}

// 月度分类堆叠柱状图
function renderStacked(el, legEl, data) {
  if (!el) return;
  const labels = (data && data.labels) || [];
  const series = (data && data.series) || [];
  if (!labels.length || !series.length) {
    el.innerHTML = `<p class="muted" style="text-align:center;padding:24px 0">暂无数据</p>`;
    if (legEl) legEl.innerHTML = "";
    return;
  }
  const colors = colorList(series);
  const W = 520, H = 220, pl = 48, pr = 10, pt = 12, pb = 30;
  const pw = W - pl - pr, ph = H - pt - pb;
  const n = labels.length;
  const totals = labels.map((_, i) => series.reduce((a, s) => a + (s.values[i] || 0), 0));
  const max = Math.max(1, ...totals);
  const gw = pw / n;
  const bw = Math.min(38, gw * 0.62);
  let bars = "", xlab = "";
  labels.forEach((lab, i) => {
    const x = pl + i * gw + (gw - bw) / 2;
    let y = pt + ph;
    series.forEach((s, si) => {
      const v = s.values[i] || 0;
      if (v <= 0) return;
      const h = (v / max) * ph;
      y -= h;
      bars += `<rect x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${bw.toFixed(1)}" height="${h.toFixed(1)}"
                 fill="${colors[si]}"><title>${lab} ${esc(s.category)} ${fmt(v)}</title></rect>`;
    });
    xlab += `<text x="${(pl + i * gw + gw / 2).toFixed(1)}" y="${H - 10}" text-anchor="middle"
               font-size="9" fill="#6b7686">${lab.slice(2)}</text>`;
  });
  const yl = [0, 0.5, 1].map(f =>
    `<line x1="${pl}" y1="${(pt + ph - f * ph).toFixed(1)}" x2="${W - pr}" y2="${(pt + ph - f * ph).toFixed(1)}" stroke="#eef0f4"/>
     <text x="${pl - 6}" y="${(pt + ph - f * ph + 3).toFixed(1)}" text-anchor="end" font-size="9" fill="#9aa3b2">${fmt(max * f).replace(".00", "")}</text>`).join("");
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%">${yl}${bars}${xlab}</svg>`;
  if (legEl) legEl.innerHTML = series.map((s, si) =>
    `<span class="leg-item"><i style="background:${colors[si]}"></i>${esc(s.category)}</span>`).join("");
}

function renderTrend(t) {
  const box = $("#trend");
  const labels = t.labels, inc = t.income, exp = t.expense;
  const W = 520, H = 210, pl = 42, pb = 26, pt = 12, pr = 8;
  const pw = W - pl - pr, ph = H - pb - pt;
  const max = Math.max(1, ...inc, ...exp);
  const n = labels.length;
  const gw = pw / n;
  const bw = Math.min(14, gw / 2 - 2);
  let bars = "", xlab = "";
  inc.forEach((v, i) => {
    const x = pl + i * gw + gw / 2;
    const hE = (exp[i] / max) * ph, hI = (v / max) * ph;
    bars += `<rect x="${x - bw - 1}" y="${pt + ph - hE}" width="${bw}" height="${hE}" fill="#e57373">
      <title>${labels[i]} 支出 ${fmt(exp[i])}</title></rect>`;
    bars += `<rect x="${x + 1}" y="${pt + ph - hI}" width="${bw}" height="${hI}" fill="#43a047">
      <title>${labels[i]} 收入 ${fmt(v)}</title></rect>`;
    if (i % 2 === 0 || n <= 8)
      xlab += `<text x="${x}" y="${H - 8}" text-anchor="middle" font-size="9" fill="#6b7686">${labels[i].slice(2)}</text>`;
  });
  const yl = [0, 0.5, 1].map(f =>
    `<line x1="${pl}" y1="${pt + ph - f * ph}" x2="${W - pr}" y2="${pt + ph - f * ph}" stroke="#eef0f4"/>
     <text x="${pl - 5}" y="${pt + ph - f * ph + 3}" text-anchor="end" font-size="9" fill="#9aa3b2">${fmt(max * f).replace(".00", "")}</text>`).join("");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="100%">
    ${yl}${bars}${xlab}
    <rect x="${pl + 4}" y="${pt + 2}" width="10" height="8" fill="#43a047"/><text x="${pl + 17}" y="${pt + 9}" font-size="9" fill="#6b7686">收入</text>
    <rect x="${pl + 54}" y="${pt + 2}" width="10" height="8" fill="#e57373"/><text x="${pl + 67}" y="${pt + 9}" font-size="9" fill="#6b7686">支出</text>
  </svg>`;
}

// ---------------- 流水 ----------------
$("#tx-search").addEventListener("click", () => { TX_PAGE = 1; TX_SELECTED.clear(); loadTx(); });
$("#tx-prev").addEventListener("click", () => { if (TX_PAGE > 1) { TX_PAGE--; loadTx(); } });
$("#tx-next").addEventListener("click", () => { TX_PAGE++; loadTx(); });
$("#tx-q").addEventListener("keydown", e => { if (e.key === "Enter") { TX_PAGE = 1; TX_SELECTED.clear(); loadTx(); } });
// 全选本页
$("#tx-selall").addEventListener("change", () => {
  const checked = $("#tx-selall").checked;
  $$("#tx-table .tx-sel").forEach(cb => { cb.checked = checked; toggleSel(cb.dataset.id, checked); });
  updateSelUI();
});
// 批量删除
$("#tx-batch").addEventListener("click", async () => {
  const ids = [...TX_SELECTED];
  if (!ids.length) return;
  if (!confirm(`确认删除选中的 ${ids.length} 笔交易？此操作不可撤销。`)) return;
  try {
    const res = await postJSON("/api/transactions/batch_delete", { ids });
    toast(`已删除 ${res.deleted} 笔`);
    TX_SELECTED.clear();
    loadTx(); loadDash();
  } catch (e) { toast("删除失败：" + e.message); }
});

function toggleSel(id, on) {
  if (on) TX_SELECTED.add(id); else TX_SELECTED.delete(id);
}
function updateSelUI() {
  const n = TX_SELECTED.size;
  $("#tx-sel").textContent = n ? `已选 ${n} 笔` : "";
  $("#tx-batch").disabled = n === 0;
}

async function loadTx() {
  const range = $("#tx-range").value;
  const dir = $("#tx-dir").value;
  const cat = $("#tx-cat").value;
  const q = $("#tx-q").value.trim();
  const params = new URLSearchParams({ page: TX_PAGE, page_size: 50 });
  if (range) params.set("range", range);
  if (dir) params.set("direction", dir);
  if (cat) params.set("category", cat);
  if (q) params.set("q", q);
  const data = await getJSON("/api/transactions?" + params.toString());
  TX_ITEMS = {};
  data.items.forEach(r => { TX_ITEMS[r.id] = r; });
  const tb = $("#tx-table tbody");
  if (!data.items.length) {
    tb.innerHTML = `<tr><td colspan="9" class="muted" style="text-align:center;padding:24px">暂无数据，去「导入」页上传账单吧</td></tr>`;
  } else {
    tb.innerHTML = data.items.map(r => txRowHTML(r)).join("");
    // 行内单笔删除
    $$("#tx-table [data-del]").forEach(b => b.addEventListener("click", async () => {
      if (!confirm("确认删除该笔记录？")) return;
      await apiFetch("/api/transactions/" + b.dataset.del, { method: "DELETE" });
      TX_SELECTED.delete(b.dataset.del);
      toast("已删除"); loadTx(); loadDash();
    }));
    // 进入行内编辑
    $$("#tx-table [data-edit]").forEach(b => b.addEventListener("click", () => enterEditRow(b.dataset.edit)));
    // 行选择
    $$("#tx-table .tx-sel").forEach(cb => cb.addEventListener("change", () => {
      toggleSel(cb.dataset.id, cb.checked);
      updateSelUI();
      const all = $$("#tx-table .tx-sel");
      $("#tx-selall").checked = all.length > 0 && all.every(c => c.checked);
    }));
    updateSelUI();
  }
  const pages = Math.ceil(data.total / data.page_size) || 1;
  $("#tx-page").textContent = `第 ${data.page} / ${pages} 页（共 ${data.total} 笔）`;
  $("#tx-prev").disabled = TX_PAGE <= 1;
  $("#tx-next").disabled = TX_PAGE >= pages;
  // 应用「显示列」设置（每次重渲染都要重新套用）
  applyColVisibility();
  // 图表（当前筛选结果）
  loadTxChart(range, dir, cat, q);
}

// 流水行（普通展示态）
function txRowHTML(r) {
  return `<tr data-id="${r.id}">
    <td><input type="checkbox" class="tx-sel" data-id="${r.id}" ${TX_SELECTED.has(String(r.id)) ? "checked" : ""} /></td>
    <td data-col="date">${esc(r.date)}</td>
    <td data-col="category"><span class="tag">${esc(r.category)}${r.subcategory ? '<span class="muted"> / ' + esc(r.subcategory) + '</span>' : ''}</span></td>
    <td data-col="account">${esc(r.account)}</td>
    <td data-col="counterparty">${esc(r.counterparty)}</td>
    <td data-col="note" class="tx-note">${esc(r.note)}</td>
    <td data-col="amount" class="amt ${r.direction}">${fmt(r.amount)}</td>
    <td data-col="direction"><span class="pill ${r.direction}">${r.direction === "income" ? "收入" : "支出"}</span></td>
    <td class="tx-op" data-col="op">
      <button class="btn" data-edit="${r.id}">编辑</button>
      <button class="btn danger" data-del="${r.id}">删</button>
    </td>
  </tr>`;
}

// 进入行内编辑态：把该行替换成输入框
function enterEditRow(id) {
  const r = TX_ITEMS[id];
  if (!r) return;
  const tr = document.querySelector(`#tx-table tr[data-id="${id}"]`);
  if (!tr) return;
  const catOpts = CAT_LIST.map(c =>
    `<option value="${esc(c.name)}" ${c.name === r.category ? "selected" : ""}>${esc(c.name)}</option>`).join("");
  tr.innerHTML = `
    <td><input type="checkbox" disabled /></td>
    <td data-col="date"><input type="date" class="tx-edit-input" data-f="date" value="${esc(r.date)}" /></td>
    <td data-col="category">
      <select class="tx-edit-input" data-f="category">${catOpts}</select>
      <select class="tx-edit-input" data-f="subcategory" style="margin-top:4px"><option value="">（无二级分类）</option></select>
    </td>
    <td data-col="account"><input class="tx-edit-input" data-f="account" value="${esc(r.account)}" /></td>
    <td data-col="counterparty"><input class="tx-edit-input" data-f="counterparty" value="${esc(r.counterparty)}" /></td>
    <td data-col="note"><input class="tx-edit-input" data-f="note" value="${esc(r.note)}" /></td>
    <td data-col="amount"><input type="number" step="0.01" class="tx-edit-input" data-f="amount" value="${esc(r.amount)}" style="text-align:right;width:100px" /></td>
    <td data-col="direction"><select class="tx-edit-input" data-f="direction">
        <option value="expense" ${r.direction === "expense" ? "selected" : ""}>支出</option>
        <option value="income" ${r.direction === "income" ? "selected" : ""}>收入</option>
      </select></td>
    <td class="tx-op" data-col="op">
      <button class="btn" data-save="${id}">保存</button>
      <button class="btn ghost" data-cancel="${id}">取消</button>
    </td>`;
  // 二级分类下拉：随一级分类联动
  const catSel = tr.querySelector('[data-f="category"]');
  const subSel = tr.querySelector('[data-f="subcategory"]');
  catSel.addEventListener("change", () => loadSubOptions(subSel, catSel.value, ""));
  loadSubOptions(subSel, r.category, r.subcategory);
  tr.querySelector("[data-save]").addEventListener("click", () => saveEditRow(id));
  tr.querySelector("[data-cancel]").addEventListener("click", () => loadTx());
}

// 拉取并填充某一级分类下的二级分类下拉；cur 为当前已选中的二级分类
async function loadSubOptions(subSel, category, cur) {
  subSel.innerHTML = '<option value="">（无二级分类）</option>';
  if (!category) return;
  try {
    const list = await getJSON("/api/subcategories?category=" + encodeURIComponent(category));
    list.forEach(s => {
      const o = document.createElement("option");
      o.value = s; o.textContent = s;
      if (s === cur) o.selected = true;
      subSel.appendChild(o);
    });
  } catch (e) { /* 忽略：下拉留空即可 */ }
}

// 保存行内编辑：读取输入框 -> PUT
async function saveEditRow(id) {
  const tr = document.querySelector(`#tx-table tr[data-id="${id}"]`);
  if (!tr) return;
  const rec = {};
  tr.querySelectorAll(".tx-edit-input").forEach(inp => { rec[inp.dataset.f] = inp.value; });
  try {
    await apiFetch("/api/transactions/" + id, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rec),
    });
    toast("已保存");
    loadTx(); loadDash();
  } catch (e) { toast("保存失败：" + e.message); }
}

// 流水页图表：分类构成（环形图，右侧图例可点击下钻）
async function loadTxChart(range, dir, cat, q) {
  const params = new URLSearchParams();
  if (range) params.set("range", range);
  if (dir) params.set("direction", dir);
  if (cat) params.set("category", cat);
  if (q) params.set("q", q);
  try {
    const s = await getJSON("/api/tx_summary?" + params.toString());
    renderDonut({ box: "#tx-donut", legend: "#tx-donut-legend", data: s.by_category, title: "合计", empty: "当前筛选无数据" });
    $("#tx-chart-sum").textContent = `共 ${s.count} 笔 · 收入 ${fmt(s.income)} / 支出 ${fmt(s.expense)} · 结余 ${fmt(s.balance)}`;
    // 环形图扇区 / 图例 -> 点击看该分类在当前筛选下的逐笔明细
    $$("#tx-donut .slice, #tx-donut-legend .leg-item").forEach(el => {
      el.onclick = () => openTxCatDetail(el.dataset.cat);
    });
  } catch (e) {
    $("#tx-chart-sum").textContent = "图表加载失败：" + e.message;
  }
}

// 打开「流水页当前筛选条件下」某分类的逐笔明细弹窗
async function openTxCatDetail(name) {
  if (!name) return;
  const qs = txQuery();
  const box = $("#cat-modal");
  box.classList.add("show");
  $("#cat-modal-title").textContent = "分类明细：" + name;
  $("#cat-modal-sub").textContent = "加载中…";
  const tb = $("#cat-detail-table tbody");
  tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">加载中…</td></tr>`;
  try {
    const [sum, data] = await Promise.all([
      getJSON(`/api/tx_summary?${qs}`),
      getJSON(`/api/transactions?${qs}&category=${encodeURIComponent(name)}&page_size=500`),
    ]);
    const catSum = data.items.reduce((a, r) => a + Math.abs(r.amount), 0);
    const grand = (sum.by_category || []).reduce((a, r) => a + r.total, 0) || 0;
    const grandPct = grand ? ((catSum / grand) * 100).toFixed(1) : "0.0";
    $("#cat-modal-sub").textContent =
      `当前筛选共 ${data.total} 笔，合计 ${fmt(catSum)}（占总金额 ${grandPct}%）`;
    if (!data.items.length) {
      tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">该分类当前筛选下暂无数据</td></tr>`;
      return;
    }
    tb.innerHTML = data.items.map(r => {
      const amt = Math.abs(r.amount);
      const pCat = catSum ? (amt / catSum * 100).toFixed(1) : "0.0";
      const pAll = grand ? (amt / grand * 100).toFixed(1) : "0.0";
      return `<tr>
        <td>${esc(r.date)}</td>
        <td>${esc(r.category)}${r.subcategory ? '<span class="muted"> / ' + esc(r.subcategory) + "</span>" : ""}</td>
        <td class="amt ${r.direction}">${fmt(r.amount)}</td>
        <td style="text-align:right">${pCat}%</td>
        <td style="text-align:right">${pAll}%</td>
        <td>${esc(r.note || r.counterparty || "")}</td>
      </tr>`;
    }).join("");
  } catch (e) {
    tb.innerHTML = `<tr><td colspan="6" class="muted" style="text-align:center;padding:18px">加载失败：${esc(e.message)}</td></tr>`;
  }
}

// 组装流水页当前的筛选查询串
function txQuery() {
  const range = $("#tx-range").value, dir = $("#tx-dir").value,
        cat = $("#tx-cat").value, q = $("#tx-q").value.trim();
  const p = new URLSearchParams();
  if (range) p.set("range", range);
  if (dir) p.set("direction", dir);
  if (cat) p.set("category", cat);
  if (q) p.set("q", q);
  return p.toString();
}

// 流水表格列宽拖拽（拖动表头右边缘调整列宽，按列序记忆到 localStorage）
function enableColResize(tableSel, storeKey) {
  const table = $(tableSel);
  if (!table) return;
  const ths = Array.from(table.querySelectorAll("thead tr > th"));
  // 各列默认宽度（px）。第一列选择框固定 36，无需在此列出。
  const defaults = [null, 96, 110, 110, 170, 240, 120, 76, 96];
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(storeKey) || "null"); } catch (e) { saved = null; }
  ths.forEach((th, i) => {
    if (i === 0) return;   // 第一列（选择框）固定，不拖拽
    const w = (saved && saved[i] != null) ? saved[i] : defaults[i];
    if (w) th.style.width = w + "px";
    const handle = document.createElement("span");
    handle.className = "col-resizer";
    th.appendChild(handle);
    handle.addEventListener("mousedown", e => {
      e.preventDefault(); e.stopPropagation();
      const startX = e.clientX;
      const startW = th.getBoundingClientRect().width;
      const onMove = ev => {
        const nw = Math.max(48, startW + ev.clientX - startX);
        th.style.width = nw + "px";
      };
      const onUp = () => {
        document.removeEventListener("mousemove", onMove);
        document.removeEventListener("mouseup", onUp);
        const cur = ths.map(t => t.style.width.replace("px", "") || null);
        try { localStorage.setItem(storeKey, JSON.stringify(cur)); } catch (e) { /* ignore */ }
      };
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });
  });
}

// 流水表格「显示列」：按 data-col 切换列的显隐，记忆到 localStorage
const TX_HIDDEN_KEY = "tx-hidden-cols";
function applyColVisibility() {
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem(TX_HIDDEN_KEY) || "[]"); } catch (e) { hidden = []; }
  const set = new Set(hidden);
  document.querySelectorAll("#tx-table th[data-col]").forEach(th => {
    th.classList.toggle("col-hidden", set.has(th.dataset.col));
  });
  document.querySelectorAll("#tx-table td[data-col]").forEach(td => {
    td.classList.toggle("col-hidden", set.has(td.dataset.col));
  });
}
function initColVisibility() {
  const box = $("#tx-cols");
  if (!box) return;
  // 用本地存储恢复复选框状态
  let hidden = [];
  try { hidden = JSON.parse(localStorage.getItem(TX_HIDDEN_KEY) || "[]"); } catch (e) { hidden = []; }
  const set = new Set(hidden);
  $$("#tx-cols [data-col-toggle]").forEach(cb => { cb.checked = !set.has(cb.dataset.colToggle); });
  $$("#tx-cols [data-col-toggle]").forEach(cb => cb.addEventListener("change", () => {
    const cur = new Set(JSON.parse(localStorage.getItem(TX_HIDDEN_KEY) || "[]"));
    if (cb.checked) cur.delete(cb.dataset.colToggle); else cur.add(cb.dataset.colToggle);
    localStorage.setItem(TX_HIDDEN_KEY, JSON.stringify([...cur]));
    applyColVisibility();
  }));
}

// ---------------- 导入 ----------------
const dz = $("#drop"), fileInput = $("#file");
dz.addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => { if (fileInput.files[0]) uploadPreview(fileInput.files[0], ""); });
["dragover", "dragenter"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.add("drag"); }));
["dragleave", "drop"].forEach(ev => dz.addEventListener(ev, e => { e.preventDefault(); dz.classList.remove("drag"); }));
dz.addEventListener("drop", e => { const f = e.dataTransfer.files[0]; if (f) uploadPreview(f, ""); });

async function uploadPreview(file, preset) {
  IMPORT_FILE = file;
  const fd = new FormData();
  fd.append("file", file);
  if (preset) fd.append("preset", preset);
  toast("正在解析文件…");
  try {
    const r = await apiFetch("/api/import/preview", { method: "POST", body: fd });
    if (!r.ok) throw new Error(await readErr(r));
    IMPORT_RESP = await r.json();
    renderMapping();
    $("#import-step2").style.display = "block";
  } catch (e) { toast("解析失败：" + e.message); }
}

function renderMapping() {
  const r = IMPORT_RESP;
  $("#import-info").textContent = `共 ${r.total_rows} 行 · 文件：${r.filename}`;
  // 服务端判定「读到的是残缺内容」时，必须显眼提示，不能让用户以为文件本身没数据
  const warn = $("#import-warn");
  if (warn) {
    if (r.warning) { warn.textContent = "⚠️ " + r.warning; warn.style.display = "block"; }
    else { warn.textContent = ""; warn.style.display = "none"; }
  }
  const headers = r.headers;
  const opts = h => `<option value="">— 不导入 —</option>` +
    headers.map(h2 => `<option value="${esc(h2)}">${esc(h2)}</option>`).join("");
  $("#map-ui").innerHTML = `<table class="map-table"><thead><tr>
      <th>标准字段</th><th>对应来源列</th></tr></thead><tbody>` +
    r.fields.map(f => `<tr><td><b>${r.field_labels[f]}</b><br><span class="muted" style="font-size:12px">${f}</span></td>
      <td><select data-field="${f}">${opts(r.mapping[f])}</select></td></tr>`).join("") +
    `</tbody></table>`;
  // 回填建议映射
  $$("#map-ui select").forEach(s => { const f = s.dataset.field; if (r.mapping[f]) s.value = r.mapping[f]; });

  const head = "<tr>" + headers.map(h => `<th>${esc(h)}</th>`).join("") + "</tr>";
  const body = r.sample.map(row => "<tr>" + headers.map(h => `<td>${esc(row[h])}</td>`).join("") + "</tr>").join("");
  $("#preview-table").innerHTML = head + body;
}

$$("#import-step2 [data-preset]").forEach(b => b.addEventListener("click", () => {
  if (IMPORT_FILE) uploadPreview(IMPORT_FILE, b.dataset.preset);
}));

$("#import-apply").addEventListener("click", async () => {
  const mapping = {};
  $$("#map-ui select").forEach(s => { if (s.value) mapping[s.dataset.field] = s.value; });
  if (!mapping.date || !mapping.amount) { toast("请至少映射「日期」和「金额」两列"); return; }
  const btn = $("#import-apply"); btn.disabled = true;
  const out = $("#import-result");
  try {
    const res = await postJSON("/api/import/apply", {
      token: IMPORT_RESP.token, mapping, source: $("#import-source").value.trim() || "导入",
    });
    const why = res.reason_text ? `跳过原因：${res.reason_text}` : "";
    if (res.inserted === 0) {
      out.textContent = `没有导入任何记录（共 ${res.total} 行）。${why} `
        + `请检查「日期」是否映射到了真正的日期列。`;
      toast("未导入任何记录，请检查列映射");
    } else {
      out.textContent = `成功导入 ${res.inserted} 笔`
        + (res.skipped ? `，跳过 ${res.skipped} 行（${why}）` : "。");
      toast(`导入完成！共 ${res.inserted} 笔`);
    }
    loadDash();
  } catch (e) {
    out.textContent = "";
    toast("导入失败：" + e.message);
  }
  finally { btn.disabled = false; }
});

// ---------------- 分类 ----------------
async function loadCat() {
  const list = $("#cat-list");
  list.innerHTML = CAT_LIST.map(c => `
    <div class="cat-item">
      <span style="font-size:20px">${c.icon || "●"}</span>
      <div style="flex:1">
        <b>${esc(c.name)}</b> <span class="pill ${c.type}">${c.type === "income" ? "收入" : c.type === "expense" ? "支出" : "收支"}</span>
        <div class="chips" style="margin-top:4px">${(c.keywords || []).map(k => `<span class="chip">${esc(k)}</span>`).join("") || '<span class="muted">无关键词</span>'}</div>
      </div>
      <button class="btn danger" data-cat="${esc(c.name)}">删除</button>
    </div>`).join("");
  $$("#cat-list [data-cat]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("删除分类「" + b.dataset.cat + "」？已归类交易不会变。")) return;
    await apiFetch("/api/categories/" + encodeURIComponent(b.dataset.cat), { method: "DELETE" });
    await loadCategories(); loadCat(); toast("已删除");
  }));
}

$("#cat-add").addEventListener("click", async () => {
  const name = $("#cat-name").value.trim();
  if (!name) { toast("请填写分类名称"); return; }
  const kws = $("#cat-keywords").value.split(/[,，]/).map(s => s.trim()).filter(Boolean);
  await postJSON("/api/categories", {
    name, type: $("#cat-type").value, keywords: kws,
    icon: $("#cat-icon").value || "🏷️", color: $("#cat-color").value || "#3b6ef5",
  });
  $("#cat-name").value = ""; $("#cat-keywords").value = "";
  await loadCategories(); loadCat(); toast("分类已添加");
});

// ---------------- 智能问答 ----------------
async function askQA(text) {
  if (!text.trim()) return;
  const res = await postJSON("/api/query", { text });
  const box = $("#qa-result");
  box.style.display = "block";
  const bd = res.breakdown || [];
  const items = res.items || [];
  const itemsTotal = res.items_total || 0;
  const scoped = !!res.scoped;
  const focusCat = res.focus_category || res.category || "";
  const dirWord = res.metric === "income" ? "收入" : "支出";

  let html = `<div style="font-size:16px;font-weight:600">${esc(res.answer)}</div>
    <div class="muted" style="margin-top:6px">区间：${esc(res.period)} · 指标：${esc(res.metric)} · 收入 ${fmt(res.income)} / 支出 ${fmt(res.expense)}</div>`;

  if (bd.length) {
    // 指定了分类时，标题直接点明「该分类占总体比例」；否则是各分类构成
    const head = scoped
      ? `「${esc(focusCat)}」占${esc(res.period)}总${dirWord}的 ${res.focus_share}%（总${dirWord} ${fmt(res.focus_total)}）`
      : "分类构成（各分类占比）";
    html += `<div class="qa-chart"><h4>${head}</h4><div id="qa-donut"></div><div class="legend" id="qa-donut-legend"></div></div>`;
  }

  if (items.length) {
    const catName = res.category ? `（分类：${esc(res.category)}）` : "";
    html += `<div class="qa-detail"><h4>对应交易明细 ${catName}</h4>
      <div class="muted">共命中 ${itemsTotal} 笔，按金额从大到小显示前 ${items.length} 笔</div>
      <div style="max-height:320px;overflow:auto;margin-top:6px">
      <table class="map-table"><thead><tr>
        <th>日期</th><th>分类</th><th style="text-align:right">金额</th><th>备注 / 商户</th>
      </tr></thead><tbody>${items.map(r => `<tr>
        <td>${esc(r.date)}</td>
        <td>${esc(r.category)}${r.subcategory ? '<span class="muted"> / ' + esc(r.subcategory) + '</span>' : ''}</td>
        <td class="amt ${r.direction}">${fmt(r.amount)}</td>
        <td>${esc(r.note || r.counterparty || "")}</td>
      </tr>`).join("")}</tbody></table></div></div>`;
  } else if (scoped || res.category) {
    html += `<div class="qa-detail"><h4>对应交易明细</h4><div class="muted">该区间内没有命中的交易。</div></div>`;
  }

  box.innerHTML = html;
  if (bd.length) {
    renderDonut({
      box: "#qa-donut", legend: "#qa-donut-legend", data: bd,
      title: scoped ? focusCat : "合计",
      centerValue: scoped && bd[0] ? bd[0].total : null,
      empty: "无数据",
    });
  }
}
$("#qa-ask").addEventListener("click", () => askQA($("#qa-text").value));
$("#qa-text").addEventListener("keydown", e => { if (e.key === "Enter") askQA($("#qa-text").value); });
$$("#tab-qa .chip").forEach(c => c.addEventListener("click", () => { $("#qa-text").value = c.dataset.q; askQA(c.dataset.q); }));

// ---------------- 登录 / 会话 ----------------
let booted = false;
let CURRENT_USER = "";

function setWhoami(name) {
  CURRENT_USER = name || "";
  const el = $("#whoami");
  if (el) el.textContent = CURRENT_USER ? "👤 " + CURRENT_USER : "";
}

function showLogin() {
  $("#login-overlay").classList.remove("hidden");
  $("#userbox").style.display = "none";
  const errEl = $("#login-err");
  if (errEl) errEl.textContent = "";
  // 只回填账户名，不回填密码：账户名不是秘密，密码留在浏览器本地风险太大。
  // 会话本身靠 HttpOnly Cookie 维持 30 天，本来也不需要把密码存下来。
  try {
    const u = localStorage.getItem("ledger_user");
    if (u) $("#login-user").value = u;
  } catch (e) {}
  const uEl = $("#login-user");
  (uEl.value ? $("#login-pw") : uEl).focus();
}
function hideLogin() {
  $("#login-overlay").classList.add("hidden");
  $("#userbox").style.display = "";
  // 登录成功后立刻清掉输入框里的密码：它只是被隐藏了，DOM 里还留着明文
  $("#login-pw").value = "";
}
function boot() {
  if (booted) return;
  booted = true;
  loadCategories();
  enableColResize("#tx-table", "tx-col-widths");
  initColVisibility();
  loadDash();
}

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("#login-user").value.trim();
  const pw = $("#login-pw").value;
  const errEl = $("#login-err");
  errEl.textContent = "";
  if (!username) { errEl.textContent = "请输入账户名（默认 admin）"; $("#login-user").focus(); return; }
  if (!pw) { errEl.textContent = "请输入密码"; $("#login-pw").focus(); return; }
  const btn = $("#login-btn"); btn.disabled = true;
  try {
    const r = await fetch("/api/login", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: username, password: pw }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      errEl.textContent = d.detail || "登录失败";
      return;
    }
    const d = await r.json().catch(() => ({}));
    try { localStorage.setItem("ledger_user", username); } catch (e2) {}
    setWhoami(d.username || username);
    hideLogin();
    boot();
  } catch (err) {
    errEl.textContent = "网络错误，请重试";
  } finally {
    btn.disabled = false;
  }
});

$("#btn-logout").addEventListener("click", async () => {
  await apiFetch("/api/logout", { method: "POST" }).catch(() => {});
  booted = false;
  $("#login-pw").value = "";        // 退出时清掉输入框里的密码，别留在屏幕上
  showLogin();
});

// ---------------- 账户设置弹窗 ----------------
function openAccount(focus) {
  const box = $("#acct-modal");
  box.classList.add("show");
  $("#acct-err").textContent = "";
  // 从登录页进来时用户还没登录，此时「当前密码」是必填项，提示语要说清楚
  $("#acct-user").value = CURRENT_USER || $("#login-user").value.trim() || "";
  if (focus === "pw") { $("#acct-cur").focus(); } else { $("#acct-user").focus(); }
}
function closeAccount() {
  $("#acct-modal").classList.remove("show");
  $("#acct-cur").value = ""; $("#acct-new").value = ""; $("#acct-new2").value = "";
  $("#acct-err").textContent = "";
}
$("#btn-account").addEventListener("click", () => openAccount("user"));
$("#acct-close").addEventListener("click", closeAccount);
// 点遮罩空白处关闭
$("#acct-modal").addEventListener("click", (e) => { if (e.target.id === "acct-modal") closeAccount(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && $("#acct-modal").classList.contains("show")) closeAccount();
});

$("#acct-save-user").addEventListener("click", async () => {
  const newName = $("#acct-user").value.trim();
  const errEl = $("#acct-err");
  errEl.textContent = "";
  if (!newName) { errEl.textContent = "账户名不能为空"; return; }
  try {
    const res = await postJSON("/api/auth/set_username", { current: $("#acct-cur").value, new: newName });
    setWhoami(res.username || newName);
    try { localStorage.setItem("ledger_user", res.username || newName); } catch (e2) {}
    closeAccount();
    toast("账户名已改为 " + (res.username || newName) + "，下次登录请用它");
  } catch (e) {
    errEl.textContent = e.message;
  }
});

$("#acct-save-pw").addEventListener("click", async () => {
  const nw = $("#acct-new").value, nw2 = $("#acct-new2").value;
  const errEl = $("#acct-err");
  errEl.textContent = "";
  if (nw.length < 4) { errEl.textContent = "新密码至少 4 位"; return; }
  if (nw !== nw2) { errEl.textContent = "两次输入的新密码不一致"; return; }
  try {
    await postJSON("/api/auth/set_password", { current: $("#acct-cur").value, new: nw });
    closeAccount();
    toast("密码已修改（其它设备需重新登录）");
  } catch (e) {
    errEl.textContent = e.message;
  }
});

// 登录页「Change password?」直接打开弹窗的改密码部分
// （部分登录模板里没有这个入口，做了空判断避免加载即报错中断整个脚本）
const _lf = $("#login-forgot");
if (_lf) _lf.addEventListener("click", (e) => { e.preventDefault(); openAccount("pw"); });
// 登录页「Create」：单账户系统没有注册流程，指向同一处账户设置
const _lc = $("#login-create");
if (_lc) _lc.addEventListener("click", (e) => { e.preventDefault(); openAccount("user"); });

// ---------------- 服务端版本 ----------------
// 显示容器里实际运行的代码版本：排查「本机已修好、NAS 还是旧镜像」时最关键的一行信息
async function loadBuildInfo() {
  const el = $("#build-info");
  if (!el) return;
  try {
    const h = await getJSON("/api/health");
    el.textContent = `服务端 v${h.version}`;
  } catch (e) {
    el.textContent = "服务端版本获取失败";
  }
}

// ---------------- 启动 ----------------
(async function init() {
  loadBuildInfo();
  try {
    const me = await getJSON("/api/me");
    if (me.authenticated) { setWhoami(me.username); hideLogin(); boot(); return; }
  } catch (e) { /* 401 走下方登录页 */ }
  showLogin();
})();
