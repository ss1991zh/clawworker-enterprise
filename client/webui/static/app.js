/* =========================================================
 * Clawworker Client v4 · skill-only 架构
 * - 会话侧栏(list / new / delete / switch)
 * - 主聊天区(消息 + 单行进度 + 轮询)
 * - 附件:本条消息附带一份密文(拖拽 / 点选)
 * - 设置 modal(连接 / 密文文件 / 同态密钥 / 账户)
 * ========================================================= */

const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const ICON_SVG = {
  doc: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  warn: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  ask: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
};

// ============ 状态 ============
const state = {
  sessions: [],
  currentSid: null,
  currentSession: null,
  pendingCipher: null,   // {name, path, size, uploading?}
  files: [],
};

// ============ API helpers ============
async function api(method, path, body, isMultipart = false) {
  const opts = { method, headers: {} };
  if (body) {
    if (isMultipart) opts.body = body;
    else { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  }
  const r = await fetch(path, opts);
  let bodyJson = null, bodyText = "";
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("application/json")) { try { bodyJson = await r.json(); } catch {} }
  else { try { bodyText = await r.text(); } catch {} }

  if (r.status === 401) {
    if (bodyJson && bodyJson.error === "not_logged_in") {
      window.location = "/login";
      throw new Error("unauthorized");
    }
    const detail = (bodyJson && (bodyJson.detail || bodyJson.message)) || bodyText || "401";
    throw new Error(detail);
  }
  if (!r.ok) {
    const detail = (bodyJson && (bodyJson.detail || bodyJson.message)) || bodyText || `${r.status}`;
    throw new Error(detail);
  }
  return bodyJson != null ? bodyJson : bodyText;
}

// ============ Sidebar ============
async function loadSessions() {
  state.sessions = await api("GET", "/api/sessions");
  renderSessionList();
}

function renderSessionList() {
  const el = $("sessionList");
  if (!state.sessions.length) {
    el.innerHTML = '<div class="session-empty">还没有会话<br>点击上方"新建会话"</div>';
    return;
  }
  el.innerHTML = state.sessions.map(s => `
    <div class="session ${s.id === state.currentSid ? "active" : ""}" data-id="${s.id}">
      <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <span class="session__title">${esc(s.title)}</span>
      <button class="session__del" data-del="${s.id}" title="删除会话">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
        </svg>
      </button>
    </div>
  `).join("");
  el.querySelectorAll(".session").forEach(node => {
    node.addEventListener("click", e => {
      if (e.target.closest("[data-del]")) return;
      selectSession(node.dataset.id);
    });
  });
  el.querySelectorAll("[data-del]").forEach(b => {
    b.addEventListener("click", async e => {
      e.stopPropagation();
      if (!confirm("删除这个会话?其消息记录会一并丢失。")) return;
      await api("DELETE", `/api/sessions/${b.dataset.del}`);
      if (b.dataset.del === state.currentSid) {
        state.currentSid = null; state.currentSession = null;
        showWelcome();
      }
      await loadSessions();
    });
  });
}

async function createSession() {
  const s = await api("POST", "/api/sessions");
  await loadSessions();
  await selectSession(s.id);
}

async function selectSession(sid) {
  state.currentSid = sid;
  const data = await api("GET", `/api/sessions/${sid}/messages`);
  state.currentSession = data.session;
  state.currentSession.messages = data.messages;
  renderSessionList();
  renderChat();
  enableComposer();
  $("sidebar").classList.remove("open");
  $("scrim").classList.remove("open");
  // 重新轮询所有 pending/running 的 assistant 消息(防主进程重启后丢轮询)
  (state.currentSession.messages || []).forEach(m => {
    if (m.role === "assistant" && (m.status === "pending" || m.status === "running")) {
      pollMessage(sid, m.id);
    }
  });
}

// ============ Chat 渲染 ============
function showWelcome() {
  $("chat").innerHTML = "";
  $("chat").appendChild(welcomeNode());
}

function welcomeNode() {
  const w = document.createElement("div");
  w.className = "welcome";
  w.innerHTML = `
    <div class="logo welcome-logo">爪</div>
    <div class="big">同态加密 · 数据分析助手</div>
    <div class="sub">明文不出本机 · 计算全程密文 · 输出 Excel 解密回本机</div>
    <div class="chips">
      <div class="chip">按大区统计销售目标完成率,排名 + 涂色,导 Excel</div>
      <div class="chip">月度回款率明细 + 大区汇总 + TOP10 / BOTTOM10</div>
      <div class="chip">算库存周转天数 + ABC 分类,标记呆滞物料</div>
      <div class="chip">客户分群:RFM 分箱后看高价值客户分布</div>
    </div>
    <p class="welcome-hint">点击左侧 <strong>新建会话</strong> 开始 · 或选一条已有会话继续</p>
  `;
  w.querySelectorAll(".chip").forEach(c => {
    c.addEventListener("click", async () => {
      if (!state.currentSid) await createSession();
      $("input").value = c.textContent;
      $("input").focus();
    });
  });
  return w;
}

function renderChat() {
  const chat = $("chat");
  chat.innerHTML = "";
  const msgs = state.currentSession?.messages || [];
  if (!msgs.length) { chat.appendChild(welcomeNode()); return; }
  msgs.forEach(m => chat.appendChild(renderMessage(m)));
  $("main").scrollTop = $("main").scrollHeight;
}

function renderMessage(m) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${m.role === "user" ? "user" : "bot"}`;
  wrap.dataset.mid = m.id;

  const avatar = `<div class="avatar">${m.role === "user" ? "我" : "爪"}</div>`;
  let content = `<div class="msg__content">`;

  if (m.role === "user") {
    content += `<div class="bubble">${esc(m.content).replace(/\n/g, "<br>")}</div>`;
    if (m.attached_cipher) {
      const nm = m.attached_cipher.split("/").pop();
      content += `<div class="att-line"><span class="att-piece">${ICON_SVG.doc} ${esc(nm)}</span></div>`;
    }
  } else {
    // ============ assistant ============
    const running = (m.status === "pending" || m.status === "running");
    const failed = (m.status === "failed");
    const needsCipher = (m.status === "needs_cipher");
    const steps = m.steps || [];

    // 进度行(running 时持续刷新;done 时也保留)
    if (steps.length) {
      content += `<div class="trace">`;
      steps.forEach(s => {
        const cls = s.kind || "step";
        content += `<div class="step ${cls}">${esc(s.label)}</div>`;
      });
      content += `</div>`;
    }

    if (running) {
      content += `
        <div class="run-pill">
          <svg class="spark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
            <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
          </svg>
          <span>${m.status === "pending" ? "排队中" : "推理中"}</span>
          <span class="run-time" data-since="${m.created_at}">0s</span>
        </div>`;
    } else if (failed) {
      content += `<div class="err-card">${ICON_SVG.warn}<span class="err-text">${esc(m.error || "未知错误")}</span></div>`;
      if (m.summary) {
        content += `<div class="bubble">${esc(m.summary).replace(/\n/g, "<br>")}</div>`;
      }
    } else if (needsCipher) {
      content += `<div class="ask-card">${ICON_SVG.ask}<span>${esc(m.summary || "请附一份已加密的数据文件")}</span></div>`;
    } else {
      // done
      content += `<div class="bubble">${esc(m.summary || "(无总结)").replace(/\n/g, "<br>")}</div>`;
      if (m.excel_path && m.excel_name) {
        const dlUrl = `/api/excel/download?path=${encodeURIComponent(m.excel_path)}`;
        content += `
          <a class="file-card" href="${dlUrl}" download="${esc(m.excel_name)}">
            <div class="fc-ic">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                <polyline points="14 2 14 8 20 8"/>
                <line x1="8" y1="13" x2="16" y2="13"/>
                <line x1="8" y1="17" x2="16" y2="17"/>
              </svg>
            </div>
            <div class="fc-body">
              <div class="fc-nm">${esc(m.excel_name)}</div>
              <div class="fc-hint">Excel 输出 · 点击下载</div>
            </div>
            <div class="fc-btn">⬇ 下载</div>
          </a>`;
      }
      // 底栏小信息:跑了哪些 skill + 用了哪份密文 + 时长
      const meta = [];
      if (m.skill_calls && m.skill_calls.length) meta.push(`skill: ${m.skill_calls.join(" · ")}`);
      if (m.used_cipher) meta.push(`密文: ${m.used_cipher.split("/").pop()}`);
      if (m.duration_sec) meta.push(`${m.duration_sec.toFixed(1)}s`);
      if (meta.length) content += `<div class="msg-meta">${esc(meta.join(" · "))}</div>`;
    }
  }
  content += `</div>`;
  wrap.innerHTML = avatar + content;
  return wrap;
}

// ============ 轮询 assistant 消息 ============
function pollMessage(sid, mid) {
  const since = Date.now();
  const intv = setInterval(async () => {
    try {
      const m = await api("GET", `/api/sessions/${sid}/messages/${mid}`);
      const el = document.querySelector(`.msg[data-mid="${mid}"] .run-time`);
      if (el) el.textContent = ((Date.now() - since) / 1000).toFixed(0) + "s";

      // 进度行实时刷新(即使还在 running)
      if (m.steps && m.steps.length) {
        const node = document.querySelector(`.msg[data-mid="${mid}"]`);
        const idx = state.currentSession?.messages?.findIndex(x => x.id === mid);
        if (idx >= 0) state.currentSession.messages[idx] = m;
        if (node) {
          const fresh = renderMessage(m);
          node.replaceWith(fresh);
          $("main").scrollTop = $("main").scrollHeight;
        }
      }

      if (m.status === "done" || m.status === "failed" || m.status === "needs_cipher") {
        clearInterval(intv);
        loadSessions();
      }
    } catch (e) {
      clearInterval(intv);
    }
  }, 1200);
}

// ============ 发送消息 ============
function enableComposer() {
  $("input").disabled = false;
  $("sendBtn").disabled = false;
  $("input").focus();
}

async function sendMessage() {
  const text = $("input").value.trim();
  if (!text) return;
  if (!state.currentSid) await createSession();

  // 还在上传 cipher 阻塞
  if (state.pendingCipher && state.pendingCipher.uploading) {
    alert("等密文加密完成后再发送");
    return;
  }

  const sendBtn = $("sendBtn");
  sendBtn.disabled = true;
  $("input").value = "";
  $("input").style.height = "auto";

  const attached_cipher = state.pendingCipher?.path || "";
  state.pendingCipher = null;
  renderAttachChips();

  try {
    const res = await api("POST", `/api/sessions/${state.currentSid}/messages`, {
      content: text, attached_cipher,
    });
    state.currentSession.messages.push(res.user_message, res.assistant_message);
    if (state.currentSession.messages.length === 2 && state.currentSession.title === "新会话") {
      state.currentSession.title = text.slice(0, 40);
    }
    renderChat();
    pollMessage(state.currentSid, res.assistant_message.id);
    loadSessions();
  } catch (e) {
    alert("发送失败:" + e.message);
  } finally {
    sendBtn.disabled = false;
    $("input").focus();
  }
}

// ============ 附件(单密文) ============
function renderAttachChips() {
  const box = $("attachChips");
  if (!state.pendingCipher) { box.innerHTML = ""; return; }
  const a = state.pendingCipher;
  box.innerHTML = `
    <div class="att-chip ${a.uploading ? "uploading" : ""}">
      <span class="nm">${esc(a.name)}</span>
      ${a.size ? `<span class="sz">${a.size}</span>` : ""}
      <button class="rm" data-rm="1">×</button>
    </div>
  `;
  box.querySelector("[data-rm]")?.addEventListener("click", () => {
    state.pendingCipher = null;
    renderAttachChips();
  });
}

async function handleFileAttach(file) {
  if (!file) return;
  const ext = (file.name.split(".").pop() || "").toLowerCase();
  // 已加密文件直接使用 / 原始数据先上传加密
  const isCipher = file.name.includes("_enc") || file.name.endsWith(".cipher");
  if (!["csv", "xlsx", "xls"].includes(ext)) {
    alert(`不支持的文件类型:.${ext} · 仅 CSV / XLSX`);
    return;
  }
  const chip = {
    name: file.name + " (加密中…)", uploading: true,
    size: (file.size / 1024).toFixed(1) + "KB",
  };
  state.pendingCipher = chip;
  renderAttachChips();
  const fd = new FormData();
  fd.append("raw_file", file);
  try {
    const res = await api("POST", "/api/files/upload", fd, true);
    chip.name = res.name; chip.path = res.path; chip.uploading = false;
    renderAttachChips();
    loadFiles();
  } catch (e) {
    state.pendingCipher = null;
    renderAttachChips();
    alert("加密失败:" + e.message);
  }
}

function pickExistingCipher(path, name) {
  state.pendingCipher = { name: name || path.split("/").pop(), path, uploading: false };
  renderAttachChips();
  closeModal();
}

// ============ 设置 Modal ============
const TABS = {
  general: { title: "连接 / 计算", render: renderGeneralTab },
  files:   { title: "密文文件管理", render: renderFilesTab },
  keys:    { title: "同态密钥", render: renderKeysTab },
  account: { title: "账户", render: renderAccountTab },
};
let currentTab = "general";

function openModal(tab) {
  currentTab = tab || "general";
  $("modalMask").classList.add("open");
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.classList.toggle("active", b.dataset.tab === currentTab);
  });
  TABS[currentTab].render();
}
function closeModal() { $("modalMask").classList.remove("open"); }

async function renderGeneralTab() {
  const cfg = await api("GET", "/api/config");
  $("modalBody").innerHTML = `
    <h2>${TABS.general.title}</h2>
    <p class="sub">主机地址 · HE backend</p>
    <div id="cfgAlert"></div>
    <div class="field">
      <label>主机地址</label>
      <input type="text" id="cfgHost" value="${esc(cfg.host_url)}">
      <p class="hint">修改后请登出 + 重新登录,新的 session 会走新主机</p>
    </div>
    <div class="field">
      <label>HE Backend</label>
      <select id="cfgBackend">
        <option value="stub" ${cfg.backend === "stub" ? "selected" : ""}>stub(测试)</option>
        <option value="real" ${cfg.backend === "real" ? "selected" : ""}>real(真实同态加密)</option>
      </select>
    </div>
    <button class="btn-primary" id="cfgSave">保存</button>
  `;
  $("cfgSave").addEventListener("click", async () => {
    try {
      await api("POST", "/api/config", {
        host_url: $("cfgHost").value, backend: $("cfgBackend").value,
      });
      $("cfgAlert").innerHTML = '<div class="alert-box success">已保存</div>';
    } catch (e) {
      $("cfgAlert").innerHTML = `<div class="alert-box">保存失败:${esc(e.message)}</div>`;
    }
  });
}

async function renderFilesTab() {
  await loadFiles();
  $("modalBody").innerHTML = `
    <h2>${TABS.files.title}</h2>
    <p class="sub">已加密的本地文件 · 数字列加密 / 字符串列保留为身份标识</p>
    <div id="filesList"></div>

    <h3 style="font-size:14px; margin: 22px 0 10px;">上传新数据</h3>
    <div class="sk-drop" id="fileDropZone" tabindex="0">
      <div class="sk-drop__t">点击或拖入 <strong>CSV / XLSX</strong> 数据文件</div>
      <div class="sk-drop__s" id="fileDropHint">系统会自动识别数字列加密、字符串列做身份标识</div>
      <input type="file" id="fileUpRaw" accept=".csv,.xlsx,.xls" hidden>
    </div>
    <div id="fileUpStatus" style="margin-top:12px;"></div>
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
      $("fileUpStatus").innerHTML =
        `<div class="alert-box success">✓ 已加密入库:<strong>${esc(res.name)}</strong>
         <br>${enc.length} 列加密 · ${pt.length} 列身份标识 · ${res.row_count || "?"} 行</div>`;
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
    <div class="modal" style="position:relative; max-width:840px; height:auto; max-height:80vh;">
      <button class="modal__close" id="prevClose">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
      <div class="modal__body" style="padding:24px 30px;">
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
        <h3 style="font-size:14px; margin:18px 0 8px;">字段结构(共 ${cols.length} 列)</h3>
        <div class="list-item"><div class="grow">
          <div class="t">加密列(${enc.length})</div>
          <div class="d mono" style="white-space:normal;">${enc.map(esc).join(" · ")}</div>
        </div></div>
        <div class="list-item"><div class="grow">
          <div class="t">身份列(${pt.length} · 明文)</div>
          <div class="d mono" style="white-space:normal;">${pt.map(esc).join(" · ")}</div>
        </div></div>`;
    }
    if (info.meta_preview && info.meta_preview.length) {
      const cols = info.meta_columns || [];
      html += `
        <h3 style="font-size:14px; margin:18px 0 8px;">身份列预览(前 ${info.meta_preview.length} / 共 ${info.meta_row_count} 行)</h3>
        <div style="overflow-x:auto; border:1px solid var(--border); border-radius:10px;">
          <table class="usage-tbl" style="font-size:12px; margin:0;">
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

async function renderKeysTab() {
  const k = await api("GET", "/api/keys");
  const sizeKb = (p) => p ? "(已沙盒化)" : "—";
  $("modalBody").innerHTML = `
    <h2>${TABS.keys.title}</h2>
    <p class="sub">本机沙盒 · sk / evk 永不出本机 · user_authorization 由主机签发</p>

    <h3 class="keys-h3">解密密钥 <span class="mono-tag">sk</span></h3>
    <div class="key-row">
      <div class="key-meta">${k.sk_present
        ? `<span class="badge ok">已导入</span>`
        : `<span class="badge no">未导入</span>`}</div>
    </div>
    <div class="sk-drop" id="dropSk">
      <div class="sk-drop__t"><strong>拖入</strong> 或 <span class="sk-pick" data-pick="sk">点击选择</span> sk 文件</div>
      <div class="sk-drop__s">永不上传主机 · 仅本机沙盒读取</div>
      <input type="file" id="skFile" hidden>
    </div>

    <h3 class="keys-h3">计算密钥 <span class="mono-tag">evk</span></h3>
    <div class="key-row">
      <div class="key-meta">${k.evk_present
        ? `<span class="badge ok">已导入</span>`
        : `<span class="badge no">未导入</span>`}</div>
    </div>
    <div class="sk-drop" id="dropEvk">
      <div class="sk-drop__t"><strong>拖入</strong> 或 <span class="sk-pick" data-pick="evk">点击选择</span> evk 文件</div>
      <div class="sk-drop__s">用于 HE 加法 / 乘法 · 不暴露明文</div>
      <input type="file" id="evkFile" hidden>
    </div>

    <h3 class="keys-h3">用户授权 <span class="mono-tag">user_authorization</span></h3>
    <div class="key-row">
      <div class="key-meta">${k.user_auth_present
        ? `<span class="badge ok">已同步</span>`
        : `<span class="badge warn">未获取</span>`}</div>
    </div>
    <div class="auth-fetch">
      <div class="af-body">
        <div class="af-t">证书由 admin 统一签发 · 客户端从主机自动同步</div>
        <div class="af-s">用户不上传 · admin 吊销后客户端 init 会失败</div>
      </div>
      <button class="btn-primary" id="fetchAuthBtn">${k.user_auth_present ? "重新拉取" : "从主机获取"}</button>
    </div>

    <div id="kStatus" style="margin-top:14px;"></div>
  `;

  bindKeyDrop("dropSk", "skFile", "sk", "/api/keys/sk");
  bindKeyDrop("dropEvk", "evkFile", "evk", "/api/keys/evk");

  $("fetchAuthBtn").addEventListener("click", async () => {
    $("kStatus").innerHTML = '<div class="alert-box info">正在从主机拉取证书…</div>';
    try {
      const res = await api("POST", "/api/keys/fetch_auth");
      $("kStatus").innerHTML = `<div class="alert-box success">✓ 已同步(${(res.size_bytes / 1024).toFixed(1)} KB)</div>`;
      setTimeout(renderKeysTab, 600);
    } catch (e) {
      $("kStatus").innerHTML = `<div class="alert-box">同步失败:${esc(e.message)}</div>`;
    }
  });
}

function bindKeyDrop(zoneId, inputId, label, endpoint) {
  const zone = $(zoneId), inp = $(inputId);
  if (!zone || !inp) return;
  async function upload(file) {
    const fd = new FormData(); fd.append("file", file);
    $("kStatus").innerHTML = `<div class="alert-box info">${esc(label)} 写入沙盒中…</div>`;
    try {
      const res = await api("POST", endpoint, fd, true);
      $("kStatus").innerHTML = `<div class="alert-box success">✓ ${esc(label)} 已写入沙盒(${(res.size_bytes / 1024).toFixed(1)} KB)</div>`;
      setTimeout(renderKeysTab, 600);
    } catch (e) {
      $("kStatus").innerHTML = `<div class="alert-box">${esc(label)} 上传失败:${esc(e.message)}</div>`;
    }
  }
  inp.addEventListener("change", e => { if (e.target.files[0]) upload(e.target.files[0]); });
  zone.querySelectorAll("[data-pick]").forEach(p =>
    p.addEventListener("click", e => { e.stopPropagation(); inp.click(); })
  );
  zone.addEventListener("click", e => {
    if (e.target.closest("button, .sk-pick, a")) return;
    inp.click();
  });
  ["dragover", "dragenter"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.add("dragover"); })
  );
  ["dragleave", "dragend"].forEach(ev =>
    zone.addEventListener(ev, () => zone.classList.remove("dragover"))
  );
  zone.addEventListener("drop", e => {
    e.preventDefault(); e.stopPropagation(); zone.classList.remove("dragover");
    if (e.dataTransfer.files && e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]);
  });
}

async function renderAccountTab() {
  const me = await api("GET", "/api/me");
  $("modalBody").innerHTML = `
    <h2>${TABS.account.title}</h2>
    <p class="sub">登录信息 · 凭据过期时间</p>
    <div class="list-item"><div class="grow"><div class="t">用户名</div><div class="d">${esc(me.username)}</div></div></div>
    <div class="list-item"><div class="grow"><div class="t">主机</div><div class="d">${esc(me.host_url)}</div></div></div>
    <div class="list-item"><div class="grow"><div class="t">凭据有效期</div><div class="d">${esc(me.expires_at || '—')}</div></div></div>
    <button class="btn-danger" id="logoutBtn" style="margin-top:16px;">退出登录</button>
  `;
  $("logoutBtn").addEventListener("click", async () => {
    if (!confirm("退出登录?当前会话会保留,下次登录后仍可看到。")) return;
    await api("POST", "/logout");
    window.location = "/login";
  });
}

// ============ 事件绑定 ============
function bindEvents() {
  $("newBtn").addEventListener("click", createSession);
  $("sendBtn").addEventListener("click", sendMessage);
  $("input").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $("input").addEventListener("input", e => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(140, e.target.scrollHeight) + "px";
  });

  $("attachBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", e => handleFileAttach(e.target.files?.[0]));

  // 拖拽到 composer overlay
  const modalOpen = () => $("modalMask").classList.contains("open");
  let dragCounter = 0;
  document.addEventListener("dragenter", e => {
    if (modalOpen()) return;
    e.preventDefault(); dragCounter++;
    if (e.dataTransfer.types.includes("Files")) $("dropOverlay").classList.add("show");
  });
  document.addEventListener("dragleave", () => {
    if (modalOpen()) return;
    dragCounter--; if (dragCounter <= 0) { dragCounter = 0; $("dropOverlay").classList.remove("show"); }
  });
  document.addEventListener("dragover", e => { if (!modalOpen()) e.preventDefault(); });
  document.addEventListener("drop", e => {
    if (modalOpen()) return;
    e.preventDefault(); dragCounter = 0;
    $("dropOverlay").classList.remove("show");
    if (e.dataTransfer.files && e.dataTransfer.files[0]) handleFileAttach(e.dataTransfer.files[0]);
  });

  // settings
  $("settingsBtn").addEventListener("click", () => openModal("general"));
  $("modalClose").addEventListener("click", closeModal);
  $("modalMask").addEventListener("click", e => {
    if (e.target === $("modalMask")) closeModal();
  });
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.addEventListener("click", () => openModal(b.dataset.tab));
  });

  // mobile menu
  $("menuBtn")?.addEventListener("click", () => {
    $("sidebar").classList.add("open");
    $("scrim").classList.add("open");
  });
  $("scrim")?.addEventListener("click", () => {
    $("sidebar").classList.remove("open");
    $("scrim").classList.remove("open");
  });
}

// ============ Boot ============
(async function init() {
  bindEvents();
  await loadSessions();
  await loadFiles();
  showWelcome();
  if (state.sessions.length) await selectSession(state.sessions[0].id);
})();
