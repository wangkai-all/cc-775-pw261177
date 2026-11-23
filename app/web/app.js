/* 实验板空间效应分析前端：原生 JS + 手绘 SVG，无 CDN、无构建步骤。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));
const tooltip = $("#tooltip");
const popover = $("#well-popover");

// 颜色全部走 CSS 变量，浅色/深色主题切换时自动使用各自已校验色阶
const cssVar = (name) =>
  getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const treatColor = (t) =>
  cssVar(`--treat-${String(t).toLowerCase()}`) || cssVar("--treat-ctrl");
const SEQ = 7;
function seqColor(i) { return cssVar(`--seq-${i + 1}`); }

const state = {
  plates: [],
  analyses: [],
  currentId: null,
  current: null,
  viewPlate: null,
};

// ---- 通用工具 ----
function api(path, opts = {}) {
  return fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  }).then(async (r) => {
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  });
}
const fmt = (x, d = 3) => (x === null || x === undefined || Number.isNaN(x) ? "—" : Number(x).toFixed(d));
function showTip(html, evt) {
  tooltip.innerHTML = html;
  tooltip.style.display = "block";
  tooltip.style.left = `${evt.clientX + 14}px`;
  tooltip.style.top = `${evt.clientY + 14}px`;
}
function hideTip() { tooltip.style.display = "none"; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function colorScale(v, vmin, vmax) {
  if (vmin === vmax) return seqColor(2);
  const t = (v - vmin) / (vmax - vmin);
  const idx = Math.max(0, Math.min(SEQ - 1, Math.round(t * (SEQ - 1))));
  return seqColor(idx);
}
function divergeScale(v, vabs) {
  if (vabs <= 0) return "var(--diverge-mid)";
  const t = Math.max(0, Math.min(1, Math.abs(v) / vabs));
  // 插值：中性 → 蓝（正）/红（负），用 CSS 颜色混合近似
  const end = v > 0 ? "var(--diverge-pos)" : "var(--diverge-neg)";
  return `color-mix(in srgb, ${end} ${Math.round(t * 100)}%, var(--diverge-mid))`;
}

// ---- 标签页 ----
$$(".tabs button").forEach((btn) => btn.addEventListener("click", () => {
  $$(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".tabpane").forEach((p) => p.classList.toggle("active", p.id === `tab-${btn.dataset.tab}`));
}));

$("#theme-select").addEventListener("change", (e) => {
  document.documentElement.dataset.theme = e.target.value;
  if (state.current) renderAll();  // 色值在渲染时从 CSS 变量解析，切主题需重绘
});

// ---- 数据加载 ----
async function loadPlates() {
  state.plates = await api("/api/plates");
  const tbody = $("#plate-table tbody");
  tbody.innerHTML = state.plates.length ? "" :
    `<tr><td colspan="5" class="muted">尚未导入任何板</td></tr>`;
  for (const p of state.plates) {
    const roleBadge = p.role === "validation"
      ? `<span class="badge warn">独立验证</span>`
      : `<span class="badge neutral">校准</span>`;
    tbody.insertAdjacentHTML("beforeend",
      `<tr><td><b>${p.plate_id}</b></td><td>${roleBadge}</td>
       <td>${p.scenario}</td><td class="num">${p.n_wells}</td>
       <td class="num">#${p.import_seq}</td></tr>`);
  }
  const picker = $("#plate-picker");
  picker.innerHTML = "";
  for (const p of state.plates) {
    const opt = document.createElement("option");
    opt.value = p.plate_id;
    opt.textContent = `${p.plate_id}（${p.role === "validation" ? "验证" : "校准"} / ${p.scenario}）`;
    opt.selected = true;
    picker.add(opt);
  }
  const viewPicker = $("#plate-view-picker");
  const old = viewPicker.value;
  viewPicker.innerHTML = "";
  for (const p of state.plates) {
    const opt = document.createElement("option");
    opt.value = p.plate_id;
    opt.textContent = p.plate_id;
    viewPicker.add(opt);
  }
  if (old && state.plates.some((p) => p.plate_id === old)) viewPicker.value = old;
  state.viewPlate = viewPicker.value || null;
  $("#append-note").textContent = state.plates.length
    ? "追加新板只会出现在新分析中；历史分析的板集合、阈值与结论保持不变。"
    : "";
}

async function loadAnalyses() {
  state.analyses = await api("/api/analyses");
  const ul = $("#analysis-timeline");
  ul.innerHTML = state.analyses.length ? "" :
    `<li class="muted small">暂无分析</li>`;
  for (const a of state.analyses) {
    const li = document.createElement("li");
    li.className = a.id === state.currentId ? "current" : "";
    li.innerHTML = `#${a.id} <span class="small muted">${a.created_at}</span><br>
      <span class="small">${a.method.toUpperCase()} · 秩 ${a.rank}${a.rank_deficient ? " · 秩缺陷" : ""}
      · ${a.plate_set.length} 板${a.parent_analysis_id ? ` · 派生自 #${a.parent_analysis_id}` : ""}</span>`;
    li.addEventListener("click", () => selectAnalysis(a.id));
    ul.appendChild(li);
  }
}

async function selectAnalysis(id) {
  state.currentId = id;
  state.current = await api(`/api/analyses/${id}`);
  const plateSet = state.current.plate_set;
  if (!plateSet.includes(state.viewPlate)) state.viewPlate = plateSet[0];
  // 板面图切换器只列当前快照包含的板，避免选到分析外的板
  const viewPicker = $("#plate-view-picker");
  viewPicker.innerHTML = "";
  for (const pid of plateSet) {
    const opt = document.createElement("option");
    opt.value = pid;
    opt.textContent = pid;
    viewPicker.add(opt);
  }
  viewPicker.value = state.viewPlate;
  await loadAnalyses();
  renderAll();
}

// ---- 导入与分析创建 ----
$("#btn-import-all").addEventListener("click", async () => {
  try {
    const r = await api("/api/import/generate", { method: "POST", body: {} });
    await loadPlates();
    alert(`导入完成：\n` + Object.entries(r).map(([k, v]) => `${k}: ${v.join(", ") || "（已存在，跳过）"}`).join("\n"));
  } catch (e) { alert(e.message); }
});
$$("[data-scenario]").forEach((btn) => btn.addEventListener("click", async () => {
  try {
    const r = await api("/api/import/scenario",
      { method: "POST", body: { scenario: btn.dataset.scenario } });
    await loadPlates();
    alert(`场景 ${r.scenario} 导入：${r.imported.join(", ")}`);
  } catch (e) { alert(e.message); }
}));

$("#btn-run").addEventListener("click", async () => {
  const selected = [...$("#plate-picker").selectedOptions].map((o) => o.value);
  try {
    const r = await api("/api/analyses", {
      method: "POST",
      body: { plate_ids: selected, k: parseFloat($("#bad-k").value) },
    });
    await loadAnalyses();
    await selectAnalysis(r.analysis_id);
  } catch (e) { alert(e.message); }
});

$("#plate-view-picker").addEventListener("change", (e) => {
  state.viewPlate = e.target.value;
  renderPlateView();
});
$("#plate-metric").addEventListener("change", renderPlateView);

// ---- 设计诊断页 ----
function renderDesign() {
  const a = state.current;
  if (!a) return;
  const s = a.summary;
  const metrics = [
    ["拟合方法", a.method.toUpperCase()],
    ["设计秩 / 参数数", `${a.rank} / ${s.n_par}`],
    ["观测孔 / 参与拟合", `${s.n_obs} / ${a.used_keys.length}`],
    ["残差尺度 σ", fmt(s.sigma)],
    ["坏孔阈值 k", String(a.bad_well_k)],
  ];
  $("#design-metrics").innerHTML = metrics.map(([l, v]) =>
    `<div class="metric-tile"><div class="v">${v}</div><div class="l">${l}</div></div>`).join("");

  const alerts = $("#design-alerts");
  if (!a.diagnostics.length) {
    alerts.innerHTML = `<div class="alert infobox"><b>设计满秩，所有参数可识别</b>
      设计矩阵 SVD 未发现零空间方向，行列/边缘/处理效应可以分开估计。</div>`;
  } else {
    alerts.innerHTML = a.diagnostics.map((d) =>
      `<div class="alert"><b>不可识别：${escapeHtml(d.kind)}</b>${escapeHtml(d.message)}</div>`).join("");
  }
  $("#diagnostic-table tbody").innerHTML = a.diagnostics.length
    ? a.diagnostics.map((d) => `<tr>
        <td><span class="badge crit">${escapeHtml(d.kind)}</span></td>
        <td>${escapeHtml(d.message)}</td>
        <td class="small">${d.involved.map((x) => `<span class="kbd">${escapeHtml(x.column)}</span>`).join(" ")}</td>
      </tr>`).join("")
    : `<tr><td colspan="3" class="muted">无混杂项</td></tr>`;

  const choice = a.method_choice;
  $("#method-reasons").innerHTML = `
    <div class="alert infobox"><b>自动选择：${choice.choice.method.toUpperCase()}</b>
      ${choice.choice.reasons.map(escapeHtml).join("<br>")}</div>
    <table><thead><tr><th>对照覆盖检查</th><th>结果</th></tr></thead><tbody>
      <tr><td>对照好孔数</td><td class="num">${choice.coverage.n_control_wells}</td></tr>
      <tr><td>每板对照覆盖 ≥2 行</td><td>${yesno(choice.coverage.row_ok)}</td></tr>
      <tr><td>每板对照覆盖 ≥2 列</td><td>${yesno(choice.coverage.col_ok)}</td></tr>
      <tr><td>边缘与内部均有对照</td><td>${yesno(choice.coverage.edge_ok)}</td></tr>
    </tbody></table>`;

  const v = a.validation_result;
  const box = $("#validation-box");
  if (!v) {
    box.innerHTML = `<p class="muted small">本板集合不含独立验证板。</p>`;
  } else if (v.note) {
    box.innerHTML = `<div class="alert warnbox"><b>未独立验证</b>${escapeHtml(v.note)}</div>`;
  } else if (v.n_compared === 0) {
    box.innerHTML = `<div class="alert warnbox"><b>无法验证</b>
      校准板或验证板上的处理对比均不可估（见设计诊断），独立验证无法进行；
      这不是"验证未通过"，而是当前设计没有提供可比较的估计。</div>`;
  } else {
    const badge = v.passed ? `<span class="badge good">验证通过</span>`
      : `<span class="badge crit">验证未通过</span>`;
    box.innerHTML = `<h3>${badge} 一致 ${v.n_agreed}/${v.n_compared}</h3>
      <table><thead><tr><th>处理</th><th>状态</th><th class="num">校准板估计</th>
        <th class="num">验证板估计</th><th class="num">差值</th><th class="num">差值 95% 区间</th></tr></thead>
      <tbody>${v.rows.map((r) => `<tr>
        <td>${r.treatment}</td>
        <td>${r.status === "agree" ? '<span class="badge good">一致</span>'
          : r.status === "disagree" ? '<span class="badge crit">不一致</span>'
          : '<span class="badge warn">不可估</span>'}</td>
        <td class="num">${fmt(r.calib_estimate)}</td>
        <td class="num">${fmt(r.validation_estimate)}</td>
        <td class="num">${fmt(r.difference)}</td>
        <td class="num">${r.ci_low === undefined ? "—" : `[${fmt(r.ci_low)}, ${fmt(r.ci_high)}]`}</td>
      </tr>`).join("")}</tbody></table>`;
  }
}
function yesno(b) {
  return b ? `<span class="badge good">是</span>` : `<span class="badge crit">否</span>`;
}

// ---- 板面图 ----
function renderPlateView() {
  const a = state.current;
  if (!a || !state.viewPlate) return;
  const pid = state.viewPlate;
  const plate = state.plates.find((p) => p.plate_id === pid);
  if (!plate) return;
  const wells = a.wells.filter((w) => w.plate_id === pid);
  const metric = $("#plate-metric").value;
  const vals = wells.map((w) => w[metric]).filter((x) => x !== null && x !== undefined);
  const vmin = Math.min(...vals), vmax = Math.max(...vals);
  const isDiverge = metric === "residual" || metric === "rstd";
  const vabs = Math.max(Math.abs(vmin), Math.abs(vmax));

  const rows = {};
  for (const w of wells) (rows[w.row_idx] ||= []).push(w);
  const html = [`<div class="plate-grid">`];
  html.push(`<div class="plate-row"><span class="axis-label"></span>` +
    Array.from({ length: plate.n_cols }, (_, j) =>
      `<span class="axis-label" style="width:44px">${j + 1}</span>`).join("") + `</div>`);
  for (let i = 0; i < plate.n_rows; i++) {
    html.push(`<div class="plate-row"><span class="axis-label">${String.fromCharCode(65 + i)}</span>`);
    for (const w of rows[i]) {
      const val = w[metric];
      const bg = val === null || val === undefined ? "var(--grid)"
        : isDiverge ? divergeScale(val, vabs) : colorScale(val, vmin, vmax);
      const cls = ["cell"];
      if (w.edge) cls.push("edge-cell");
      if (w.flagged_bad) cls.push("bad-cell", "excluded");
      html.push(`<div class="${cls.join(" ")}" data-key="${pid}|${w.row_idx}|${w.col_idx}"
        style="background:${bg}" title=""></div>`);
    }
    html.push(`</div>`);
  }
  html.push(`</div>`);
  $("#plate-view").innerHTML = html.join("");

  $$("#plate-view .cell").forEach((cell) => {
    cell.addEventListener("mousemove", (e) => {
      const w = wells.find((x) => `${pid}|${x.row_idx}|${x.col_idx}` === cell.dataset.key);
      showTip(`<b>${pid} ${String.fromCharCode(65 + w.row_idx)}${w.col_idx + 1}</b><br>
        处理：<span class="treat-dot" style="background:${treatColor(w.treatment)}"></span>${w.treatment}<br>
        响应：${fmt(w.response, 2)}<br>拟合：${fmt(w.fitted, 2)}<br>
        残差：${fmt(w.residual, 2)}<br>标准化残差：${fmt(w.rstd, 2)}<br>
        边缘孔：${w.edge ? "是" : "否"}<br>参与拟合：${w.used_in_fit ? "是" : "否（坏孔屏蔽）"}`, e);
    });
    cell.addEventListener("mouseleave", hideTip);
    cell.addEventListener("click", (evt) => openWellPopover(cell.dataset.key, evt));
  });

  $("#plate-legend").innerHTML = isDiverge
    ? `<span><span class="swatch" style="background:var(--diverge-neg)"></span>负</span>
       <span><span class="swatch" style="background:var(--diverge-mid)"></span>0</span>
       <span><span class="swatch" style="background:var(--diverge-pos)"></span>正</span>
       <span class="muted">范围 [${fmt(vmin, 2)}, ${fmt(vmax, 2)}]</span>`
    : Array.from({ length: SEQ }, (_, i) => SEQ - 1 - i).map((i) =>
      `<span><span class="swatch" style="background:${seqColor(i)}"></span>${
        i === SEQ - 1 ? fmt(vmax, 1) : i === 0 ? fmt(vmin, 1) : ""}</span>`).join("")
      + `<span class="muted">范围 [${fmt(vmin, 2)}, ${fmt(vmax, 2)}]</span>`;
}

// ---- 孔位弹层：翻转标记 ----
function openWellPopover(key, evt) {
  const a = state.current;
  const [pid, ri, ci] = key.split("|");
  const w = a.wells.find((x) => x.plate_id === pid && x.row_idx === ri && x.col_idx === Number(ci));
  if (!w) return;
  popover.innerHTML = `
    <b>${pid} · ${String.fromCharCode(65 + w.row_idx)}${w.col_idx + 1}</b>
    <table>
      <tr><td>处理 / 重复组</td><td>${w.treatment}（${w.replicate_group || "—"}）</td></tr>
      <tr><td>原始响应（不可变）</td><td>${fmt(w.response, 4)}</td></tr>
      <tr><td>拟合值 / 残差</td><td>${fmt(w.fitted, 3)} / ${fmt(w.residual, 3)}</td></tr>
      <tr><td>标准化残差</td><td>${fmt(w.rstd, 3)}</td></tr>
      <tr><td>系统建议坏孔</td><td>${w.suggested_bad ? "是" : "否"}</td></tr>
      <tr><td>当前是否屏蔽</td><td>${w.flagged_bad ? "是（不参与拟合）" : "否"}</td></tr>
    </table>
    <p class="small muted">翻转标记会以相同板集合与阈值创建<b>新分析版本</b>，当前版本保持不变。</p>
    <div class="pill-row">
      <button class="primary" id="pop-toggle">${w.flagged_bad ? "恢复该孔（标记为好孔）" : "标记为坏孔并重新分析"}</button>
      <button class="ghost" id="pop-close">关闭</button>
    </div>`;
  popover.style.display = "block";
  popover.style.left = `${Math.min(window.innerWidth - 330, evt.clientX + 10)}px`;
  popover.style.top = `${Math.min(window.innerHeight - 320, evt.clientY + 10)}px`;
  $("#pop-close").onclick = () => (popover.style.display = "none");
  $("#pop-toggle").onclick = async () => {
    // 继承当前版本的全部显式标记，再翻转本孔
    const flags = { ...a.explicit_flags, [key]: !w.flagged_bad };
    try {
      const r = await api(`/api/analyses/${a.id}/rerun`, {
        method: "POST",
        body: { explicit_flags: flags, note: `翻转孔位 ${key} 标记` },
      });
      popover.style.display = "none";
      await loadAnalyses();
      await selectAnalysis(r.analysis_id);
    } catch (e) { alert(e.message); }
  };
}

// ---- 残差图（SVG）----
function svgEl(tag, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}
function renderResidualScatter() {
  const a = state.current;
  const host = $("#resid-scatter");
  host.innerHTML = "";
  const W = 860, H = 380, M = { l: 56, r: 20, t: 16, b: 44 };
  const svg = svgEl("svg", { width: "100%", viewBox: `0 0 ${W} ${H}` });
  const wells = a.wells.filter((w) => w.used_in_fit);
  const xs = wells.map((w) => w.fitted), ys = wells.map((w) => w.residual);
  const xmin = Math.min(...xs), xmax = Math.max(...xs);
  const yabs = Math.max(...ys.map((y) => Math.abs(y))) * 1.15;
  const X = (x) => M.l + ((x - xmin) / (xmax - xmin || 1)) * (W - M.l - M.r);
  const Y = (y) => M.t + (1 - (y + yabs) / (2 * yabs)) * (H - M.t - M.b);
  for (const gy of [-yabs, -yabs / 2, 0, yabs / 2, yabs]) {
    svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: Y(gy), y2: Y(gy),
      class: gy === 0 ? "zero-line" : "gridline" }));
    const t = svgEl("text", { x: M.l - 8, y: Y(gy) + 4, "text-anchor": "end" });
    t.textContent = gy.toFixed(1);
    svg.appendChild(t);
  }
  svg.appendChild(svgEl("line", { x1: M.l, x2: M.l, y1: M.t, y2: H - M.b, class: "axis-line" }));
  svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: H - M.b, y2: H - M.b, class: "axis-line" }));
  const xlab = svgEl("text", { x: (W + M.l - M.r) / 2, y: H - 8, "text-anchor": "middle" });
  xlab.textContent = "拟合值"; svg.appendChild(xlab);
  const ylab = svgEl("text", { x: 16, y: (H + M.t - M.b) / 2, "text-anchor": "middle",
    transform: `rotate(-90 16 ${(H + M.t - M.b) / 2})` });
  ylab.textContent = "残差"; svg.appendChild(ylab);
  for (const w of wells) {
    const c = svgEl("circle", {
      cx: X(w.fitted), cy: Y(w.residual), r: 4.5,
      "fill-opacity": 0.85,
      stroke: "var(--surface)", "stroke-width": 1.5,
    });
    c.style.fill = treatColor(w.treatment);
    c.addEventListener("mousemove", (e) => showTip(
      `<b>${w.plate_id} ${String.fromCharCode(65 + w.row_idx)}${w.col_idx + 1}</b><br>
       处理 ${w.treatment}<br>拟合 ${fmt(w.fitted, 2)}<br>残差 ${fmt(w.residual, 2)}`, e));
    c.addEventListener("mouseleave", hideTip);
    svg.appendChild(c);
  }
  host.appendChild(svg);
}

function renderBandResiduals(hostId, keyFn, labels) {
  const a = state.current;
  const host = $(hostId);
  host.innerHTML = "";
  const groups = {};
  for (const w of a.wells.filter((x) => x.used_in_fit)) {
    (groups[keyFn(w)] ||= []).push(w.residual);
  }
  const W = 460, H = 260, M = { l: 44, r: 14, t: 14, b: 40 };
  const svg = svgEl("svg", { width: "100%", viewBox: `0 0 ${W} ${H}` });
  const medians = labels.map(([idx, name]) => ({
    name, med: groups[idx] ? groups[idx].sort((p, q) => p - q)[Math.floor(groups[idx].length / 2)] : 0,
  }));
  const yabs = Math.max(0.5, ...medians.map((m) => Math.abs(m.med))) * 1.3;
  const Y = (y) => M.t + (1 - (y + yabs) / (2 * yabs)) * (H - M.t - M.b);
  const bw = (W - M.l - M.r) / medians.length;
  svg.appendChild(svgEl("line", { x1: M.l, x2: W - M.r, y1: Y(0), y2: Y(0), class: "zero-line" }));
  medians.forEach((m, i) => {
    const x = M.l + i * bw + bw * 0.18, w = bw * 0.64;
    const y0 = Y(0), y1 = Y(m.med);
    svg.appendChild(svgEl("rect", {
      x, y: Math.min(y0, y1), width: w, height: Math.abs(y1 - y0) || 2,
      rx: 4, fill: m.med >= 0 ? "var(--diverge-pos)" : "var(--diverge-neg)",
      "fill-opacity": 0.85,
    }));
    const t = svgEl("text", { x: x + w / 2, y: H - M.b + 16, "text-anchor": "middle" });
    t.textContent = m.name; svg.appendChild(t);
    const v = svgEl("text", { x: x + w / 2, y: Math.min(y0, y1) - 4,
      "text-anchor": "middle" });
    v.textContent = fmt(m.med, 2); svg.appendChild(v);
  });
  host.appendChild(svg);
}

// ---- 坏孔复核 ----
function renderBadWells() {
  const a = state.current;
  const suggested = a.suggested_bad.length;
  const flagged = a.wells.filter((w) => w.flagged_bad).length;
  $("#badwell-summary").innerHTML = `
    <div class="metrics">
      <div class="metric-tile"><div class="v">${suggested}</div><div class="l">系统建议坏孔（|r_std| &gt; ${a.bad_well_k}）</div></div>
      <div class="metric-tile"><div class="v">${flagged}</div><div class="l">当前版本实际屏蔽（原记录均保留）</div></div>
      <div class="metric-tile"><div class="v">${a.wells.length - flagged}</div><div class="l">参与拟合孔数</div></div>
    </div>
    <p class="small muted">阈值 k=${a.bad_well_k} 在分析 #${a.id} 创建时冻结，后续追加板不会回头修改本版本。</p>`;
  const candidates = a.wells.filter((w) => w.suggested_bad || w.flagged_bad);
  $("#badwell-table tbody").innerHTML = candidates.length
    ? candidates.map((w) => `<tr>
        <td>${w.plate_id}</td>
        <td><b>${String.fromCharCode(65 + w.row_idx)}${w.col_idx + 1}</b></td>
        <td>${w.treatment}</td>
        <td class="num">${fmt(w.response, 2)}</td>
        <td class="num">${fmt(w.rstd, 2)}</td>
        <td>${w.suggested_bad ? '<span class="badge warn">建议</span>' : '<span class="badge neutral">否</span>'}</td>
        <td>${w.flagged_bad ? '<span class="badge crit">已屏蔽</span>' : '<span class="badge good">参与拟合</span>'}</td>
        <td><button data-toggle="${w.plate_id}|${w.row_idx}|${w.col_idx}">翻转标记（新版本）</button></td>
      </tr>`).join("")
    : `<tr><td colspan="8" class="muted">没有坏孔建议，也没有被屏蔽的孔</td></tr>`;
  $$("#badwell-table [data-toggle]").forEach((btn) => btn.addEventListener("click", async () => {
    const key = btn.dataset.toggle;
    const w = a.wells.find((x) => `${x.plate_id}|${x.row_idx}|${x.col_idx}` === key);
    const flags = { ...a.explicit_flags, [key]: !w.flagged_bad };
    try {
      const r = await api(`/api/analyses/${a.id}/rerun`, {
        method: "POST", body: { explicit_flags: flags, note: `翻转孔位 ${key} 标记` } });
      await loadAnalyses();
      await selectAnalysis(r.analysis_id);
    } catch (e) { alert(e.message); }
  }));
  renderVersionDiff();
}

async function renderVersionDiff() {
  const a = state.current;
  const host = $("#version-diff");
  if (!a.parent_analysis_id) {
    host.innerHTML = `<p class="muted small">当前为基线版本，没有可对比的父版本。</p>`;
    return;
  }
  const parent = await api(`/api/analyses/${a.parent_analysis_id}`);
  const curMap = new Map(a.wells.map((w) => [`${w.plate_id}|${w.row_idx}|${w.col_idx}`, w]));
  const parMap = new Map(parent.wells.map((w) => [`${w.plate_id}|${w.row_idx}|${w.col_idx}`, w]));
  const changed = [];
  for (const [key, w] of curMap) {
    const p = parMap.get(key);
    if (p && p.flagged_bad !== w.flagged_bad)
      changed.push({ key, from: p.flagged_bad, to: w.flagged_bad });
  }
  const curC = new Map(a.contrasts.filter((c) => c.kind === "treatment" && c.estimable)
    .map((c) => [c.label, c]));
  const parC = new Map(parent.contrasts.filter((c) => c.kind === "treatment" && c.estimable)
    .map((c) => [c.label, c]));
  host.innerHTML = `
    <p>坏孔标记变化：</p>
    <ul>${changed.map((c) => `<li class="${c.to ? "diff-del" : "diff-add"}">
      <span class="kbd">${c.key}</span>：${c.from ? "屏蔽" : "参与"} → ${c.to ? "屏蔽" : "参与"}</li>`).join("")
      || '<li class="muted">无</li>'}</ul>
    <table><thead><tr><th>处理对比</th><th class="num">父版本 #${parent.id}</th>
      <th class="num">当前 #${a.id}</th><th class="num">估计变化</th><th class="num">SE 变化</th></tr></thead>
    <tbody>${[...curC.keys()].map((label) => {
      const p = parC.get(label), c = curC.get(label);
      if (!p) return `<tr><td>${label}</td><td colspan="4" class="muted">父版本不可估或缺失</td></tr>`;
      return `<tr><td>${label}</td><td class="num">${fmt(p.estimate)}</td>
        <td class="num">${fmt(c.estimate)}</td>
        <td class="num ${c.estimate - p.estimate >= 0 ? "diff-add" : "diff-del"}">${fmt(c.estimate - p.estimate)}</td>
        <td class="num">${fmt(c.se - p.se)}</td></tr>`;
    }).join("")}</tbody></table>`;
}

// ---- 逐处理对比 ----
function renderTreatment() {
  const a = state.current;
  const cs = a.contrasts.filter((c) => c.kind === "treatment");
  const host = $("#treat-plot");
  host.innerHTML = "";
  const estimable = cs.filter((c) => c.estimable);
  if (!estimable.length) {
    host.innerHTML = `<div class="alert"><b>没有可估的处理对比</b>
      设计完全混杂时不输出数值估计，请查看"设计诊断"页的不可识别项。</div>`;
  } else {
    const W = 860, rowH = 46, M = { l: 130, r: 40, t: 16, b: 36 };
    const H = M.t + M.b + estimable.length * rowH;
    const svg = svgEl("svg", { width: "100%", viewBox: `0 0 ${W} ${H}` });
    const allEst = estimable.flatMap((c) => [c.ci_low, c.ci_high]);
    const lo = Math.min(0, ...allEst), hi = Math.max(0, ...allEst);
    const pad = (hi - lo) * 0.08 || 1;
    const X = (x) => M.l + ((x - lo + pad) / (hi - lo + 2 * pad)) * (W - M.l - M.r);
    svg.appendChild(svgEl("line", { x1: X(0), x2: X(0), y1: M.t, y2: H - M.b, class: "zero-line" }));
    estimable.forEach((c, i) => {
      const y = M.t + i * rowH + rowH / 2;
      const t = svgEl("text", { x: M.l - 10, y: y + 4, "text-anchor": "end" });
      t.textContent = c.label; svg.appendChild(t);
      svg.appendChild(svgEl("line", { x1: X(c.ci_low), x2: X(c.ci_high), y1: y, y2: y,
        stroke: "var(--blue)", "stroke-width": 2, "stroke-linecap": "round" }));
      svg.appendChild(svgEl("line", { x1: X(c.ci_low), x2: X(c.ci_low), y1: y - 6, y2: y + 6,
        stroke: "var(--blue)", "stroke-width": 2 }));
      svg.appendChild(svgEl("line", { x1: X(c.ci_high), x2: X(c.ci_high), y1: y - 6, y2: y + 6,
        stroke: "var(--blue)", "stroke-width": 2 }));
      const dot = svgEl("circle", { cx: X(c.estimate), cy: y, r: 6,
        fill: "var(--blue)", stroke: "var(--surface)", "stroke-width": 2 });
      dot.addEventListener("mousemove", (e) => showTip(
        `<b>${c.label}</b><br>估计 ${fmt(c.estimate, 3)}<br>SE ${fmt(c.se, 3)}<br>
         95% CI [${fmt(c.ci_low, 3)}, ${fmt(c.ci_high, 3)}]`, e));
      dot.addEventListener("mouseleave", hideTip);
      svg.appendChild(dot);
    });
    for (const gx of [lo, (lo + hi) / 2, hi]) {
      const t = svgEl("text", { x: X(gx), y: H - M.b + 18, "text-anchor": "middle" });
      t.textContent = gx.toFixed(1); svg.appendChild(t);
    }
    host.appendChild(svg);
  }
  const notEst = cs.filter((c) => !c.estimable);
  $("#treat-table tbody").innerHTML = cs.map((c) => `<tr>
    <td>${c.label}</td>
    <td>${c.estimable ? '<span class="badge good">可估</span>' : '<span class="badge crit">不可估</span>'}</td>
    <td class="num">${fmt(c.estimate)}</td><td class="num">${fmt(c.se)}</td>
    <td class="num">${fmt(c.ci_low)}</td><td class="num">${fmt(c.ci_high)}</td>
    <td class="small muted">${escapeHtml(c.note || "")}</td></tr>`).join("")
    + (notEst.length ? "" : "");
  const mp = a.summary.median_polish_detail;
  $("#mp-table tbody").innerHTML = mp ? mp.contrasts.map((c) => `<tr>
    <td>${c.treatment} vs ${mp.reference}</td>
    <td class="num">${fmt(c.estimate)}</td><td class="num">${fmt(c.se)}</td>
    <td class="num">${c.n_plates || "—"}</td></tr>`).join("") : "";
}

// ---- 总渲染 ----
function renderAll() {
  if (!state.current) return;
  renderDesign();
  renderPlateView();
  renderResidualScatter();
  const plate = state.plates.find((p) => p.plate_id === state.viewPlate);
  if (plate) {
    renderBandResiduals("#row-resid", (w) => w.row_idx,
      Array.from({ length: plate.n_rows }, (_, i) => [i, String.fromCharCode(65 + i)]));
    renderBandResiduals("#col-resid", (w) => w.col_idx,
      Array.from({ length: plate.n_cols }, (_, j) => [j, String(j + 1)]));
  }
  renderBadWells();
  renderTreatment();
}

// ---- 启动 ----
(async function init() {
  await loadPlates();
  await loadAnalyses();
  if (state.analyses.length) await selectAnalysis(state.analyses[state.analyses.length - 1].id);
})();
