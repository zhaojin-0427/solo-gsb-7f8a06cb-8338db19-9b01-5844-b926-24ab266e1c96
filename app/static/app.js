"use strict";

const state = {
  jobId: null,
  filename: "",
  chapters: [],
  links: [],
  stats: null,
  decisions: {},
};

const $ = (id) => document.getElementById(id);

function setStatus(el, msg, kind) {
  el.textContent = msg || "";
  el.className = kind || "";
}

const reasonText = {
  "id-missing": "篇内缺少目标 id",
  "target-missing": "目标 XHTML 文件不存在",
  "path-escape": "相对路径越出包根",
  "bad-fragment-encoding": "片段百分号编码非法",
  "not-in-manifest": "目标 XHTML 不在 manifest 中",
};

function reasonOf(r) {
  if (!r) return "失效";
  const [key, detail] = r.split(":");
  return (reasonText[key] || key) + (detail ? `：${decodeURIComponentSafe(detail)}` : "");
}

function decodeURIComponentSafe(s) {
  try { return decodeURIComponent(s); } catch (_) { return s; }
}

$("file").addEventListener("change", () => {
  const f = $("file").files[0];
  if (!f) return;
  upload(f);
});

async function upload(file) {
  // 换书：清空旧裁决与所有结果
  Object.assign(state, {
    jobId: null, filename: "", chapters: [], links: [], stats: null, decisions: {},
  });
  $("result-card").hidden = true;
  $("export-card").hidden = true;
  $("errors-card").hidden = true;
  setStatus($("export-status"), "", "");
  setStatus($("status"), `正在上传并解析 ${file.name} …`);

  const fd = new FormData();
  fd.append("file", file);
  let resp;
  try {
    resp = await fetch("/api/upload", { method: "POST", body: fd });
  } catch (e) {
    setStatus($("status"), `请求失败: ${e}`, "error");
    return;
  }
  const body = await resp.json();
  if (resp.status === 422 && body.structural_errors) {
    setStatus($("status"), "", "");
    showStructuralErrors(body.structural_errors);
    return;
  }
  if (!resp.ok) {
    setStatus($("status"), body.detail || "解析失败", "error");
    return;
  }
  state.jobId = body.job_id;
  state.filename = body.filename;
  state.chapters = body.chapters;
  state.links = body.links;
  state.stats = body.stats;
  state.decisions = {};
  setStatus($("status"), "解析完成。", "ok");
  render();
}

function showStructuralErrors(list) {
  const ul = $("errors-list");
  ul.innerHTML = "";
  for (const e of list) {
    const li = document.createElement("li");
    li.textContent = `${e.location || ""}${e.message}`;
    ul.appendChild(li);
  }
  $("errors-card").hidden = false;
}

function render() {
  $("book-name").textContent =
    `${state.filename} · OPF 结构正常（${state.stats.documents} 篇 XHTML）`;
  const s = state.stats;
  $("stats").innerHTML =
    `<span>校验链接 <b>${s.checked_links}</b></span>` +
    `<span>失效 <b style="color:var(--danger)">${s.broken_links}</b></span>` +
    `<span>外部链接（不改） <b>${s.external_links}</b></span>` +
    (s.unlocatable_links
      ? `<span>无法定位原文字节 <b style="color:var(--danger)">${s.unlocatable_links}</b></span>`
      : "");

  const tb = $("links-body");
  tb.innerHTML = "";
  if (state.links.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 5;
    td.className = "muted";
    td.textContent = "未发现失效跳转，可以直接导出（导出内容与原书一致）。";
    tr.appendChild(td);
    tb.appendChild(tr);
  } else {
    state.links.forEach((lk, i) => tb.appendChild(row(lk, i)));
  }

  $("result-card").hidden = false;
  $("export-card").hidden = false;
  updateExportHint();
}

function chapterOptions(selectedFile) {
  return state.chapters.map((ch) => {
    const label = `${ch.title}${ch.in_spine ? "" : "（非 spine）"} — ${ch.path}`;
    const o = document.createElement("option");
    o.value = ch.path;
    o.textContent = label;
    if (ch.path === selectedFile) o.selected = true;
    return o;
  });
}

function idOptions(ch, selectedId) {
  const frag = document.createDocumentFragment();
  const top = document.createElement("option");
  top.value = "";
  top.textContent = "— 章节顶部 —";
  if (!selectedId) top.selected = true;
  frag.appendChild(top);
  ch.ids.forEach((d) => {
    const o = document.createElement("option");
    o.value = d.id;
    o.textContent = `#${d.id}（第 ${d.line} 行 <${d.tag}>）${d.snippet ? " " + d.snippet.slice(0, 40) : ""}`;
    if (d.id === selectedId) o.selected = true;
    frag.appendChild(o);
  });
  return frag;
}

function row(lk, i) {
  const tr = document.createElement("tr");

  const tdN = document.createElement("td");
  tdN.textContent = String(i + 1);
  tr.appendChild(tdN);

  const tdLoc = document.createElement("td");
  tdLoc.className = "file-cell";
  tdLoc.textContent = `${lk.file}:${lk.line}`;
  if (!lk.locatable) {
    const b = document.createElement("span");
    b.className = "badge block";
    b.textContent = "无法定位";
    b.title = "解析出的链接无法唯一对应到原文属性位置，不能修改，禁止导出";
    tdLoc.appendChild(document.createElement("br"));
    tdLoc.appendChild(b);
  }
  tr.appendChild(tdLoc);

  const tdText = document.createElement("td");
  tdText.className = "row-text";
  tdText.textContent = lk.text || "（无文字）";
  tr.appendChild(tdText);

  const tdHref = document.createElement("td");
  const code = document.createElement("div");
  code.className = "href-code";
  code.textContent = lk.href || "（空 href）";
  tdHref.appendChild(code);
  const why = document.createElement("div");
  why.className = "muted";
  why.textContent = reasonOf(lk.reason);
  tdHref.appendChild(why);
  tr.appendChild(tdHref);

  const tdTarget = document.createElement("td");
  if (lk.locatable) {
    const fileSel = document.createElement("select");
    fileSel.append(...chapterOptions(lk.target_file || state.chapters[0]?.path));
    const idSel = document.createElement("select");
    const preview = document.createElement("div");
    preview.className = "preview";
    preview.hidden = true;
    const newHref = document.createElement("div");
    newHref.className = "new-href";

    const refresh = () => {
      const ch = state.chapters.find((c) => c.path === fileSel.value);
      idSel.innerHTML = "";
      idSel.appendChild(idOptions(ch, null));
      // 同篇且原片段能匹配现有 id 列表时预选，否则默认顶部
      updatePreview(preview, ch, idSel.value);
      state.decisions[String(lk.id)] = { file: fileSel.value, id: idSel.value };
      newHref.textContent = `新 href：${buildHrefPreview(lk, fileSel.value, idSel.value)}`;
    };
    fileSel.addEventListener("change", refresh);
    idSel.addEventListener("change", refresh);

    // 初始 id：同篇时优先保留原片段（若该 id 存在），否则顶部
    const initCh = state.chapters.find((c) => c.path === fileSel.value);
    let initId = "";
    if (lk.target_file && lk.fragment && initCh) {
      const decoded = decodeURIComponentSafe(lk.fragment);
      if (initCh.ids.some((d) => d.id === decoded)) initId = decoded;
    }
    idSel.appendChild(idOptions(initCh, initId));
    updatePreview(preview, initCh, initId);

    tdTarget.appendChild(fileSel);
    tdTarget.appendChild(idSel);
    tdTarget.appendChild(preview);
    tdTarget.appendChild(newHref);
    state.decisions[String(lk.id)] = { file: fileSel.value, id: initId };
    newHref.textContent = `新 href：${buildHrefPreview(lk, fileSel.value, initId)}`;

    fileSel.addEventListener("change", updateExportHint);
    idSel.addEventListener("change", updateExportHint);
  } else {
    tdTarget.classList.add("muted");
    tdTarget.textContent = "该链接无法唯一对应到原文属性位置，已禁止导出；请在源文件中修正后重新上传。";
  }
  tr.appendChild(tdTarget);
  return tr;
}

function updatePreview(el, ch, id) {
  if (!ch) { el.hidden = true; return; }
  if (!id) {
    el.textContent = `章节顶部 · ${ch.title}`;
    el.hidden = false;
    return;
  }
  const d = ch.ids.find((x) => x.id === id);
  el.textContent = d
    ? `#${d.id} 第 ${d.line} 行 <${d.tag}> ${d.snippet || "（无文字）"}`
    : "";
  el.hidden = !d;
}

// 与后端 urltools.build_href 保持同样编码规则的前端预览
function buildHrefPreview(lk, targetFile, id) {
  if (targetFile === lk.file) {
    return id ? `#${encodeURIComponentKeepPunct(id)}` : "";
  }
  const dir = lk.file.includes("/") ? lk.file.slice(0, lk.file.lastIndexOf("/")) : "";
  const from = dir ? dir.split("/") : [];
  const to = targetFile.split("/");
  let i = 0;
  while (i < from.length && i < to.length - 1 && from[i] === to[i]) i++;
  const parts = [];
  for (let k = i; k < from.length; k++) parts.push("..");
  parts.push(...to.slice(i));
  let rel = parts.join("/") || targetFile.split("/").pop();
  rel = rel.split("/").map(encodeURIComponentKeepPunct).join("/");
  return id ? `${rel}#${encodeURIComponentKeepPunct(id)}` : rel;
}
function encodeURIComponentKeepPunct(s) {
  // encodeURIComponent 对路径段常见保留字符多编码了，这里仅作预览，
  // 精确编码以后端为准。
  return encodeURIComponent(s).replace(/[!'()*]/g,
    (c) => "%" + c.charCodeAt(0).toString(16).toUpperCase());
}

function updateExportHint() {
  const total = state.links.length;
  const blocked = state.links.filter((l) => !l.locatable).length;
  const decided = Object.keys(state.decisions).length;
  $("export-btn").disabled = blocked > 0;
  $("export-hint").textContent = blocked
    ? `${blocked} 条链接无法定位到原文字节，无法导出（需先在源文件中修正）。`
    : `共 ${total} 条失效链接，已为全部 ${decided} 条选择目标。`;
}

$("export-btn").addEventListener("click", async () => {
  if (!state.jobId) return;
  setStatus($("export-status"), "正在生成并复检成品 …", "");
  const resp = await fetch(`/api/jobs/${state.jobId}/export`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decisions: state.decisions }),
  });
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    setStatus($("export-status"), body.detail || `导出被拒绝（HTTP ${resp.status}）`, "error");
    return;
  }
  const blob = await resp.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  const stem = state.filename.replace(/\.epub$/i, "") || "book";
  a.href = url;
  a.download = `${stem}.fixed.epub`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
  setStatus($("export-status"), "导出成功：成品已通过重扫，未发现失效跳转。", "ok");
});
