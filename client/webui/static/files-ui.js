/* 本机加密文件上传、列表和预览。 */
(function initClawFilesUI(global) {
  "use strict";
  function create({ state, api, $, esc, pickExistingCipher }) {
  function openFilesModal() {
    $("filesMask").classList.add("open");
    renderFilesModal();
  }
  function closeFilesModal() { $("filesMask").classList.remove("open"); }

  async function renderFilesModal() {
    await loadFiles();
    $("filesBody").innerHTML = `
      <h2>密文文件管理</h2>
      <p class="sub">已加密的本地文件 · 数字列加密 / 字符串列保留为身份标识</p>
      <div id="filesList"></div>

      <h3 class="section-heading">上传新数据</h3>
      <div class="sk-drop" id="fileDropZone" tabindex="0">
        <div class="sk-drop__t">点击或拖入 <strong>CSV / XLSX</strong> 数据文件</div>
        <div class="sk-drop__s" id="fileDropHint">系统会自动识别数字列加密、字符串列做身份标识</div>
        <input type="file" id="fileUpRaw" accept=".csv,.xlsx,.xls" hidden>
      </div>
      <div id="fileUpStatus" class="status-spaced"></div>
    `;
    renderFilesList();

    const zone = $("fileDropZone"), inp = $("fileUpRaw"), hint = $("fileDropHint");
    const setHint = (h) => hint.innerHTML = h;

    async function doUpload(file) {
      if (!file) return;
      if (!/\.(csv|xlsx|xls)$/i.test(file.name)) {
        $("fileUpStatus").innerHTML = `<div class="alert-box">仅支持 CSV / XLSX</div>`;
        return;
      }
      setHint(`正在加密 <strong>${esc(file.name)}</strong>…`);
      $("fileUpStatus").innerHTML = '<div class="alert-box info">加密中…</div>';
      const fd = new FormData(); fd.append("raw_file", file);
      try {
        const res = await api("POST", "/api/files/upload", fd, true);
        const enc = res.encrypted_columns || [], pt = res.plaintext_columns || [];
        const dh = res.data_health || {};
        const dhLine = (dh.message && dh.message !== "数据干净,无需清洗。")
          ? `<br><span class="af-s">数据体检:${esc(dh.message)}</span>` : "";
        const ff = res.formula_filled || [];
        const ffLine = ff.length
          ? `<br><span class="af-s">已按源表公式补算派生列:${esc(ff.join("、"))}</span>` : "";
        const msWarn = res.multi_sheet_warning
          ? `<br><span class="af-s" style="color:#b45309">${esc(res.multi_sheet_warning)}</span>` : "";
        $("fileUpStatus").innerHTML =
          `<div class="alert-box success">✓ 已加密入库:<strong>${esc(res.name)}</strong>
           <br>${enc.length} 列加密 · ${pt.length} 列身份标识 · ${res.row_count || "?"} 行${dhLine}${ffLine}${msWarn}</div>`;
        setHint("点击或拖入 <strong>CSV / XLSX</strong> 数据文件");
        inp.value = "";
        await loadFiles(); renderFilesList();
      } catch (e) {
        $("fileUpStatus").innerHTML = `<div class="alert-box">加密失败:${esc(e.message)}</div>`;
        setHint("点击或拖入 <strong>CSV / XLSX</strong> 数据文件");
      }
    }

    zone.addEventListener("click", () => inp.click());
    zone.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); inp.click(); }
    });
    inp.addEventListener("change", e => doUpload(e.target.files[0]));
    ["dragenter", "dragover"].forEach(ev =>
      zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.add("dragover"); })
    );
    ["dragleave", "drop"].forEach(ev =>
      zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.remove("dragover"); })
    );
    zone.addEventListener("drop", e => {
      if (e.dataTransfer.files && e.dataTransfer.files[0]) doUpload(e.dataTransfer.files[0]);
    });
  }

  async function loadFiles() {
    try { state.files = await api("GET", "/api/files"); } catch { state.files = []; }
  }

  function renderFilesList() {
    const box = $("filesList");
    if (!box) return;
    if (!state.files.length) {
      box.innerHTML = '<div class="alert-box info">本机还没有加密文件</div>';
      return;
    }
    box.innerHTML = state.files.map(f => `
      <div class="list-item">
        <div class="grow">
          <div class="t">${esc(f.name)}</div>
          <div class="d">${f.size_kb} KB · ${esc(f.mtime.slice(0, 19))}${f.has_meta ? ' · <span class="badge ok">meta</span>' : ""}</div>
        </div>
        <button class="btn-ghost btn-sm" data-pick="${esc(f.path)}" data-name="${esc(f.name)}">附给下条消息</button>
        <button class="btn-ghost btn-sm" data-view="${esc(f.name)}">查看</button>
        <button class="btn-danger" data-del="${esc(f.name)}">删除</button>
      </div>
    `).join("");

    box.querySelectorAll("[data-pick]").forEach(b => b.addEventListener("click", () => {
      pickExistingCipher(b.dataset.pick, b.dataset.name);
    }));
    box.querySelectorAll("[data-view]").forEach(b => b.addEventListener("click", () => {
      showFilePreview(b.dataset.view);
    }));
    box.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
      if (!confirm(`删除 ${b.dataset.del}?`)) return;
      await api("DELETE", `/api/files/${encodeURIComponent(b.dataset.del)}`);
      await loadFiles(); renderFilesList();
    }));
  }

  async function showFilePreview(name) {
    const wrap = document.createElement("div");
    wrap.className = "modal-mask open"; wrap.style.zIndex = 30;
    wrap.innerHTML = `
      <div class="modal preview-modal">
        <button class="modal__close" id="prevClose">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
        <div class="modal__body">
          <h2>${esc(name)}</h2>
          <p class="sub">密文文件不可读 · 这里展示同目录的 meta sidecar(明文身份列)+ 自动推断 schema</p>
          <div id="prevBody"><div class="alert-box info">加载中…</div></div>
        </div>
      </div>
    `;
    document.body.appendChild(wrap);
    wrap.querySelector("#prevClose").addEventListener("click", () => wrap.remove());
    wrap.addEventListener("click", e => { if (e.target === wrap) wrap.remove(); });

    try {
      const info = await api("GET", `/api/files/${encodeURIComponent(name)}/preview`);
      const body = wrap.querySelector("#prevBody");
      let html = `
        <div class="list-item"><div class="grow">
          <div class="t">文件信息</div>
          <div class="d">${esc(info.path)} · ${info.size_kb} KB</div>
        </div></div>`;
      if (info.schema && info.schema.columns) {
        const cols = info.schema.columns;
        const enc = cols.filter(c => c.encrypted).map(c => c.name);
        const pt = cols.filter(c => !c.encrypted).map(c => c.name);
        html += `
          <h3 class="section-heading compact">字段结构(共 ${cols.length} 列)</h3>
          <div class="list-item"><div class="grow">
            <div class="t">加密列(${enc.length})</div>
            <div class="d mono text-wrap">${enc.map(esc).join(" · ")}</div>
          </div></div>
          <div class="list-item"><div class="grow">
            <div class="t">身份列(${pt.length} · 明文)</div>
            <div class="d mono text-wrap">${pt.map(esc).join(" · ")}</div>
          </div></div>`;
      }
      if (info.meta_preview && info.meta_preview.length) {
        const cols = info.meta_columns || [];
        html += `
          <h3 class="section-heading compact">身份列预览(前 ${info.meta_preview.length} / 共 ${info.meta_row_count} 行)</h3>
          <div class="preview-table-wrap">
            <table class="usage-tbl preview-table">
              <thead><tr>${cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>
              <tbody>${info.meta_preview.map(row =>
                `<tr>${row.map(v => `<td>${esc(v)}</td>`).join("")}</tr>`).join("")}</tbody>
            </table>
          </div>`;
      }
      body.innerHTML = html;
    } catch (e) {
      wrap.querySelector("#prevBody").innerHTML =
        `<div class="alert-box">加载失败:${esc(e.message)}</div>`;
    }
  }

    return { openFilesModal, closeFilesModal, renderFilesModal, loadFiles, renderFilesList, showFilePreview };
  }
  global.ClawFilesUI = Object.freeze({ create });
}(window));
