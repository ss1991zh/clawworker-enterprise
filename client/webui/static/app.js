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

// 安全 markdown → HTML(先 esc 防 XSS,再处理 标题/列表/加粗/斜体/行内代码/代码块/链接)。
// 链接仅放行 http(s),统一 target=_blank rel=noopener。给 AI 回复 / summary 通用排版。
function mdToHtml(src) {
  if (!src) return "";
  let s = esc(src);                       // 1) 整体转义,后续只引入受控标签
  const blocks = [];                      // 围栏代码块(块级)
  const inl = [];                         // 行内代码 / 链接 占位(避免被斜体正则误吃 _blank)
  const stash = (html) => { inl.push(html); return `@@I${inl.length - 1}@@`; };
  // 2) 围栏代码块 ```lang\n...```
  s = s.replace(/```[^\n`]*\n?([\s\S]*?)```/g, (_, code) => {
    blocks.push(code.replace(/\n+$/, ""));
    return `@@B${blocks.length - 1}@@`;
  });
  // 3) 行内代码 `x` → 占位
  s = s.replace(/`([^`\n]+)`/g, (_, c) => stash(`<code>${c}</code>`));
  // 4) 链接 [text](url) —— 仅 http(s) → 占位(含 target="_blank")
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_, t, u) => stash(`<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`));
  // 5) 裸 URL(前面是空白/行首/左括号,避免命中已在 href 里的)→ 占位
  s = s.replace(/(^|[\s(（])(https?:\/\/[^\s<)）]+)/g,
    (_, pre, u) => `${pre}${stash(`<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`)}`);
  // 6) 加粗 / 斜体(链接/代码已占位,_blank 等不会被误吃)
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
       .replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>')
       .replace(/(^|[^_])_([^_\n]+)_(?!_)/g, '$1<em>$2</em>');
  // 7) 逐行:标题 / 有序无序列表 / 引用 / 分隔线 / 段落
  const out = [];
  let list = null;  // 'ul' | 'ol'
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of s.split("\n")) {
    const ph = line.match(/^@@B(\d+)@@$/);
    if (ph) { closeList(); out.push(`<pre><code>${blocks[+ph[1]]}</code></pre>`); continue; }
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeList(); const lv = Math.min(m[1].length, 6); out.push(`<h${lv}>${m[2]}</h${lv}>`); continue;
    }
    if (/^\s*([-*+])\s+/.test(line)) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${line.replace(/^\s*[-*+]\s+/, "")}</li>`); continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${line.replace(/^\s*\d+\.\s+/, "")}</li>`); continue;
    }
    if (/^\s*>\s?/.test(line)) { closeList(); out.push(`<blockquote>${line.replace(/^\s*>\s?/, "")}</blockquote>`); continue; }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { closeList(); out.push("<hr>"); continue; }
    if (line.trim() === "") { closeList(); continue; }
    closeList(); out.push(`<p>${line}</p>`);
  }
  closeList();
  // 还原行内代码/链接占位
  let html = out.join("").replace(/@@I(\d+)@@/g, (_, i) => inl[+i] || "");
  // 1) 相邻链接(仅空白相隔)→ 插入分隔符「·」
  html = html.replace(/<\/a>\s*<a /g, '</a><span class="link-sep">·</span><a ');
  // 2) 链接放在句末标点「之后」:把紧跟在(整串)链接后的句末标点(。.!?;)挪到链接前,
  //    让链接成为句末的引用。单链接 / 多链接并排都正确。
  const linkRun = '(?:<a\\b[^>]*>[^<]*<\\/a>(?:<span class="link-sep">·<\\/span>)?)+';
  html = html.replace(new RegExp(`(${linkRun})\\s*([。.!?;！?;])`, 'g'),
                      (_, run, punct) => `${punct} ${run}`);
  return html;
}

// 文件卡图标:明文(文档)/ 密文(锁)
const FILE_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="16" y2="17"/></svg>';
const LOCK_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>';
const SESS_CLOCK_INLINE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>';
const FOLDER_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>';
const CHECK_ICON_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6L9 17l-5-5"/></svg>';

// 单个文件卡:仅右侧「下载」按钮触发下载(不自动下载,需手动点击)
function oneFileCard(path, name, kind) {
  const dl = `/api/excel/download?path=${encodeURIComponent(path)}`;
  const icon = kind === "cipher" ? LOCK_ICON_SVG : FILE_ICON_SVG;
  const hint = kind === "cipher" ? "加密文件 · 数值列为密文" : "Excel 输出 · 明文";
  return `
    <div class="file-card" data-path="${esc(path)}" data-name="${esc(name)}" data-kind="${kind}">
      <div class="fc-ic ${kind}">${icon}</div>
      <div class="fc-body">
        <div class="fc-nm">${esc(name)}</div>
        <div class="fc-hint">${hint}</div>
      </div>
      <a class="fc-btn" href="${dl}" download="${esc(name)}">⬇ 下载</a>
    </div>`;
}

// 一条 assistant 消息的文件区:加密卡在前、解密卡在后;保留密文未解密时给「解密」按钮
function fileCardsHtml(m, willType) {
  const cards = [];
  if (m.enc_excel_path && m.enc_excel_name) cards.push(oneFileCard(m.enc_excel_path, m.enc_excel_name, "cipher"));
  if (m.excel_path && m.excel_name) cards.push(oneFileCard(m.excel_path, m.excel_name, "plain"));
  let decBtn = "";
  if (m.enc_excel_path && m.can_decrypt && !m.excel_path) {
    decBtn = `<button class="dec-file-btn" data-mid="${esc(m.id)}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" class="dec-ic"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
      <span>解密查看明文</span>
    </button>`;
  }
  if (!cards.length && !decBtn) return "";
  const hidden = willType ? ' data-defer-reveal="1"' : '';
  return `<div class="file-cards"${hidden}>${cards.join("")}${decBtn}</div>`;
}

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
  currentTaskId: "",       // 当前定时会话对应的任务 id(供顶栏 4 面板)
  ov: false,               // 总概览全屏模式:false | "list" | "detail"
  lastSidByView: { normal: null, scheduled: null },  // 每个视图最后停留的会话(切换时自动回到)
  lastSeen: (() => { try { return JSON.parse(localStorage.getItem("cw_seen") || "{}"); } catch (e) { return {}; } })(),  // sid → 上次查看时的 updated_at(未读判定)
  _seenBaselined: false,   // 首次加载会话后,把现有会话设为已读基线
  pendingCipher: null,   // {name, path, size, uploading?}
  pendingTexts: [],      // [{name, content, chars, uploading?}]
  files: [],
  skills: { skill_md: [], builtin: [], custom: [] },
  tasks: [], tasksPending: [], tasksHistory: [], tasksPendingCount: 0,
  histFilter: { date: "" },
  // 运行状态:有 assistant 消息在跑时锁住发送按钮 / 变停止
  running: false,
  runningMid: null,
  // 已经播过打字机动画的 mid(防止重渲时再次动画)
  typedMids: new Set(),
  wizardSeen: new Set(),   // 已自动弹过向导的消息 id(避免轮询重复弹)
  wizard: null,            // 创建定时任务向导的当前状态 {step, slots, files}
  sessionView: (() => { try { return localStorage.getItem("cw_sess_view") === "scheduled" ? "scheduled" : "normal"; } catch (e) { return "normal"; } })(),
  // 正在轮询的 mid(防止 sync + selectSession 重复起 interval)
  pollingMids: new Set(),
  // 联网搜索开关(发送时透传给后端;需所用模型/服务支持)。
  // 持久化到 localStorage —— 刷新/重启后保留上次选择。
  webSearch: (() => { try { return localStorage.getItem("cw_web_search") === "1"; } catch (e) { return false; } })(),
};

// ============ API helpers ============
const CSRF_TOKEN = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";

async function api(method, path, body, isMultipart = false) {
  const opts = { method, headers: {} };
  // 改状态请求带 CSRF token(后端中间件校验);安全 GET 不需要但带上无害
  if (CSRF_TOKEN) opts.headers["X-CSRF-Token"] = CSRF_TOKEN;
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
  // 首次加载:把现有会话设为"已读"基线(避免一上来全标未读);之后新产生的活动才算未读
  if (!state._seenBaselined) {
    state.sessions.forEach(s => { if (state.lastSeen[s.id] === undefined) state.lastSeen[s.id] = s.updated_at; });
    state._seenBaselined = true;
    _saveLastSeen();
  }
  // 仅在列表确有变化(新增/标题/更新时间/运行态/当前选中)时才重渲,避免每 6s 无谓重建 DOM
  const sig = JSON.stringify(state.sessions.map(s => [s.id, s.title, s.updated_at, s.running, s.missed_count]).concat([["cur", state.currentSid]]));
  if (sig === state._sessSig) return;
  state._sessSig = sig;
  renderSessionList();
}

function _saveLastSeen() {
  try { localStorage.setItem("cw_seen", JSON.stringify(state.lastSeen)); } catch (e) {}
}
function _markSeen(sid) {
  const s = (state.sessions || []).find(x => x.id === sid);
  state.lastSeen[sid] = (s && s.updated_at) || new Date().toISOString();
  _saveLastSeen();
}
function _isUnread(s) {
  return s.id !== state.currentSid && s.updated_at > (state.lastSeen[s.id] || "");
}

function isSchedSess(s) { return (s.kind === "scheduled") || (s.title || "").startsWith("⏰"); }

function renderSessionList() {
  const el = $("sessionList");
  const SESS_CLOCK_SVG = `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`;
  const SESS_CHAT_SVG = `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;

  // 全局切换:只显示当前视图(普通 / 定时任务)
  const view = state.sessionView || "normal";
  const normal = state.sessions.filter(s => !isSchedSess(s));
  const sched = state.sessions.filter(isSchedSess);
  // 切换条上的计数:普通=会话数;定时任务=任务总数(与「定时任务管理」一致,而非已打开的会话数)
  document.querySelectorAll("#sessToggle .sess-toggle__btn").forEach(b => {
    b.classList.toggle("active", b.dataset.view === view);
    const n = b.dataset.view === "scheduled" ? (state.tasks || []).length : normal.length;
    b.dataset.count = n;
  });

  const list = view === "scheduled" ? sched : normal;
  if (!list.length) {
    el.innerHTML = view === "scheduled"
      ? '<div class="session-empty">还没有定时任务会话<br>点上方「定时任务管理」,在任务上点「查看会话」即可在此打开</div>'
      : '<div class="session-empty">还没有普通会话<br>点击上方"新建会话"</div>';
    return;
  }
  const RUN_DOTS = '<span class="sess-run" title="正在运行"><i></i><i></i><i></i></span>';
  const oneItem = s => {
    const isSched = isSchedSess(s);
    const title = (s.title || "").replace(/^⏰\s*/, "");
    // 运行中 → 动态"…"图标;否则原图标;非当前会话有新内容 → 未读点
    const icon = s.running ? RUN_DOTS : (isSched ? SESS_CLOCK_SVG : SESS_CHAT_SVG);
    const unread = !s.running && _isUnread(s);
    const missed = (s.missed_count || 0) > 0;
    return `
    <div class="session ${s.id === state.currentSid ? "active" : ""} ${isSched ? "sched" : ""} ${s.running ? "running" : ""}" data-id="${s.id}">
      ${icon}
      <span class="session__title">${esc(title)}</span>
      ${missed ? `<span class="sess-missed" title="有 ${s.missed_count} 次漏跑待处理"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg></span>` : ''}
      ${unread ? '<span class="sess-unread" title="有新消息"></span>' : ''}
      <button class="session__del" data-del="${s.id}" title="删除会话">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
        </svg>
      </button>
    </div>`;
  };
  el.innerHTML = list.map(oneItem).join("");
  el.querySelectorAll(".session").forEach(node => {
    node.addEventListener("click", e => {
      if (e.target.closest("[data-del]")) return;
      selectSession(node.dataset.id);
    });
  });
  el.querySelectorAll("[data-del]").forEach(b => {
    b.addEventListener("click", async e => {
      e.stopPropagation();
      const sid = b.dataset.del;
      const s = (state.sessions || []).find(x => x.id === sid) || {};
      const isSched = isSchedSess(s);
      const msg = isSched
        ? "从列表移除这个定时任务会话?\n\n任务会继续运行,历次运行内容不会丢失;点该任务的「查看会话」可随时找回。"
        : "删除这个会话?其消息记录会一并丢失。";
      if (!confirm(msg)) return;
      await api("DELETE", `/api/sessions/${sid}`);
      if (sid === state.currentSid) {
        state.currentSid = null; state.currentSession = null;
        showWelcome();
      }
      await loadSessions();
    });
  });
}

async function createSession() {
  const s = await api("POST", "/api/sessions");
  setSessionView("normal");          // 新建的是普通会话
  await loadSessions();
  await selectSession(s.id);
}

function setSessionView(view) {
  state.sessionView = (view === "scheduled") ? "scheduled" : "normal";
  try { localStorage.setItem("cw_sess_view", state.sessionView); } catch (e) {}
  const sched = state.sessionView === "scheduled";
  if ($("newBtn")) $("newBtn").style.display = sched ? "none" : "";
  // 「新建定时任务」入口已移至「定时任务管理」页内(顶部 ovNew 按钮)
  if ($("overviewBtn")) $("overviewBtn").style.display = sched ? "" : "none";
  renderSessionList();
}

// 切到某视图时,自动选中该视图上次停留的会话(没有则选第一条,空则欢迎页)
async function selectViewSession(view) {
  const inView = (state.sessions || []).filter(s => (view === "scheduled") === isSchedSess(s));
  let sid = state.lastSidByView[view];
  if (!sid || !inView.some(s => s.id === sid)) sid = inView[0] && inView[0].id;
  if (sid) { await selectSession(sid); return; }
  // 该视图没有会话 → 欢迎页 + 复位输入栏
  state.currentSession = null;
  showWelcome();
  updateSessionChrome();
}

// 当前会话是否定时任务会话 → 禁用输入框 + 顶栏显示 4 个任务面板入口
// 「定时任务管理」(总览)里不需要发送框 —— 隐藏整个 footer;
// 但创建定时任务向导(wizBar)也在 footer 里,向导激活时要保留可见。
function _syncFooter() {
  const f = document.querySelector("footer");
  const wizActive = $("wizBar") && !$("wizBar").hidden;
  const hide = !!state.ov && !wizActive;
  if (f) f.style.display = hide ? "none" : "";
}

function updateSessionChrome() {
  const meta = (state.sessions || []).find(x => x.id === state.currentSid);
  const sched = !!(meta && isSchedSess(meta));
  const inp = $("input"), send = $("sendBtn"), attach = $("attachBtn"), web = $("webSearchBtn");
  if (inp) {
    inp.disabled = sched;
    inp.placeholder = sched ? "定时任务会话 · 由系统按计划自动运行,不能手动发送"
                            : "问问看 · 回车发送 · Shift+回车换行";
  }
  if (send) send.disabled = sched;   // 普通会话保持可发送(发送时再校验内容);定时会话禁用
  if (attach) attach.style.display = sched ? "none" : "";
  if (web) web.style.display = sched ? "none" : "";
  // 顶栏入口
  const ta = $("taskActions");
  if (ta) ta.style.display = sched ? "" : "none";
  state.currentTaskId = sched ? (meta.task_id || "") : "";
  // 「待解密文件」入口:只有"有数据绑定"的定时任务才有(无数据问答任务直接出文本,无需解密)
  const pendBtn = document.querySelector('#taskActions [data-tpanel="pending"]');
  if (pendBtn) {
    const isData = !!(meta && meta.task_needs_data);
    pendBtn.style.disp…34230 tokens truncated…L =
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

    <h3 class="keys-h3">计算密钥 / 字典 <span class="mono-tag">evk · dictf</span></h3>
    <div class="key-row">
      <div class="key-meta">${k.evk_present
        ? `<span class="badge ok">已导入</span>`
        : `<span class="badge no">未导入</span>`}</div>
    </div>
    <div class="sk-drop" id="dropEvk">
      <div class="sk-drop__t"><strong>拖入</strong> 或 <span class="sk-pick" data-pick="evk">点击选择</span> 计算密钥(evk,即字典 dictf)</div>
      <div class="sk-drop__s">密态计算必需 · evk 与 dictf 是同一文件 · 文件较大,上传需稍候 · 仅本机沙盒</div>
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

    <h3 class="keys-h3">密钥体检 <span class="mono-tag">selfcheck</span></h3>
    <div class="auth-fetch">
      <div class="af-body">
        <div class="af-t">在这套密钥上实测:能算什么、精度多少、能平稳跑多大规模</div>
        <div class="af-s">导入密钥/字典后跑一次 · 不配套或损坏会当场暴露</div>
      </div>
      <button class="btn-primary" id="keycheckBtn">开始体检</button>
    </div>
    <div id="keycheckResult" style="margin-top:10px;"></div>

    <div id="kStatus" style="margin-top:14px;"></div>
  `;

  bindKeyDrop("dropSk", "skFile", "sk", "/api/keys/sk");
  bindKeyDrop("dropEvk", "evkFile", "evk", "/api/keys/evk");

  $("keycheckBtn").addEventListener("click", async () => {
    const btn = $("keycheckBtn");
    btn.disabled = true; btn.textContent = "体检中…";
    $("keycheckResult").innerHTML = '<div class="alert-box info">正在体检(对拍实测,约数秒)…</div>';
    try {
      const rep = await api("GET", "/api/keycheck?quick=true");
      $("keycheckResult").innerHTML = renderKeycheckResult(rep);
    } catch (e) {
      $("keycheckResult").innerHTML = `<div class="alert-box">体检失败:${esc(e.message)}</div>`;
    } finally {
      btn.disabled = false; btn.textContent = "重新体检";
    }
  });

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

function renderKeycheckResult(rep) {
  const head = rep.ok
    ? '<div class="alert-box success">✓ 密钥可用 · 全部套件通过</div>'
    : '<div class="alert-box">⚠ 部分套件未通过(详见下方)</div>';
  // HE 库授权状态:ok=success,warn/critical/expired=醒目
  const lic = rep.license || {};
  let licBox = "";
  if (lic.message) {
    const cls = (lic.level === "ok") ? "success" : (lic.level === "warn") ? "info" : "";
    licBox = `<div class="alert-box ${cls}">${esc(lic.message)}</div>`;
  }
  const rows = (rep.suites || []).map(s =>
    `<div class="key-row"><div class="key-meta">
       <span class="badge ${s.ok ? "ok" : "no"}">${s.ok ? "通过" : "未过"}</span>
       <strong>${esc(s.name)}</strong> <span class="af-s">· ${esc(s.detail)}</span>
     </div></div>`).join("");
  const t = rep.scale_tier || {};
  const tier = t.max_smooth_n
    ? `<div class="af-s" style="margin-top:8px;">规模:向量化聚合平稳到 ${Number(t.max_smooth_n).toLocaleString()} 行` +
      `(group-by ${t.groupby_secs_at_max}s);排名/topk 大表自动走授权解密。</div>`
    : "";
  const brief = rep.capability_brief
    ? `<details style="margin-top:8px;"><summary class="af-s" style="cursor:pointer;">能力清单(对拍实测)</summary>` +
      `<pre class="keycheck-brief">${esc(rep.capability_brief)}</pre></details>`
    : "";
  return head + licBox + rows + tier + brief;
}

async function renderAuditTab() {
  let data;
  try {
    data = await api("GET", "/api/audit?limit=100");
  } catch (e) {
    $("modalBody").innerHTML = `<h2>可信审计</h2><div class="alert-box">读取失败:${esc(e.message)}</div>`;
    return;
  }
  const s = data.summary || {};
  const events = (data.events || []).slice().reverse();
  $("modalBody").innerHTML = `
    <h2>可信审计</h2>
    <p class="sub">证明:明文不出本机 · LLM 只见字段名 schema · 解密均经本机授权可追溯</p>
    <div class="alert-box ${s.zero_plaintext_holds ? "success" : ""}">${esc(s.statement || "暂无审计记录 —— 跑一次分析后这里会有台账。")}</div>
    <div class="key-row"><div class="key-meta af-s">
      LLM 暴露事件 <strong>${s.llm_exposures || 0}</strong> ·
      解密授权 <strong>${s.decrypt_authorizations || 0}</strong>
      (授权 ${s.decrypt_granted || 0} / 拒绝或保留密文 ${s.decrypt_denied || 0}) ·
      疑似明文外发 <strong>${s.plaintext_breaches || 0}</strong>
    </div></div>
    <h3 class="keys-h3">最近事件</h3>
    <div>${events.length ? events.map(renderAuditEvent).join("") : '<div class="af-s">暂无记录。</div>'}</div>
    <div style="margin-top:12px;"><button class="btn-primary" id="auditExportBtn">导出合规报告(Word)</button></div>
  `;
  $("auditExportBtn").addEventListener("click", () => {
    // 服务端生成带排版、大白话的 .docx;用隐藏 <a> 触发下载(带登录 cookie)
    const a = document.createElement("a");
    a.href = "/api/audit/export";
    a.download = "";
    document.body.appendChild(a); a.click(); a.remove();
  });
}

function renderAuditEvent(e) {
  const ts = esc((e.ts || "").replace("T", " ").slice(0, 19));
  if (e.type === "llm_exposure") {
    const ok = e.no_plaintext;
    return `<div class="key-row"><div class="key-meta">
      <span class="badge ${ok ? "ok" : "no"}">${ok ? "零明文" : "疑似外发"}</span>
      <span class="mono-tag">LLM</span> <span class="af-s">${ts} · 字段 ${e.field_count || 0} 个:${esc((e.fields || []).slice(0, 6).join("、"))}</span>
    </div></div>`;
  }
  if (e.type === "decrypt_auth") {
    const d = e.decision;
    const label = d === "granted" ? "授权解密" : d === "keep_encrypted" ? "保留密文" : "拒绝/取消";
    return `<div class="key-row"><div class="key-meta">
      <span class="badge ${d === "granted" ? "ok" : "warn"}">${label}</span>
      <span class="mono-tag">解密</span> <span class="af-s">${ts} · ${esc(e.detail || "")}</span>
    </div></div>`;
  }
  return "";
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

// ============ 站内信(只读通知)============
const NOTICE_LV = { info: "提示", warning: "注意", critical: "严重" };

async function loadNotices() {
  try {
    const r = await api("GET", "/api/notices");
    state.notices = r.items || [];
    state.noticeUnread = r.unread || 0;
  } catch (e) { return; }   // 未登录/网络问题:静默
  renderNoticeDot();
  if (state.noticeOpen) renderNoticePanel();
}

function renderNoticeDot() {
  const d = $("noticeDot");
  if (d) d.hidden = !(state.noticeUnread > 0);
}

function _noticeTime(iso) { return ((iso || "").replace("T", " ").slice(0, 16)) || "—"; }

function renderNoticePanel() {
  const box = $("noticeList");
  if (!box) return;
  const items = state.notices || [];
  if (!items.length) { box.innerHTML = '<div class="notice-empty">暂无站内信</div>'; return; }
  box.innerHTML = items.map(n => `
    <div class="notice-item ${esc(n.level)} ${n.read ? "" : "unread"}">
      <div class="notice-item__t"><span>${esc(n.title)}</span>
        <span class="notice-item__lv">${esc(NOTICE_LV[n.level] || "提示")}</span></div>
      <div class="notice-item__s">${esc(n.summary)}</div>
      <div class="notice-item__time">${esc(_noticeTime(n.created_at))}</div>
    </div>`).join("");
}

async function openNotices() {
  state.noticeOpen = true;
  $("noticePanel").classList.add("open");
  $("noticeScrim").classList.add("open");
  await loadNotices();
  renderNoticePanel();
  // 打开即全部已读 → 小红点消失
  if (state.noticeUnread > 0) {
    try { await api("POST", "/api/notices/read"); } catch (e) {}
    state.noticeUnread = 0;
    (state.notices || []).forEach(n => { n.read = true; });
    renderNoticeDot(); renderNoticePanel();
  }
}

function closeNotices() {
  state.noticeOpen = false;
  $("noticePanel").classList.remove("open");
  $("noticeScrim").classList.remove("open");
}

// ============ 事件绑定 ============
function bindEvents() {
  $("newBtn").addEventListener("click", createSession);
  // 会话视图切换:普通 / 定时任务 —— 切换后自动回到该视图上次停留的会话
  document.querySelectorAll("#sessToggle .sess-toggle__btn").forEach(b => {
    b.addEventListener("click", async () => {
      setSessionView(b.dataset.view);
      // 切到「定时任务」→ 直接展示「定时任务管理」总览;切回「普通会话」→ 回到该视图会话
      if (b.dataset.view === "scheduled") await openOverview();
      else await selectViewSession("normal");
    });
  });
  $("sendBtn").addEventListener("click", sendMessage);

  // 输入法(中文/日文/韩文等)组词期间不发送
  // - e.isComposing 是 W3C 标准属性,组词期间为 true
  // - e.keyCode === 229 是兼容老浏览器的 fallback(Safari/老 Chrome 在选词期触发的 Enter)
  // - 还监听 compositionstart/end 维护一个手动标志,兜底
  let imeComposing = false;
  const input = $("input");
  input.addEventListener("compositionstart", () => { imeComposing = true; });
  input.addEventListener("compositionend",   () => { imeComposing = false; });
  input.addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.shiftKey) return;
    if (e.isComposing || e.keyCode === 229 || imeComposing) return;  // 组词中,Enter 用于确认候选,不发送
    e.preventDefault();
    sendMessage();
  });
  $("input").addEventListener("input", e => {
    e.target.style.height = "auto";
    e.target.style.height = Math.min(140, e.target.scrollHeight) + "px";
  });

  $("attachBtn").addEventListener("click", () => $("fileInput").click());
  $("fileInput").addEventListener("change", e => {
    handleFileAttach(e.target.files);
    e.target.value = "";  // 允许重复选同一文件
  });

  // 联网搜索开关:点亮即在后续消息里启用(需所用模型支持,否则后端自动降级)。
  // 状态持久化:启动时回显上次选择,点击时写回 localStorage。
  const wsBtn = $("webSearchBtn");
  if (wsBtn) {
    const syncWs = () => {
      wsBtn.classList.toggle("active", state.webSearch);
      wsBtn.setAttribute("aria-pressed", state.webSearch ? "true" : "false");
    };
    syncWs();  // 回显持久化的初始状态
    wsBtn.addEventListener("click", () => {
      state.webSearch = !state.webSearch;
      try { localStorage.setItem("cw_web_search", state.webSearch ? "1" : "0"); } catch (e) {}
      syncWs();
    });
  }

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
    if (e.dataTransfer.files && e.dataTransfer.files.length) handleFileAttach(e.dataTransfer.files);
  });

  // 站内信
  $("noticeBtn")?.addEventListener("click", () => state.noticeOpen ? closeNotices() : openNotices());
  $("noticeClose")?.addEventListener("click", closeNotices);
  $("noticeScrim")?.addEventListener("click", closeNotices);

  // settings
  $("settingsBtn").addEventListener("click", () => openModal("general"));
  $("modalClose").addEventListener("click", closeModal);
  $("modalMask").addEventListener("click", e => {
    if (e.target === $("modalMask")) closeModal();
  });
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.addEventListener("click", () => openModal(b.dataset.tab));
  });

  // 密文文件管理弹窗(入口已移除,保留关闭逻辑以防残留打开)
  $("filesClose")?.addEventListener("click", closeFilesModal);
  $("filesMask")?.addEventListener("click", e => {
    if (e.target === $("filesMask")) closeFilesModal();
  });

  // 侧栏「定时任务管理」→ 打开管理页(页内顶部有「新建定时任务」)
  $("overviewBtn")?.addEventListener("click", () => openOverview());

  // 定时任务会话顶栏:打开对应 tab 的弹窗任务面板
  document.querySelectorAll("#taskActions [data-tpanel]").forEach(b => {
    b.addEventListener("click", () => openTaskPanel(b.dataset.tpanel));
  });
  $("taskPanelClose")?.addEventListener("click", closeTaskPanel);
  $("taskPanelMask")?.addEventListener("click", e => {
    if (e.target === $("taskPanelMask")) closeTaskPanel();
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
  state._bootISO = new Date().toISOString();   // 只对启动后新产生的向导消息自动弹窗(避免刷新后重弹)
  bindEvents();
  setSessionView(state.sessionView);   // 同步「新建会话/新建定时任务」按钮显隐
  await loadSessions();
  // 刷新前若停在「定时任务管理」总览 → 恢复到总览(而不是跳去某个会话)
  let wasOv = false;
  try { wasOv = localStorage.getItem("cw_ov") === "1"; } catch (e) {}
  if (wasOv) {
    setSessionView("scheduled");
    await openOverview();
  } else {
    // 恢复上次选中的会话(刷新后保持);失效则回退到第一条。
    // 直接选中目标会话,**不先画欢迎页**,避免"先闪新会话页再切回"的抖动。
    let restore = "";
    try { restore = localStorage.getItem("cw_cur_sid") || ""; } catch (e) {}
    const target = (restore && state.sessions.some(s => s.id === restore))
      ? restore : (state.sessions[0] && state.sessions[0].id);
    if (target) await selectSession(target);
    else showWelcome();
  }
  loadFiles();   // 非阻塞:密文文件列表后台加载,不拖慢首屏
  refreshTaskCount();   // 「定时任务」切换条数字 = 任务总数
  // 站内信:首刷 + 每 15s 轮询(近实时;读取即同步,天然补发停机期间的消息)
  loadNotices();
  setInterval(loadNotices, 15000);
  // 定时任务待批红点:首刷 + 每 30s 轮询
  refreshTasksBadge();
  setInterval(refreshTasksBadge, 30000);
  // 当前会话后台同步:每 4s 接住服务端(定时任务)注入的新消息
  setInterval(syncCurrentSession, 4000);
  // 侧栏每 15s 刷一次,捕获定时任务新建的「⏰」会话
  // 侧栏每 6s 刷新:及时反映各会话的"运行中…"图标与未读标记(定时任务可能在别的会话里跑)
  setInterval(() => { if (!state.pollingMids.size) loadSessions(); }, 6000);
})();

