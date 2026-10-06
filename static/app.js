/* EPUB3 章节跳转修复 —— 原生 JS 前端 */
"use strict";

const state = {
  sessionId: null,
  data: null,          // 上传响应
  decisions: {},       // linkId -> {targetFile, fragment}
  filesById: {},       // file -> {file,title,ids...}
  fileOrder: [],
  selectedFile: null,
  _hasErrors: false,
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------------------
// 元素引用
// ---------------------------------------------------------------------------
const form = $("uploadForm");
const fileInput = $("fileInput");
const uploadLabel = $("uploadLabel");
const uploadBtn = $("uploadBtn");
const uploadMsg = $("uploadMsg");
const uploadCard = $("uploadCard");
const fatalCard = $("fatalCard");
const fatalBox = $("fatalBox");
const resultCard = $("resultCard");
const bookMeta = $("bookMeta");
const faultBox = $("faultBox");
const faultList = $("faultList");
const chapterList = $("chapterList");
const linkTbody = $("linkTbody");
const linkStats = $("linkStats");
const linkTitle = $("linkTitle");
const showAll = $("showAllLinks");
const exportBtn = $("exportBtn");
const exportMsg = $("exportMsg");

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[c]);
}

function setMsg(el, text, kind = "") {
  el.textContent = text || "";
  el.className = "msg" + (kind ? " " + kind : "");
}

function resetResults() {
  // 换书时清空旧裁决与旧数据
  state.sessionId = null;
  state.data = null;
  state.decisions = {};
  state.filesById = {};
  state.fileOrder = [];
  state.selectedFile = null;
  fatalCard.classList.add("hidden");
  resultCard.classList.add("hidden");
  faultBox.classList.add("hidden");
  showAll.checked = false;
  state._hasErrors = false;
  fileInput.value = "";
  uploadBtn.disabled = true;
  uploadLabel.textContent = "选择或拖入未加密的 .epub 文件";
  setMsg(uploadMsg, "");
  setMsg(exportMsg, "");
}

// ---------------------------------------------------------------------------
// 上传
// ---------------------------------------------------------------------------

fileInput.addEventListener("change", () => {
  const f = fileInput.files[0];
  uploadBtn.disabled = !f;
  uploadLabel.textContent = f ? f.name : "选择或拖入未加密的 .epub 文件";
  setMsg(uploadMsg, "");
});

const dropZone = document.querySelector(".upload-box");
["dragenter", "dragover"].forEach((ev) =>
  dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropZone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((ev) =>
  dropZone.addEventListener(ev, (e) => {
    e.preventDefault();
    dropZone.classList.remove("drag");
  })
);
dropZone.addEventListener("drop", (e) => {
  const f = e.dataTransfer.files && e.dataTransfer.files[0];
  if (f) {
    const dt = new DataTransfer();
    dt.items.add(f);
    fileInput.files = dt.files;
    fileInput.dispatchEvent(new Event("change"));
  }
});

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = fileInput.files[0];
  if (!f) return;
  resetResults();
  uploadBtn.disabled = true;
  setMsg(uploadMsg, "正在上传并分析…");

  const fd = new FormData();
  fd.append("file", f);
  let resp;
  try {
    resp = await fetch("/api/upload", { method: "POST", body: fd });
  } catch (err) {
    setMsg(uploadMsg, "网络错误：" + err.message, "error");
    uploadBtn.disabled = false;
    return;
  }

  if (resp.status === 422) {
    const body = await resp.json();
    fatalBox.innerHTML = renderFault(body.fatal ?? body.detail, true);
    fatalCard.classList.remove("hidden");
    setMsg(uploadMsg, "解析失败，请查看下方定位信息。", "error");
    uploadBtn.disabled = false;
    return;
  }
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    setMsg(uploadMsg, "上传失败：" + (body.detail?.message || resp.status), "error");
    uploadBtn.disabled = false;
    return;
  }

  const data = await resp.json();
  state.sessionId = data.sessionId;
  state.data = data;
  applyAnalysis(data);
  setMsg(uploadMsg, "");
  uploadBtn.disabled = false;
});

function renderFault(f, fatal) {
  if (!f) return "";
  if (Array.isArray(f)) {
    return f.map((x) => renderFault(x, fatal)).join("");
  }
  const severity = f.severity || (fatal ? "error" : "warning");
  const cls = "fault " + (fatal || severity === "error" ? "fatal" : "warn");
  const tag = severity === "error" ? "【阻断导出】" : "【提示】";
  const loc = f.file
    ? `<span class="loc">${esc(tag)}${esc(f.file)}</span>${f.linkId ? ` 链接 ${esc(f.linkId)}` : ""}<br>`
    : "";
  return `<div class="${cls}">${loc}${esc(f.message || "")}</div>`;
}

// ---------------------------------------------------------------------------
// 渲染分析结果
// ---------------------------------------------------------------------------

function applyAnalysis(data) {
  resultCard.classList.remove("hidden");

  state.filesById = {};
  state.fileOrder = data.files.map((x) => x.file);
  data.files.forEach((f) => { state.filesById[f.file] = f; });

  const s = data.stats;
  bookMeta.textContent =
    `OPF：${data.opf} · XHTML ${s.xhtmlCount} 篇 · ` +
    `链接 ${s.linkCount} 条 · 失效 ${s.brokenCount} 条 · 外部 ${s.externalCount} 条`;

  // 结构问题
  const errors = (data.faults || []).filter((f) => (f.severity || "error") === "error");
  const warnings = (data.faults || []).filter((f) => f.severity === "warning");
  state._hasErrors = errors.length > 0;
  if (data.faults && data.faults.length) {
    faultBox.classList.remove("hidden");
    faultList.innerHTML =
      errors.map((f) => renderFault(f, false)).join("") +
      warnings.map((f) => renderFault(f, false)).join("");
  } else {
    faultBox.classList.add("hidden");
    state._hasErrors = false;
  }

  // 章节（spine 顺序）
  chapterList.innerHTML = data.chapters.map((c) => `
    <li>
      ${esc(c.title)}
      ${c.isNav ? '<span class="nav-tag">nav</span>' : ""}
      <div class="file">${esc(c.file)}</div>
    </li>`).join("");

  // 链接
  state.decisions = {};
  renderLinks();
  updateExportState();
}

showAll.addEventListener("change", renderLinks);

function visibleLinks() {
  const links = state.data.links;
  if (showAll.checked) return links;
  return links.filter((l) => !l.external && l.status === "broken");
}

function renderLinks() {
  const links = visibleLinks();
  const broken = state.data.links.filter((l) => !l.external && l.status === "broken");
  const external = state.data.links.filter((l) => l.external);
  linkTitle.textContent = showAll.checked ? "全部链接" : "失效链接";
  linkStats.textContent =
    `当前显示 ${links.length} 条；其中失效 ${broken.length} 条，外部 ${external.length} 条（外部链接不检查、不修改）。`;

  // 为每条可定位的失效链接给出默认裁决（默认：目标文件存在则指向该文件顶部）；
  // 不可定位的链接无法导出，不建立裁决。
  const brokenLinks = state.data.links.filter(
    (l) => !l.external && l.status === "broken" && l.mappable
  );
  brokenLinks.forEach((l) => {
    if (!state.decisions[l.id]) {
      const targetFile =
        l.targetFile && state.filesById[l.targetFile] ? l.targetFile : l.file;
      state.decisions[l.id] = { targetFile, fragment: "" };
    }
  });

  const rows = links.map((l, idx) => renderLinkRow(l, idx + 1)).join("");
  linkTbody.innerHTML = rows;

  // 绑定行内控件
  linkTbody.querySelectorAll("tr[data-link]").forEach((tr) => {
    const id = tr.getAttribute("data-link");
    const fileSel = tr.querySelector(".target-file");
    const fragSel = tr.querySelector(".target-fragment");
    if (!fileSel) return;
    fileSel.addEventListener("change", () => onTargetFileChange(id));
    fragSel.addEventListener("change", () => onFragmentChange(id));
    syncRow(id);
  });
}

function renderLinkRow(l, idx) {
  const isBroken = !l.external && l.status === "broken";
  const rowClass = isBroken ? "row-broken" : (l.external ? "row-external" : "");
  const statusBadge = l.external
    ? '<span class="badge external">外部</span>'
    : isBroken
      ? '<span class="badge broken">失效</span>'
      : '<span class="badge ok">有效</span>';
  const sourceBadge = `<span class="badge ${esc(l.source)}">${
    { nav: "nav", spine: "spine", manifest: "manifest" }[l.source] || esc(l.source)
  }</span>`;
  const mapBadge = isBroken && !l.mappable
    ? '<span class="badge nomap">无法定位</span>'
    : "";

  const reasonClass = l.status === "ok" ? "reason ok-reason" : "reason";
  const reason = esc(l.reasonText || l.reason);

  let targetCells = "";
  if (isBroken && !l.mappable) {
    targetCells = `
      <td class="hint" colspan="1">无法唯一定位原文字节，禁止修改（见上方定位信息）</td>
      <td class="mono">${esc(l.href)}</td>`;
  } else if (isBroken) {
    targetCells = `
      <td>
        <select class="target-file" data-link="${esc(l.id)}"></select>
        <select class="target-fragment" data-link="${esc(l.id)}"></select>
      </td>
      <td class="mono preview-href" data-preview="${esc(l.id)}"></td>`;
  } else {
    targetCells = `
      <td class="hint">${l.external ? "外部链接保持不变" : "无需修改"}</td>
      <td class="mono">${esc(l.href)}</td>`;
  }

  return `
    <tr class="${rowClass}" data-link="${esc(l.id)}">
      <td>${idx}</td>
      <td>
        ${sourceBadge}${mapBadge}
        <div class="file">${esc(l.file)}</div>
      </td>
      <td>${esc(l.text || "（无文字）")}</td>
      <td class="mono">${esc(l.href)}</td>
      <td>${statusBadge}<div class="${reasonClass}">${reason}</div></td>
      ${targetCells}
    </tr>`;
}

function populateFileSelect(sel, link, chosen) {
  sel.innerHTML = state.data.files.map((f) => {
    const label = f.title && f.title !== f.file ? `${f.title}（${f.file}）` : f.file;
    return `<option value="${esc(f.file)}" ${f.file === chosen ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
}

function populateFragmentSelect(sel, targetFile, chosen) {
  const fm = state.filesById[targetFile];
  const topLabel = "（章节顶部）";
  const top = `<option value="" ${chosen === "" ? "selected" : ""}>${topLabel}</option>`;
  const opts = fm.ids.map((idv) =>
    `<option value="${esc(idv)}" ${idv === chosen ? "selected" : ""}>#${esc(idv)}</option>`
  ).join("");
  sel.innerHTML = top + opts;
}

function onTargetFileChange(linkId) {
  const tr = linkTbody.querySelector(`tr[data-link="${cssEsc(linkId)}"]`);
  const file = tr.querySelector(".target-file").value;
  state.decisions[linkId] = { targetFile: file, fragment: "" };
  populateFragmentSelect(tr.querySelector(".target-fragment"), file, "");
  updatePreview(linkId);
  updateExportState();
}

function onFragmentChange(linkId) {
  const tr = linkTbody.querySelector(`tr[data-link="${cssEsc(linkId)}"]`);
  const file = tr.querySelector(".target-file").value;
  const frag = tr.querySelector(".target-fragment").value;
  state.decisions[linkId] = { targetFile: file, fragment: frag };
  updatePreview(linkId);
  updateExportState();
}

function cssEsc(s) {
  return String(s).replace(/([^a-zA-Z0-9_-])/g, "\\$1");
}

function syncRow(linkId) {
  const tr = linkTbody.querySelector(`tr[data-link="${cssEsc(linkId)}"]`);
  if (!tr) return;
  const dec = state.decisions[linkId];
  const link = state.data.links.find((x) => x.id === linkId);
  const fileSel = tr.querySelector(".target-file");
  const fragSel = tr.querySelector(".target-fragment");
  if (!fileSel) return;
  populateFileSelect(fileSel, link, dec.targetFile);
  populateFragmentSelect(fragSel, dec.targetFile, dec.fragment ?? "");
  updatePreview(linkId);
}

async function updatePreview(linkId) {
  const dec = state.decisions[linkId];
  const box = linkTbody.querySelector(`[data-preview="${cssEsc(linkId)}"]`);
  if (!box || !dec) return;
  box.textContent = "…";
  try {
    const resp = await fetch("/api/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sessionId: state.sessionId,
        linkId,
        targetFile: dec.targetFile,
        fragment: dec.fragment ?? null,
      }),
    });
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      box.textContent = "（" + (body.detail?.message || resp.status) + "）";
      return;
    }
    const data = await resp.json();
    box.textContent = data.href || "(空，指向本篇顶部)";
  } catch (err) {
    box.textContent = "预览失败：" + err.message;
  }
}

// ---------------------------------------------------------------------------
// 导出
// ---------------------------------------------------------------------------

function updateExportState() {
  const hasErrors = !!state._hasErrors;
  const broken = state.data
    ? state.data.links.filter((l) => !l.external && l.status === "broken" && l.mappable)
    : [];
  const unmappable = broken.some((l) => !l.mappable);
  const undecided = broken.some((l) => !state.decisions[l.id]);
  const blocked = hasErrors || unmappable || undecided;
  exportBtn.disabled = blocked;
  exportBtn.title = hasErrors || unmappable
    ? "存在无法唯一定位的链接或致命结构问题，禁止导出"
    : undecided ? "仍有失效链接未处理" : "";
}

exportBtn.addEventListener("click", async () => {
  if (!state.sessionId) return;
  setMsg(exportMsg, "正在重写并重新打包、重扫…");
  exportBtn.disabled = true;

  // 仅提交实际裁决（去掉前端标记字段）
  const decisions = {};
  Object.entries(state.decisions).forEach(([k, v]) => {
    decisions[k] = { targetFile: v.targetFile, fragment: v.fragment ?? null };
  });

  let resp;
  try {
    resp = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sessionId: state.sessionId, decisions }),
    });
  } catch (err) {
    setMsg(exportMsg, "导出失败：" + err.message, "error");
    exportBtn.disabled = false;
    return;
  }

  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    const d = body.detail || {};
    let msg = d.message || `导出被拒绝（HTTP ${resp.status}）`;
    if (d.faults && d.faults.length) {
      msg += "\n" + d.faults.map((f) => `· [${f.code}] ${f.file || ""} ${f.message}`).join("\n");
    }
    if (d.broken && d.broken.length) {
      msg += "\n重扫仍有失效链接：" + d.broken
        .map((b) => `· ${b.file} 「${b.text}」 ${b.href}`).join("\n");
    }
    setMsg(exportMsg, msg, "error");
    exportBtn.disabled = false;
    return;
  }

  const blob = await resp.blob();
  const cd = resp.headers.get("Content-Disposition") || "";
  let name = "fixed.epub";
  const m = /filename\*?=(?:UTF-8'')?([^;]+)/i.exec(cd);
  if (m) name = decodeURIComponent(m[1].replace(/^"|"$/g, ""));
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setMsg(exportMsg, "导出成功，已开始下载：" + name, "ok");
  exportBtn.disabled = false;
});
