/* =========================================================
 * Clawworker Client · ChatGPT 风格单页 app
 * - 会话侧栏(list / new / delete / switch)
 * - 主聊天区(message 渲染 + 自动 polling)
 * - 附件(数据/schema 拖拽 + 点选)
 * - 设置 modal(5 tabs)
 * ========================================================= */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

// 把消息文本里出现的 emoji(📎 / 📄 / 📊 / 🧬 / 📥 等)替换成扁平 SVG,保持简约。
const ICON_SVG = {
  clip:   '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  doc:    '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/></svg>',
  schema: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/></svg>',
  warn:   '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
  upload: '<svg class="ic-inline" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>',
};

// 文本里出现的 emoji → 扁平 SVG
function withFlatIcons(escapedHtml) {
  return escapedHtml
    .replace(/📎/g, ICON_SVG.clip)
    .replace(/📄/g, ICON_SVG.doc)
    .replace(/🧬/g, ICON_SVG.schema)
    .replace(/⚠[️]?/g, ICON_SVG.warn)
    .replace(/📥/g, ICON_SVG.upload);
}

// ============ 状态 ============
const state = {
  sessions: [],          // sidebar 列表(精简)
  currentSid: null,      // 当前会话 id
  currentSession: null,  // 完整对象(包含 messages + context)
  pendingAttachments: [], // 待发送的附件 [{kind, name, path?, content?}]
  files: [],             // 本地密文文件列表
};

// ============ API helpers ============
async function api(method, path, body, isMultipart = false) {
  const opts = { method, headers: {} };
  if (body) {
    if (isMultipart) {
      opts.body = body; // FormData
    } else {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
  }
  const r = await fetch(path, opts);

  // 解析 body(error case 也需要,用来判断是否真的是"客户端未登录")
  let bodyJson = null;
  let bodyText = "";
  const ct = r.headers.get("content-type") || "";
  if (ct.includes("application/json")) {
    try { bodyJson = await r.json(); } catch {}
  } else {
    try { bodyText = await r.text(); } catch {}
  }

  if (r.status === 401) {
    // 只有客户端自己识别出"未登录"才跳 login;否则当普通错误抛出
    // 这样防止"下游主机 401"误把用户踢回登录页
    if (bodyJson && bodyJson.error === "not_logged_in") {
      window.location = "/login";
      throw new Error("unauthorized");
    }
    // 其他 401 当下游错误处理
    const detail = (bodyJson && (bodyJson.detail || bodyJson.message)) || bodyText || "401";
    throw new Error(detail);
  }

  if (!r.ok) {
    const detail = (bodyJson && (bodyJson.detail || bodyJson.message)) || bodyText || `${r.status}`;
    throw new Error(detail);
  }

  return bodyJson != null ? bodyJson : bodyText;
}

// ============ Sidebar:会话 ============
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
  updateCtxBar();
  enableComposer();
  // mobile: close sidebar
  $("sidebar").classList.remove("open");
  $("scrim").classList.remove("open");
}

// ============ Chat 渲染 ============
function showWelcome() {
  $("chat").innerHTML = "";
  const w = document.createElement("div");
  w.className = "welcome";
  w.id = "welcome";
  w.innerHTML = $("chat").dataset.welcomeHtml || "";
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
      <div class="chip">按大区统计销售目标完成率,完成档位涂色,导 Excel</div>
      <div class="chip">用 ARIMA + EWMA 预测未来 12 个月销量,看产品线</div>
      <div class="chip">算库存周转天数 + ABC 分类,标记呆滞物料</div>
      <div class="chip">月度回款率 TOP10 / BOTTOM10 排行</div>
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
  if (!msgs.length) {
    chat.appendChild(welcomeNode());
    return;
  }
  msgs.forEach(m => chat.appendChild(renderMessage(m)));
  chat.scrollIntoView({ block: "end" });
  $("main").scrollTop = $("main").scrollHeight;
}

function renderMessage(m) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${m.role === "user" ? "user" : "bot"}`;
  wrap.dataset.mid = m.id;

  let avatar = `<div class="avatar">${m.role === "user" ? "我" : "爪"}</div>`;
  let content = `<div class="msg__content">`;

  if (m.role === "user") {
    content += `<div class="bubble">${esc(m.content)}</div>`;
    if (m.attachments && m.attachments.length) {
      content += `<div class="att-line">`;
      m.attachments.forEach(a => {
        const ic = `<svg class="ic-tiny" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/></svg>`;
        content += `<span class="att-piece">${ic} ${esc(a.name || a.kind)}</span>`;
      });
      content += `</div>`;
    }
  } else {
    // assistant
    if (m.status === "pending" || m.status === "running") {
      content += `
        <div class="run-pill">
          <svg class="spark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
            <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
          </svg>
          <span>${m.status === "pending" ? "排队中" : "推理中"}</span>
          <span class="run-time" data-since="${m.created_at}">0s</span>
        </div>`;
    } else if (m.status === "failed") {
      if (m.scenario || m.plan_summary) {
        content += `<div class="trace"><details><summary>计算追踪</summary>
          <div class="step">场景: ${esc(m.scenario || "—")}</div>
          <div class="step">步骤: ${esc(m.plan_summary || "—")}</div>
        </details></div>`;
      }
      content += `<div class="err-card">${ICON_SVG.warn}<span class="err-text">${withFlatIcons(esc(m.error || "未知错误"))}</span></div>`;
    } else {
      // done
      if (m.scenario || m.plan_summary) {
        content += `<div class="trace"><details><summary>计算追踪 · ${esc(m.scenario || "—")} · ${(m.duration_sec || 0).toFixed(1)}s</summary>
          <div class="step">${esc(m.plan_summary || "(无 op 列表)")}</div>
        </details></div>`;
      }
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
    }
  }
  content += `</div>`;
  wrap.innerHTML = avatar + content;
  return wrap;
}

// ============ Polling assistant message ============
function pollMessage(sid, mid) {
  const since = Date.now();
  const intv = setInterval(async () => {
    try {
      const m = await api("GET", `/api/sessions/${sid}/messages/${mid}`);
      // 更新计时
      const el = document.querySelector(`.msg[data-mid="${mid}"] .run-time`);
      if (el) el.textContent = ((Date.now() - since) / 1000).toFixed(0) + "s";
      if (m.status === "done" || m.status === "failed") {
        clearInterval(intv);
        // 替换该消息节点
        const node = document.querySelector(`.msg[data-mid="${mid}"]`);
        if (node) {
          // 更新 state
          const idx = state.currentSession.messages.findIndex(x => x.id === mid);
          if (idx >= 0) state.currentSession.messages[idx] = m;
          const fresh = renderMessage(m);
          node.replaceWith(fresh);
          $("main").scrollTop = $("main").scrollHeight;
          loadSessions();  // 刷 sidebar 时间
        }
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
function disableComposer() {
  $("input").disabled = true;
  $("sendBtn").disabled = true;
}

async function sendMessage() {
  const text = $("input").value.trim();
  if (!text) return;
  if (!state.currentSid) {
    await createSession();
  }

  // 1) 把附件里的密文落到 session.context(schema 由服务端从旁挂自动加载)
  let ctx = {};
  for (const a of state.pendingAttachments) {
    if (a.kind === "ciphertext" && a.path) ctx.ciphertext = a.path;
  }

  // 2) 发送
  const sendBtn = $("sendBtn");
  sendBtn.disabled = true;
  $("input").value = "";
  $("input").style.height = "auto";

  const attachments = state.pendingAttachments.map(a => ({
    kind: a.kind, name: a.name, path: a.path || "",
  }));
  state.pendingAttachments = [];
  renderAttachChips();

  try {
    const res = await api("POST", `/api/sessions/${state.currentSid}/messages`, {
      content: text, attachments, context: ctx,
    });
    state.currentSession.messages.push(res.user_message, res.assistant_message);
    if (state.currentSession.messages.length === 2 && state.currentSession.title === "新会话") {
      state.currentSession.title = text.slice(0, 40);
    }
    if (ctx.ciphertext) state.currentSession.context_ciphertext = ctx.ciphertext;
    if (ctx.ciphertext) state.currentSession.context_ciphertext_name = ctx.ciphertext.split("/").pop();
    if (ctx.schema) state.currentSession.context_schema = ctx.schema;
    updateCtxBar();
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

// ============ 附件 ============
function renderAttachChips() {
  const box = $("attachChips");
  box.innerHTML = state.pendingAttachments.map((a, i) => `
    <div class="att-chip ${a.uploading ? "uploading" : ""}">
      <span class="nm">${esc(a.name)}</span>
      ${a.size ? `<span class="sz">${a.size}</span>` : ""}
      <button class="rm" data-rm="${i}">×</button>
    </div>
  `).join("");
  box.querySelectorAll("[data-rm]").forEach(b => {
    b.addEventListener("click", () => {
      state.pendingAttachments.splice(+b.dataset.rm, 1);
      renderAttachChips();
    });
  });
}

async function handleFileAttach(files) {
  for (const f of files) {
    const ext = (f.name.split(".").pop() || "").toLowerCase();
    if (!["csv", "xlsx", "xls"].includes(ext)) {
      alert(`不支持的文件类型:.${ext} · 仅 CSV / XLSX`);
      continue;
    }
    // 上传 + 加密(schema 在服务端自动推断,无需用户介入)
    const chip = {
      kind: "ciphertext", name: f.name + " (加密中…)", uploading: true,
      size: (f.size / 1024).toFixed(1) + "KB",
    };
    state.pendingAttachments.push(chip);
    renderAttachChips();
    const fd = new FormData();
    fd.append("raw_file", f);
    try {
      const res = await api("POST", "/api/files/upload", fd, true);
      chip.name = res.name;
      chip.path = res.path;
      chip.uploading = false;
      renderAttachChips();
      loadFiles();
    } catch (e) {
      const idx = state.pendingAttachments.indexOf(chip);
      if (idx >= 0) state.pendingAttachments.splice(idx, 1);
      renderAttachChips();
      alert("加密失败:" + e.message);
    }
  }
}

// ============ 上下文条(仅密文 · schema 自动从旁挂加载)============
function updateCtxBar() {
  const bar = $("ctxBar");
  if (!state.currentSession) { bar.style.display = "none"; return; }
  bar.style.display = "flex";

  const cipher = state.currentSession.context_ciphertext_name || "";
  const cChip = $("ctxCipher");
  cChip.classList.toggle("set", !!cipher);
  cChip.querySelector(".ctx-name").textContent = cipher || "未绑定密文 · 点击选择";
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
    <p class="sub">主机地址 · HE backend · 解密授权策略</p>
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
    <div class="field">
      <label><input type="checkbox" id="cfgAuto" ${cfg.auto_approve ? "checked" : ""}> 解密自动通过(B6-1)</label>
      <p class="hint">Web UI 无 stdin 交互,默认勾上;关闭则需未来加入审批弹窗</p>
    </div>
    <button class="btn-primary" id="cfgSave">保存</button>
  `;
  $("cfgSave").addEventListener("click", async () => {
    try {
      await api("POST", "/api/config", {
        host_url: $("cfgHost").value,
        backend: $("cfgBackend").value,
        auto_approve: $("cfgAuto").checked,
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
    <p class="sub">已加密入库的文件 · 数字列自动加密 · 字符串列(姓名/大区/月份)自动保留为明文身份标识</p>
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

  const zone = $("fileDropZone");
  const inp = $("fileUpRaw");
  const hint = $("fileDropHint");

  function setHint(html) { hint.innerHTML = html; }

  async function doUpload(file) {
    if (!file) return;
    if (!/\.(csv|xlsx|xls)$/i.test(file.name)) {
      $("fileUpStatus").innerHTML = `<div class="alert-box">仅支持 CSV / XLSX</div>`;
      return;
    }
    setHint(`正在加密 <strong>${esc(file.name)}</strong>…`);
    $("fileUpStatus").innerHTML = '<div class="alert-box info">加密中(数字列 → 密文,字符串列 → 明文身份)…</div>';
    const fd = new FormData();
    fd.append("raw_file", file);
    try {
      const res = await api("POST", "/api/files/upload", fd, true);
      const enc = res.encrypted_columns || [];
      const pt  = res.plaintext_columns || [];
      const cols = res.column_preview || [];
      const sheet = res.sheet_name && res.sheet_name !== "csv" ? `sheet「${esc(res.sheet_name)}」` : "";
      const headerHint = (res.header_row || 0) > 0 ? `(从第 ${res.header_row + 1} 行开始识别表头)` : "";
      $("fileUpStatus").innerHTML =
        `<div class="alert-box success">✓ 已加密入库:<strong>${esc(res.name)}</strong>` +
        `<br>${enc.length} 列加密 · ${pt.length} 列身份标识 · ${res.row_count || "?"} 行 · ${sheet} ${headerHint}` +
        `<br>识别到的列:<span class="mono" style="font-size:11px;">${cols.map(esc).join(", ")}</span></div>`;
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
      <button class="btn-ghost btn-sm" data-use="${esc(f.path)}">用于当前会话</button>
      <button class="btn-danger" data-del="${esc(f.name)}">删除</button>
    </div>
  `).join("");
  box.querySelectorAll("[data-use]").forEach(b => b.addEventListener("click", () => {
    pickCipherForSession(b.dataset.use);
  }));
  box.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm(`删除 ${b.dataset.del}?`)) return;
    await api("DELETE", `/api/files/${encodeURIComponent(b.dataset.del)}`);
    await loadFiles(); renderFilesList();
  }));
}

async function pickCipherForSession(path) {
  if (!state.currentSid) { await createSession(); }
  await api("POST", `/api/sessions/${state.currentSid}/context`, { ciphertext: path });
  state.currentSession.context_ciphertext = path;
  state.currentSession.context_ciphertext_name = path.split("/").pop();
  updateCtxBar();
  closeModal();
}

async function renderKeysTab() {
  const k = await api("GET", "/api/keys");
  const sb = k.sandbox || {};
  const sbHealthy = sb.root_ok && sb.vault_ok && sb.files_ok;
  const sizeKb = (n) => n ? (n / 1024).toFixed(1) + " KB" : "—";

  $("modalBody").innerHTML = `
    <h2>${TABS.keys.title}</h2>
    <p class="sub">本机沙盒 · 0700 目录 / 0600 文件 · 永不出网</p>

    <!-- 沙盒状态条 -->
    <div class="vault-banner ${sbHealthy ? "ok" : "warn"}">
      <div class="vb-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
          <rect x="3" y="11" width="18" height="11" rx="2"/>
          <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
        </svg>
      </div>
      <div class="vb-body">
        <div class="vb-title">沙盒目录 · ${sbHealthy ? "✓ 健康" : "⚠ 权限异常"}</div>
        <div class="vb-path mono">${esc(k.vault_path || "")}</div>
        <div class="vb-meta">
          目录权限 ${esc(sb.vault_mode || "?")} · 仅当前 macOS 用户可读 · 防其他用户 / 进程窃取
        </div>
      </div>
    </div>

    <!-- 三把密钥状态卡 + 操作 -->
    <h3 class="keys-h3">解密密钥 <span class="mono-tag">sk</span></h3>
    <div class="key-row">
      <div class="key-meta">
        ${k.sk_present
          ? `<span class="badge ok">已沙盒化</span>
             <span class="key-size mono">${sizeKb(k.sk_size)}</span>`
          : `<span class="badge no">未导入</span>`}
      </div>
      ${k.sk_present
        ? `<button class="btn-danger" data-del-key="sk">清除</button>`
        : ""}
    </div>
    <div class="sk-drop" id="dropSk" data-which="sk">
      <div class="sk-drop__ic">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="32" height="32">
          <circle cx="12" cy="16" r="1"/><rect x="3" y="10" width="18" height="12" rx="2"/>
          <path d="M7 10V6a5 5 0 0 1 10 0v4"/>
        </svg>
      </div>
      <div class="sk-drop__t"><strong>拖入</strong> 或 <span class="sk-pick" data-pick="sk">点击选择</span> sk 文件</div>
      <div class="sk-drop__s">永不上传主机 · 仅本机沙盒读取</div>
      <input type="file" id="skFile" hidden>
    </div>

    <h3 class="keys-h3">计算密钥 <span class="mono-tag">evk</span></h3>
    <div class="key-row">
      <div class="key-meta">
        ${k.evk_present
          ? `<span class="badge ok">已沙盒化</span>
             <span class="key-size mono">${sizeKb(k.evk_size)}</span>`
          : `<span class="badge no">未导入</span>`}
      </div>
      ${k.evk_present
        ? `<button class="btn-danger" data-del-key="evk">清除</button>`
        : ""}
    </div>
    <div class="sk-drop" id="dropEvk" data-which="evk">
      <div class="sk-drop__ic">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" width="32" height="32">
          <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5m0 0l3 3L22 7l-3-3m-3.5 3.5L19 4"/>
        </svg>
      </div>
      <div class="sk-drop__t"><strong>拖入</strong> 或 <span class="sk-pick" data-pick="evk">点击选择</span> evk 文件</div>
      <div class="sk-drop__s">用于 HE 加法 / 乘法 · 不暴露明文</div>
      <input type="file" id="evkFile" hidden>
    </div>

    <h3 class="keys-h3">用户授权 <span class="mono-tag">user_authorization</span></h3>
    <div class="key-row">
      <div class="key-meta">
        ${k.user_auth_present
          ? `<span class="badge ok">已沙盒化</span>
             <span class="key-size mono">${sizeKb(k.user_auth_size)}</span>`
          : `<span class="badge warn">未获取</span>`}
      </div>
      ${k.user_auth_present
        ? `<button class="btn-danger" data-del-key="user_authorization">清除</button>`
        : ""}
    </div>
    <div class="auth-fetch">
      <div class="af-body">
        <div class="af-t">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 12a9 9 0 1 1-6.219-8.56"/>
          </svg>
          证书由 admin 统一签发 · 客户端从主机自动同步
        </div>
        <div class="af-s">用户不上传 · 防止泄漏外流 · admin 吊销后客户端 init 会失败</div>
      </div>
      <button class="btn-primary" id="fetchAuthBtn">${k.user_auth_present ? "重新拉取" : "从主机获取"}</button>
    </div>

    <div id="kStatus" style="margin-top:14px;"></div>

    <!-- 安全说明 -->
    <details class="sec-note" style="margin-top:18px;">
      <summary>沙盒安全说明</summary>
      <ul>
        <li>密钥目录 <span class="mono">~/.agent-system/keystore/&lt;user&gt;/vault/</span> 权限 <span class="mono">0700</span></li>
        <li>每个文件 <span class="mono">0600</span> · 仅当前 OS 用户可读</li>
        <li>原子写入(.tmp → rename),拒绝半成品读取</li>
        <li>路径解析强制 resolve(),拒绝 symlink 跳出沙盒</li>
        <li>主进程不主动把密钥发到任何网络出口(<span class="mono">grep</span> 验证过)</li>
        <li>生产升级路径:macOS Keychain / Secure Enclave / Linux TPM</li>
      </ul>
    </details>
  `;

  // 绑定拖拽 / 点选
  bindKeyDrop("dropSk", "skFile", "sk", "/api/keys/sk");
  bindKeyDrop("dropEvk", "evkFile", "evk", "/api/keys/evk");

  // 从主机获取证书
  $("fetchAuthBtn").addEventListener("click", async () => {
    $("kStatus").innerHTML = '<div class="alert-box info">正在从主机拉取证书…</div>';
    try {
      const res = await api("POST", "/api/keys/fetch_auth");
      $("kStatus").innerHTML = `<div class="alert-box success">✓ 已同步到沙盒(${(res.size_bytes / 1024).toFixed(1)} KB)</div>`;
      setTimeout(renderKeysTab, 600);
    } catch (e) {
      $("kStatus").innerHTML = `<div class="alert-box">同步失败:${esc(e.message)}</div>`;
    }
  });

  // 清除按钮
  document.querySelectorAll("[data-del-key]").forEach(b => {
    b.addEventListener("click", async () => {
      const name = b.dataset.delKey;
      if (!confirm(`清除 ${name}?后续使用前需重新${name === "user_authorization" ? "从主机拉取" : "上传"}。`)) return;
      try {
        await api("DELETE", `/api/keys/${name}`);
        renderKeysTab();
      } catch (e) {
        $("kStatus").innerHTML = `<div class="alert-box">清除失败:${esc(e.message)}</div>`;
      }
    });
  });
}

function bindKeyDrop(zoneId, inputId, label, endpoint) {
  const zone = $(zoneId);
  const inp = $(inputId);
  if (!zone || !inp) return;

  async function upload(file) {
    const fd = new FormData();
    fd.append("file", file);
    $("kStatus").innerHTML = `<div class="alert-box info">${esc(label)} 写入沙盒中…</div>`;
    try {
      const res = await api("POST", endpoint, fd, true);
      $("kStatus").innerHTML = `<div class="alert-box success">✓ ${esc(label)} 已写入沙盒(${(res.size_bytes / 1024).toFixed(1)} KB · 权限 0600)</div>`;
      setTimeout(renderKeysTab, 600);
    } catch (e) {
      $("kStatus").innerHTML = `<div class="alert-box">${esc(label)} 上传失败:${esc(e.message)}</div>`;
    }
  }

  inp.addEventListener("change", e => {
    if (e.target.files[0]) upload(e.target.files[0]);
  });

  zone.querySelectorAll("[data-pick]").forEach(p => {
    p.addEventListener("click", e => { e.stopPropagation(); inp.click(); });
  });
  zone.addEventListener("click", (e) => {
    // 不要让里面的链接/按钮触发外层 click
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
    e.preventDefault();
    e.stopPropagation();
    zone.classList.remove("dragover");
    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      upload(e.dataTransfer.files[0]);
    }
  });
}

async function renderAccountTab() {
  const me = await api("GET", "/api/me");
  $("modalBody").innerHTML = `
    <h2>${TABS.account.title}</h2>
    <p class="sub">登录信息 · 凭据过期时间</p>
    <div class="list-item">
      <div class="grow"><div class="t">用户名</div><div class="d">${esc(me.username)}</div></div>
    </div>
    <div class="list-item">
      <div class="grow"><div class="t">主机</div><div class="d">${esc(me.host_url)}</div></div>
    </div>
    <div class="list-item">
      <div class="grow"><div class="t">凭据有效期</div><div class="d">${esc(me.expires_at || '—')}</div></div>
    </div>
    <button class="btn-danger" id="logoutBtn" style="margin-top:16px;">退出登录</button>
  `;
  $("logoutBtn").addEventListener("click", async () => {
    if (!confirm("退出登录?当前会话会保留,下次登录后仍可看到。")) return;
    await api("POST", "/logout");
    window.location = "/login";
  });
}

// schema 现在由服务端在上传时自动生成,前端不再需要 SAMPLE_SCHEMA

// ============ 事件绑定 ============
function bindEvents() {
  $("newBtn").addEventListener("click", createSession);
  $("sendBtn").addEventListener("click", sendMessage);
  $("input").addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  $("input").addEventListener("input", e => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(140, e.target.scrollHeight) + "px";
  });

  $("attachBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", e => handleFileAttach(e.target.files));

  // 全局拖拽到 composer 的 overlay,但避开:
  // 1) 设置 modal 打开时 → 把 drop 交给 modal 内部的 sk-drop / file inputs
  // 2) drop 已经在 sk-drop 上被 stopPropagation 时
  const modalOpen = () => $("modalMask").classList.contains("open");
  let dragCounter = 0;
  document.addEventListener("dragenter", e => {
    if (modalOpen()) return;
    e.preventDefault(); dragCounter++;
    if (e.dataTransfer.types.includes("Files")) $("dropOverlay").classList.add("show");
  });
  document.addEventListener("dragleave", e => {
    if (modalOpen()) return;
    dragCounter--; if (dragCounter <= 0) { dragCounter = 0; $("dropOverlay").classList.remove("show"); }
  });
  document.addEventListener("dragover", e => { if (!modalOpen()) e.preventDefault(); });
  document.addEventListener("drop", e => {
    if (modalOpen()) return;
    e.preventDefault(); dragCounter = 0;
    $("dropOverlay").classList.remove("show");
    if (e.dataTransfer.files && e.dataTransfer.files.length) {
      handleFileAttach(e.dataTransfer.files);
    }
  });

  // ctx chip (只有 cipher,schema 自动)
  $("ctxCipher").addEventListener("click", () => openModal("files"));

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
  // 没有任何会话 → composer 仍禁用,等用户点新建
  // 已有会话:打开第一个
  if (state.sessions.length) {
    await selectSession(state.sessions[0].id);
  }
})();
