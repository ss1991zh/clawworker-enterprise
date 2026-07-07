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
    pendBtn.style.display = (sched && isData) ? "" : "none";
    const lbl = pendBtn.querySelector("span:first-child");
    if (lbl) lbl.textContent = "待解密文件";
  }
  // 「漏跑」入口:仅当该任务有漏跑时出现(数据/无数据任务都可能漏跑)
  const missBtn = document.querySelector('#taskActions [data-tpanel="missed"]');
  if (missBtn) {
    const mc = (sched && meta) ? (meta.missed_count || 0) : 0;
    missBtn.style.display = mc > 0 ? "" : "none";
    const mb = $("missedBadge");
    if (mb) { mb.style.display = mc > 0 ? "" : "none"; mb.textContent = mc > 99 ? "99+" : mc; }
  }
  refreshTasksBadge();   // 立即按当前任务刷新红点(不等 30s 轮询)
  _syncFooter();         // 总览模式隐藏发送框
}

async function selectSession(sid) {
  state.ov = false; clearInterval(_gsTimer);   // 退出总概览全屏模式
  try { localStorage.removeItem("cw_ov"); } catch (e) {}
  // 选中的若是定时任务会话,自动切到「定时任务」视图,使其在侧栏可见
  const meta = (state.sessions || []).find(x => x.id === sid);
  if (meta && isSchedSess(meta) && state.sessionView !== "scheduled") setSessionView("scheduled");
  // 记住每个视图最后停留的会话(供切换视图时自动回到)
  state.lastSidByView[(meta && isSchedSess(meta)) ? "scheduled" : "normal"] = sid;
  state.currentSid = sid;
  // 切会话时把"运行中"状态复位 —— 进的新会话单独跟踪
  setRunning(false);
  try { localStorage.setItem("cw_cur_sid", sid); } catch (e) {}   // 记住当前选中,刷新后恢复
  const data = await api("GET", `/api/sessions/${sid}/messages`);
  state.currentSession = data.session;
  state.currentSession.messages = data.messages;
  // 标记该会话为已读(清未读点)—— 取最大可得的 updated_at,确保不会留残点
  const seenAt = (data.session && data.session.updated_at)
    || ((state.sessions.find(x => x.id === sid) || {}).updated_at)
    || new Date().toISOString();
  state.lastSeen[sid] = seenAt; _saveLastSeen();
  // 已经在视图里的 assistant summary 不再播打字机(只对新消息播)
  (state.currentSession.messages || []).forEach(m => {
    if (m.role === "assistant" && m.status === "done") state.typedMids.add(m.id);
  });
  renderSessionList();
  renderChat();
  enableComposer();
  updateSessionChrome();   // 定时会话 → 禁用输入 + 顶栏显示 4 入口
  $("sidebar").classList.remove("open");
  $("scrim").classList.remove("open");
  // 重新轮询所有 pending/running 的 assistant 消息(防主进程重启后丢轮询)
  (state.currentSession.messages || []).forEach(m => {
    if (m.role === "assistant" && (m.status === "pending" || m.status === "running")) {
      setRunning(true, m.id);
      pollMessage(sid, m.id);
    }
  });
}

// ============ Chat 渲染 ============
function showWelcome() {
  state.ov = false; clearInterval(_gsTimer);
  try { localStorage.removeItem("cw_ov"); } catch (e) {}
  $("chat").classList.remove("ovmode");
  $("chat").innerHTML = "";
  $("chat").appendChild(welcomeNode());
  state.currentSid = null;
  if ($("taskActions")) $("taskActions").style.display = "none";
  _syncFooter();
}

function welcomeNode() {
  const w = document.createElement("div");
  w.className = "welcome";
  // 复用侧栏 logo 的(已带版本号的)src,保证用同一张最新 logo
  const logoSrc = document.querySelector(".sidebar__head img.logo-img")?.getAttribute("src") || "/static/logo.png";
  w.innerHTML = `
    <img class="logo welcome-logo logo-img" src="${logoSrc}" alt="Clawworker">
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
  chat.classList.remove("ovmode");   // 退出总概览全屏
  chat.innerHTML = "";
  const msgs = state.currentSession?.messages || [];
  if (!msgs.length) {
    const meta = (state.sessions || []).find(x => x.id === state.currentSid);
    chat.appendChild((meta && isSchedSess(meta)) ? schedEmptyNode() : welcomeNode());
    return;
  }
  msgs.forEach(m => chat.appendChild(renderMessage(m)));
  $("main").scrollTop = $("main").scrollHeight;
}

function schedEmptyNode() {
  const w = document.createElement("div");
  w.className = "welcome";
  const logoSrc = document.querySelector(".sidebar__head img.logo-img")?.getAttribute("src") || "/static/logo.png";
  w.innerHTML = `
    <img class="logo welcome-logo logo-img" src="${logoSrc}" alt="Clawworker">
    <div class="big">定时任务会话</div>
    <div class="sub">该任务将按计划自动运行,结果会按时出现在这里。</div>
    <p class="welcome-hint">用顶栏的 <strong>编辑任务 / 运行历史</strong> 管理当前任务 · <strong>运行状态</strong> 查看全部 · 有数据的任务还会有 <strong>待解密文件</strong> 入口</p>`;
  return w;
}

// 定时任务会话:一轮执行的页脚(执行日期/时间 · 耗时 · token 用量)
function runMetaHtml(m) {
  const dt = m.created_at || "";
  const date = dt.slice(0, 10), time = dt.slice(11, 19);
  const dur = m.duration_sec ? ` · 耗时 ${m.duration_sec}s` : "";
  const tok = ` · 消耗 ${(m.tokens || 0).toLocaleString()} tokens`;
  const when = (date || time) ? `执行于 ${esc(date)} ${esc(time)}` : "已执行";
  return `<div class="run-meta">${when}${dur}${tok}</div>`;
}

function renderMessage(m) {
  const wrap = document.createElement("div");
  wrap.dataset.mid = m.id;

  // 系统事件(漏跑 / 忽略 / 补救):居中提示行,不是对话气泡
  if (m.role === "event") {
    wrap.className = `msg-event ${esc(m.event_kind || "")}`;
    wrap.innerHTML = `<span class="msg-event__t">${esc(m.content || "")}</span>`;
    return wrap;
  }

  wrap.className = `msg ${m.role === "user" ? "user" : "bot"}`;
  const avatar = `<div class="avatar">${m.role === "user" ? "我" : "爪"}</div>`;
  let content = `<div class="msg__content">`;

  if (m.role === "user") {
    content += `<div class="bubble">${esc(m.content).replace(/\n/g, "<br>")}</div>`;
    const lines = [];
    if (m.attached_cipher) {
      const nm = m.attached_cipher.split("/").pop();
      lines.push(`<span class="att-piece cipher">${ICON_SVG.doc} ${esc(nm)}</span>`);
    }
    (m.text_attachment_names || []).forEach(nm => {
      lines.push(`<span class="att-piece text">${ICON_SVG.doc} ${esc(nm)}</span>`);
    });
    if (lines.length) content += `<div class="att-line">${lines.join("")}</div>`;
  } else {
    // ============ assistant ============
    const running = (m.status === "pending" || m.status === "running");
    const failed = (m.status === "failed");
    const needsCipher = (m.status === "needs_cipher");
    const cancelled = (m.status === "cancelled");
    const awaitingDecrypt = (m.status === "awaiting_decrypt");
    const steps = m.steps || [];

    // 进度行(running 时实时追加;done 时默认折叠)
    // 用 <details> 让用户可以折叠/展开;running 时默认打开,done 时默认收起
    if (steps.length || running || awaitingDecrypt) {
      const detailsOpen = (running || awaitingDecrypt) ? " open" : "";
      const stateLabel = awaitingDecrypt
        ? "计算追踪 · 密态计算已完成 · 等待解密授权"
        : running
        ? "计算追踪 · 密态运算中"
        : `计算追踪 · 已完成 · ${steps.length} 步`;
      content += `<details class="trace"${detailsOpen}>`;
      content += `<summary class="trace-summary">${stateLabel}</summary>`;
      content += `<div class="trace-steps">`;
      steps.forEach((s, i) => {
        const cls = s.kind || "step";
        // running 时,最后一步是"当前正在跑"的阶段 → 加 active 脉冲,告诉用户卡在哪一步
        const act = (running && i === steps.length - 1) ? " active" : "";
        content += `<div class="step ${cls}${act}">${esc(s.label)}</div>`;
      });
      content += `</div></details>`;
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
        content += `<div class="bubble md">${mdToHtml(m.summary)}</div>`;
      }
    } else if (needsCipher) {
      content += `<div class="ask-card">${ICON_SVG.ask}<span>${esc(m.summary || "请附一份已加密的数据文件")}</span></div>`;
    } else if (cancelled) {
      content += `<div class="ask-card">${ICON_SVG.warn}<span>${esc(m.summary || "已停止")}</span></div>`;
    } else if (awaitingDecrypt) {
      // 从 trace 里提"sheet「...」就绪"行,告诉用户哪些 sheet 已在密态下算好
      const readyLines = (m.steps || [])
        .filter(s => (s.label || "").includes("就绪"))
        .map(s => s.label);
      const skillLines = readyLines.length
        ? `<ul class="dc-list">${readyLines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>`
        : "";
      content += `
        <div class="decrypt-card" data-mid="${esc(m.id)}">
          <div class="dc-title">解密授权 / 审批</div>
          <div class="dc-body">
            <strong>计算已在密态下完成</strong>,全程未暴露明文。
            各 skill 的密态运算路径不同,详见上方「计算追踪」。
            ${readyLines.length ? `共产出 ${readyLines.length} 个 sheet:` : ""}
            ${skillLines}
            请选择结果是否解密展示:
          </div>
          <div class="dc-actions">
            <button class="dc-btn primary" data-choice="decrypt">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="dc-ic"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
              解密展示结果
            </button>
            <button class="dc-btn ghost" data-choice="keep_encrypted">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="dc-ic"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
              保留密文(不解密)
            </button>
          </div>
          <div class="dc-hint">选「保留密文」会导出未解密的 Excel,数值列保持同态密文形式 · 5 分钟未操作自动取消</div>
        </div>`;
    } else {
      // done
      // 打字机:首次渲染留空 bubble + cursor,渲染完后 JS 逐字填入
      const willType = !state.typedMids.has(m.id) && (m.summary || "").length > 0;
      if (willType) {
        content += `<div class="bubble md" data-typewriter="${esc(m.summary || "")}"><span class="type-cursor">▍</span></div>`;
      } else {
        content += `<div class="bubble md">${mdToHtml(m.summary || "(无总结)")}</div>`;
      }
      content += fileCardsHtml(m, willType);
      if (m.clarify && Object.keys(m.clarify).length) {
        const cl = m.clarify;
        content += `
          <div class="clarify-card" data-mid="${esc(m.id)}">
            <div class="clarify-q">${esc(cl.question || "请选择:")}</div>
            <div class="clarify-opts">
              ${(cl.options || []).map((o, i) => `<button class="clarify-btn" data-act="${esc(o.action || "")}"><span class="clarify-num">${i + 1}</span>${esc(o.label)}</button>`).join("")}
              ${cl.allow_free ? `<button class="clarify-btn ghost" data-act="free">其他 · 我自己重新描述</button>` : ""}
            </div>
          </div>`;
      }
      if (m.wizard && Object.keys(m.wizard).length) {
        if (m.wizard.created) {
          content += `
            <div class="wiz-card done">
              <div class="wiz-card__ic">${CHECK_ICON_SVG}</div>
              <div class="wiz-card__body">
                <div class="wiz-card__t">创建完成</div>
                <div class="wiz-card__s">定时任务已创建,可在「定时任务」会话或设置里查看。</div>
              </div>
            </div>`;
        } else {
          content += `
            <div class="wiz-card" data-mid="${esc(m.id)}">
              <div class="wiz-card__ic">${SESS_CLOCK_INLINE}</div>
              <div class="wiz-card__body">
                <div class="wiz-card__t">创建定时任务</div>
                <div class="wiz-card__s">我已根据你的描述预填好,点开核对并补全即可创建。</div>
              </div>
              <button class="wiz-card__btn" data-wiz-open="${esc(m.id)}">去创建</button>
            </div>`;
        }
      }
    }
    // 定时任务会话:每轮执行完,下方显示执行日期/时间 + token 用量
    if (!running && !awaitingDecrypt && state.currentSession
        && state.currentSession.kind === "scheduled" && m.created_at) {
      content += runMetaHtml(m);
    }
    // 漏跑补救说明:附在本轮执行时间下方,与该轮对话同属一个整体
    if (!running && !awaitingDecrypt && m.remediation_note) {
      content += `<div class="run-remed">${esc(m.remediation_note)}</div>`;
    }
  }
  content += `</div>`;
  wrap.innerHTML = avatar + content;

  // 歧义澄清卡:用户选择后按对应方式继续
  wrap.querySelectorAll(".clarify-card .clarify-btn").forEach(b => {
    b.addEventListener("click", async () => {
      const card = b.closest(".clarify-card"); const cmid = card.dataset.mid; const act = b.dataset.act;
      card.querySelectorAll(".clarify-btn").forEach(x => x.disabled = true);
      try {
        const r = await api("POST", `/api/sessions/${state.currentSid}/messages/${cmid}/clarify`, { choice: act });
        const msg = state.currentSession?.messages?.find(x => x.id === cmid);
        if (msg) msg.clarify = {};
        const rerender = () => {
          const node = document.querySelector(`.msg[data-mid="${cmid}"]`);
          if (node && msg) node.replaceWith(renderMessage(msg));
        };
        if (act === "wizard") {
          if (msg) msg.summary = "好的,来创建定时任务 👇";
          rerender();
          openTaskWizard(r.wizard || {}, cmid);
        } else if (act === "analyze") {
          if (msg) { msg.status = "pending"; msg.summary = ""; }
          rerender();
          setRunning(true, cmid);
          pollMessage(state.currentSid, cmid);
        } else {           // free:自己重述
          rerender();
          $("input")?.focus();
        }
      } catch (e) {
        alert("操作失败:" + e.message);
        card.querySelectorAll(".clarify-btn").forEach(x => x.disabled = false);
      }
    });
  });

  // 创建定时任务表单:只由卡片「去创建」按钮手动打开,**不再自动弹窗**(避免莫名弹出)
  wrap.querySelectorAll("[data-wiz-open]").forEach(b => {
    b.addEventListener("click", () => openTaskWizard(m.wizard || {}, m.id));
  });

  // 解密授权浮卡按钮 → POST decision
  wrap.querySelectorAll(".decrypt-card .dc-btn").forEach(b => {
    b.addEventListener("click", async () => {
      const card = b.closest(".decrypt-card");
      const mid = card?.dataset.mid;
      const choice = b.dataset.choice;
      if (!mid || !choice) return;
      // 锁定全部按钮防重复
      card.querySelectorAll(".dc-btn").forEach(x => x.disabled = true);
      card.querySelector(".dc-hint").textContent =
        choice === "decrypt" ? "已选择「解密展示」· 正在解密结果…" : "已选择「保留密文」· 正在导出未解密的 Excel…";
      try {
        await api(
          "POST",
          `/api/sessions/${state.currentSid}/messages/${mid}/decrypt_decision`,
          { choice },
        );
      } catch (e) {
        alert("提交选择失败:" + e.message);
        card.querySelectorAll(".dc-btn").forEach(x => x.disabled = false);
      }
    });
  });

  // 「保留密文」后的「解密查看明文」按钮 → 事后解密,显示明文文件卡
  wrap.querySelectorAll(".dec-file-btn").forEach(b => {
    b.addEventListener("click", async () => {
      const fmid = b.dataset.mid;
      if (!fmid) return;
      const orig = b.innerHTML;
      b.disabled = true; b.textContent = "解密中…";
      try {
        const r = await api("POST", `/api/sessions/${state.currentSid}/messages/${fmid}/decrypt_file`);
        // 回填到本地消息对象 → 重渲该条(密文卡 + 明文卡并列)
        const msg = state.currentSession?.messages?.find(x => x.id === fmid);
        if (msg) {
          msg.excel_path = r.excel_path; msg.excel_name = r.excel_name; msg.can_decrypt = false;
          const node = document.querySelector(`.msg[data-mid="${fmid}"]`);
          if (node) { state.typedMids.add(fmid); node.replaceWith(renderMessage(msg)); }
        }
      } catch (e) {
        alert("解密失败:" + e.message);
        b.disabled = false; b.innerHTML = orig;
      }
    });
  });

  // 渲染完成后:发现 data-typewriter 标记 → 启动逐字动画
  const bubble = wrap.querySelector('.bubble[data-typewriter]');
  if (bubble) {
    const full = bubble.getAttribute('data-typewriter') || "";
    bubble.removeAttribute('data-typewriter');
    state.typedMids.add(m.id);
    typewriter(bubble, full, () => {
      // 打字完 → 定格成 markdown 排版(标题/列表/加粗/可点链接)
      bubble.innerHTML = mdToHtml(full);
      // 让所有标记为 defer-reveal 的兄弟节点淡入
      wrap.querySelectorAll('[data-defer-reveal]').forEach(el => {
        el.removeAttribute('data-defer-reveal');
        el.classList.add('revealed');
      });
    });
  }
  return wrap;
}

// 打字机:逐字符注入 bubble,完成后调 onDone
function typewriter(node, fullText, onDone) {
  let i = 0;
  // 短文本快一些,长文本不要拖太久
  const total = fullText.length;
  const speed = total > 400 ? 8 : (total > 120 ? 15 : 25);  // ms / char
  const cursor = document.createElement("span");
  cursor.className = "type-cursor";
  cursor.textContent = "▍";
  node.innerHTML = "";
  node.appendChild(cursor);

  function tick() {
    if (i >= total) {
      cursor.remove();
      if (typeof onDone === "function") onDone();
      return;
    }
    // 一次注入若干字符,长文本不要让动画拖太久
    const burst = total > 400 ? 3 : (total > 120 ? 2 : 1);
    const slice = fullText.slice(i, i + burst);
    i += burst;
    // 文本插入到 cursor 之前(转义 + 换行)
    slice.split("").forEach(ch => {
      if (ch === "\n") {
        node.insertBefore(document.createElement("br"), cursor);
      } else {
        node.insertBefore(document.createTextNode(ch), cursor);
      }
    });
    const main = $("main");
    if (main && main.scrollHeight - main.scrollTop - main.clientHeight < 80) {
      main.scrollTop = main.scrollHeight;
    }
    setTimeout(tick, speed);
  }
  tick();
}

// ============ 发送/停止 按钮状态机 ============
// 停止图标:扁平实心方块,纯几何,无文字
const STOP_ICON_SVG = `
  <svg class="ic-stop" viewBox="0 0 24 24" aria-label="停止" role="img">
    <rect x="6" y="6" width="12" height="12" rx="2"></rect>
  </svg>`;

function setRunning(running, mid) {
  state.running = running;
  state.runningMid = mid || null;
  const btn = $("sendBtn");
  if (running) {
    btn.classList.add("stop");
    btn.innerHTML = STOP_ICON_SVG;
    btn.setAttribute("title", "停止");
  } else {
    btn.classList.remove("stop");
    btn.textContent = "发送";
    btn.removeAttribute("title");
  }
}

async function stopRunning() {
  const sid = state.currentSid, mid = state.runningMid;
  if (!sid || !mid) return;
  try {
    await api("POST", `/api/sessions/${sid}/messages/${mid}/cancel`);
  } catch (e) {
    // 已经结束或网络问题:不阻断 UI
    console.warn("cancel 调用失败:", e);
  }
  // 不主动 clearInterval —— 让 pollMessage 自己感知 cancelled 终态
}

// ============ 轮询 assistant 消息 ============
// 设计:running 期间只**追加**新出现的 step 行,不重渲整条消息 —— 保留:
//   ① 用户已经手动折叠/展开的状态
//   ② 滚动位置(只有真新内容到底部时才滚)
// 状态终结时(done/failed/needs_cipher)再一次性整体重渲。
function pollMessage(sid, mid) {
  if (state.pollingMids.has(mid)) return;   // 已在轮询,别重复起 interval
  state.pollingMids.add(mid);
  const since = Date.now();
  // 逐条亮出(自愈式):始终按「DOM 已显示条数」从服务端最新 steps 数组取下一条,
  // 每 230ms 一条 —— 中途整体重渲(刷新/授权浮卡)后自动对齐,不可能重复或乱序。
  let latestSteps = [];
  let revealTimer = null;
  let stopped = false;
  const stepsBoxOf = () => {
    const n = document.querySelector(`.msg[data-mid="${mid}"]`);
    return n ? n.querySelector(".trace-steps") : null;
  };
  const drain = () => {
    if (stopped || revealTimer) return;
    revealTimer = setTimeout(() => {
      revealTimer = null;
      if (stopped) return;
      const box = stepsBoxOf();
      if (box && box.children.length < latestSteps.length) {
        const s = latestSteps[box.children.length];   // 永远取"下一条",以 DOM 为准
        const wasNearBottom =
          ($("main").scrollHeight - $("main").scrollTop - $("main").clientHeight) < 80;
        const div = document.createElement("div");
        div.className = `step ${s.kind || "step"}`;
        div.textContent = s.label;
        // 新亮出的这步成为"当前活跃步"(脉冲),清掉上一步的活跃态
        box.querySelectorAll(".step.active").forEach(e => e.classList.remove("active"));
        div.classList.add("active");
        box.appendChild(div);
        if (wasNearBottom) $("main").scrollTop = $("main").scrollHeight;
        if (box.children.length < latestSteps.length) drain();
      }
    }, 230);
  };
  const intv = setInterval(async () => {
    try {
      const m = await api("GET", `/api/sessions/${sid}/messages/${mid}`);
      const node = document.querySelector(`.msg[data-mid="${mid}"]`);
      const el = node?.querySelector(".run-time");
      if (el) el.textContent = ((Date.now() - since) / 1000).toFixed(0) + "s";

      const terminal = (m.status === "done" || m.status === "failed" ||
                        m.status === "needs_cipher" || m.status === "cancelled");
      const awaitingDecrypt = (m.status === "awaiting_decrypt");

      // 出现授权门 → 主动重渲一次(把浮卡渲出来),但不终止轮询;
      // 浮卡已在则不再重渲(避免每 tick 重置折叠/滚动状态)
      if (awaitingDecrypt && node && !node.querySelector(".decrypt-card")) {
        const fresh = renderMessage(m);
        node.replaceWith(fresh);
        latestSteps = m.steps || [];   // 整体重渲已含全部 step,对齐基准
        $("main").scrollTop = $("main").scrollHeight;
      }
      const idx = state.currentSession?.messages?.findIndex(x => x.id === mid);
      if (idx >= 0) state.currentSession.messages[idx] = m;

      if (terminal) {
        stopped = true;
        // 终态:完整重渲 → 折叠态、显示 summary / Excel 卡 / 错误 / 取消
        if (node) {
          const fresh = renderMessage(m);
          node.replaceWith(fresh);
          $("main").scrollTop = $("main").scrollHeight;
        }
        clearInterval(intv);
        state.pollingMids.delete(mid);
        setRunning(false);
        loadSessions();
        return;
      }

      // running 增量:新 step 进显示队列,由 drain 逐条亮出(不一次性贴一堆)
      const steps = m.steps || [];
      if (node && steps.length) {
        let stepsBox = node.querySelector(".trace-steps");
        let traceDetails = node.querySelector("details.trace");
        // 极少数情况:首批 step 抵达前 trace 还没渲;补建一个
        if (!stepsBox) {
          const content = node.querySelector(".msg__content");
          if (content) {
            traceDetails = document.createElement("details");
            traceDetails.className = "trace";
            traceDetails.open = true;
            traceDetails.innerHTML =
              `<summary class="trace-summary">计算追踪 · 运行中</summary>
               <div class="trace-steps"></div>`;
            // 插入到 run-pill 前面(若有),否则放最前
            const pill = content.querySelector(".run-pill");
            content.insertBefore(traceDetails, pill || content.firstChild);
            stepsBox = traceDetails.querySelector(".trace-steps");
          }
        }
        if (stepsBox) {
          latestSteps = steps;
          if (stepsBox.children.length < steps.length) drain();
        }
      }
    } catch (e) {
      clearInterval(intv);
      state.pollingMids.delete(mid);
    }
  }, 600);
}

// ============ 当前会话后台同步 ============
// 定时任务在服务端往会话加消息,前端不是发起方 → 无法感知。
// 这里每 4s 拉一次当前会话,发现新消息 / 状态变化就接住并补轮询。
async function syncCurrentSession() {
  const sid = state.currentSid;
  if (!sid || !state.currentSession) return;
  // 本地有消息正在轮询 → 那条 pollMessage 自己驱动更新,sync 让路不打架
  if (state.pollingMids.size) return;
  let data;
  try { data = await api("GET", `/api/sessions/${sid}/messages`); }
  catch { return; }
  const fresh = data.messages || [];
  const cur = state.currentSession.messages || [];

  const curIds = new Set(cur.map(m => m.id));
  const newOnes = fresh.filter(m => !curIds.has(m.id));
  // 状态变了的(如某条从 pending 变 running / done)
  const curById = Object.fromEntries(cur.map(m => [m.id, m]));
  const changed = fresh.some(m => curById[m.id] && curById[m.id].status !== m.status);

  if (!newOnes.length && !changed) return;   // 无变化,不动

  // 已完成的 assistant 标记为"无需打字机"(避免同步时重播旧消息),
  // 但**新冒出来的**仍允许打字机(它们确实是新的)。
  cur.forEach(m => { if (m.role === "assistant" && m.status === "done") state.typedMids.add(m.id); });

  state.currentSession.messages = fresh;
  if (data.session && data.session.updated_at) { state.lastSeen[sid] = data.session.updated_at; _saveLastSeen(); }
  renderChat();
  if (state.currentTaskId) refreshTasksBadge();   // 定时会话有新结果 → 即时刷新待批红点
  // 给所有运行中的消息补轮询(pollMessage 自带去重)
  fresh.forEach(m => {
    if (m.role === "assistant" && (m.status === "pending" || m.status === "running" || m.status === "awaiting_decrypt")) {
      pollMessage(sid, m.id);
    }
  });
}

// ============ 发送消息 ============
function enableComposer() {
  $("input").disabled = false;
  $("sendBtn").disabled = false;
  $("input").focus();
}

async function sendMessage() {
  // 运行中按了"停止"
  if (state.running) {
    await stopRunning();
    return;
  }

  const text = $("input").value.trim();
  if (!text) return;
  if (!state.currentSid) await createSession();

  // 还在上传 cipher / 抽文本 阻塞
  if (state.pendingCipher && state.pendingCipher.uploading) {
    alert("等密文加密完成后再发送"); return;
  }
  if (state.pendingTexts.some(t => t.uploading)) {
    alert("等文本文件读取完成后再发送"); return;
  }

  $("input").value = "";
  $("input").style.height = "auto";

  const attached_cipher = state.pendingCipher?.path || "";
  const text_attachments = state.pendingTexts
    .filter(t => !t.uploading && t.content)
    .map(t => ({ name: t.name, content: t.content }));
  state.pendingCipher = null;
  state.pendingTexts = [];
  renderAttachChips();

  try {
    const res = await api("POST", `/api/sessions/${state.currentSid}/messages`, {
      content: text, attached_cipher, text_attachments,
      web_search: !!state.webSearch,
    });
    state.currentSession.messages.push(res.user_message, res.assistant_message);
    if (state.currentSession.messages.length === 2 && state.currentSession.title === "新会话") {
      state.currentSession.title = text.slice(0, 40);
    }
    renderChat();
    setRunning(true, res.assistant_message.id);
    pollMessage(state.currentSid, res.assistant_message.id);
    loadSessions();
  } catch (e) {
    alert("发送失败:" + e.message);
    setRunning(false);
  } finally {
    $("input").focus();
  }
}

// ============ 附件(密文 + 明文文本) ============
const DATA_EXTS = ["csv", "xlsx", "xls"];
const TEXT_EXTS = ["txt", "md", "markdown", "rst", "log", "text",
                   "docx", "pdf", "rtf", "html", "htm", "json", "yml", "yaml"];

function renderAttachChips() {
  const box = $("attachChips");
  const chips = [];
  if (state.pendingCipher) {
    const a = state.pendingCipher;
    chips.push(`
      <div class="att-chip cipher ${a.uploading ? "uploading" : ""}">
        <svg class="ic-tiny" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
        <span class="nm">${esc(a.name)}</span>
        ${a.size ? `<span class="sz">${a.size}</span>` : ""}
        <button class="rm" data-rm-cipher="1">×</button>
      </div>
    `);
  }
  state.pendingTexts.forEach((t, i) => {
    chips.push(`
      <div class="att-chip text ${t.uploading ? "uploading" : ""}">
        <svg class="ic-tiny" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <span class="nm">${esc(t.name)}</span>
        ${t.chars ? `<span class="sz">${t.chars} 字</span>` : ""}
        <button class="rm" data-rm-text="${i}">×</button>
      </div>
    `);
  });
  box.innerHTML = chips.join("");
  box.querySelector("[data-rm-cipher]")?.addEventListener("click", () => {
    state.pendingCipher = null; renderAttachChips();
  });
  box.querySelectorAll("[data-rm-text]").forEach(b => {
    b.addEventListener("click", () => {
      state.pendingTexts.splice(+b.dataset.rmText, 1);
      renderAttachChips();
    });
  });
}

async function handleFileAttach(filesArg) {
  // 支持单文件或多文件
  const files = filesArg instanceof FileList ? Array.from(filesArg)
                : Array.isArray(filesArg) ? filesArg
                : (filesArg ? [filesArg] : []);
  for (const file of files) {
    if (!file) continue;
    const ext = (file.name.split(".").pop() || "").toLowerCase();
    if (DATA_EXTS.includes(ext)) {
      await _attachDataFile(file);
    } else if (TEXT_EXTS.includes(ext)) {
      await _attachTextFile(file);
    } else {
      alert(`不支持的文件类型:.${ext}\n数据:${DATA_EXTS.join("/")}\n文本:${TEXT_EXTS.join("/")}`);
    }
  }
}

async function _attachDataFile(file) {
  // 同消息最多一个密文(replace 旧的)
  const chip = {
    name: file.name + " (加密中…)", uploading: true,
    size: (file.size / 1024).toFixed(1) + "KB",
  };
  state.pendingCipher = chip;
  renderAttachChips();
  const fd = new FormData(); fd.append("raw_file", file);
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

async function _attachTextFile(file) {
  const chip = { name: file.name + " (读取中…)", uploading: true, content: "", chars: 0 };
  state.pendingTexts.push(chip);
  renderAttachChips();
  const fd = new FormData(); fd.append("raw_file", file);
  try {
    const res = await api("POST", "/api/files/text_extract", fd, true);
    chip.name = res.name;
    chip.content = res.content;
    chip.chars = res.chars;
    chip.uploading = false;
    renderAttachChips();
  } catch (e) {
    const idx = state.pendingTexts.indexOf(chip);
    if (idx >= 0) state.pendingTexts.splice(idx, 1);
    renderAttachChips();
    alert("文本读取失败:" + e.message);
  }
}

function pickExistingCipher(path, name) {
  state.pendingCipher = { name: name || path.split("/").pop(), path, uploading: false };
  renderAttachChips();
  closeFilesModal();
  $("input")?.focus();
}

// ============ 设置 Modal ============
const TABS = {
  general: { title: "连接 / 计算", render: renderGeneralTab },
  ops:     { title: "自启 / 运维", render: renderOpsTab },
  skills:  { title: "Skill 管理", render: renderSkillsTab },
  keys:    { title: "同态密钥", render: renderKeysTab },
  audit:   { title: "可信审计", render: renderAuditTab },
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
function closeModal() { $("modalMask").classList.remove("open"); clearInterval(_opsTimer); }

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

// ============ 自启 / 运维 Tab(客户端自身开机自启 + 崩溃重启)============
let _opsTimer = null;
async function renderOpsTab() {
  $("modalBody").innerHTML = `
    <h2>${TABS.ops.title}</h2>
    <p class="sub">让本机的用户端(数据面 :8444)开机自动启动、崩溃后自动重启。仅作用于这台电脑。</p>
    <div id="opsAlert"></div>

    <div class="ops-cards" id="opsCards"></div>

    <div class="field" style="margin-top:18px; display:flex; gap:10px; flex-wrap:wrap;">
      <button class="btn-primary" id="opsEnable">启用开机自启 + 守护</button>
      <button class="btn-danger" id="opsDisable">停用</button>
      <button class="btn-ghost" id="opsStart">仅启动守护(本次)</button>
      <button class="btn-ghost" id="opsStop">停止守护</button>
    </div>

    <div class="alert-box info" style="margin-top:16px; line-height:1.7;">
      <strong>原理:</strong>开机自启在登录时拉起一个守护进程 <code>client_supervisor.py</code>,
      由它启动、健康探测、崩溃后退避重启用户端(:8444)。三平台一致:
      macOS 用 <code>LaunchAgent</code>、Linux 用 <code>systemd --user</code>、Windows 用计划任务。
      与控制面 Host 的守护相互独立,互不影响。<br>
      <strong>停用</strong>会移除开机自启并停止守护(用户端本身保留,界面不掉线)。
    </div>`;

  const renderCards = (s) => {
    const sup = s.supervisor || {}, au = s.autostart || {}, cl = s.client || {};
    $("opsCards").innerHTML = `
      <div class="ops-card"><div class="ops-card__label">开机自启</div>
        <div class="ops-card__val ${au.installed ? "ok" : "bad"}">${au.installed ? "已启用" : "未启用"}</div>
        <div class="ops-card__hint">${esc(au.detail || "")}</div></div>
      <div class="ops-card"><div class="ops-card__label">守护进程</div>
        <div class="ops-card__val ${sup.running ? "ok" : "bad"}">${sup.running ? "运行中" : "未运行"}</div>
        <div class="ops-card__hint">${sup.pid ? "pid " + sup.pid : "崩溃自愈未生效"}</div></div>
      <div class="ops-card"><div class="ops-card__label">用户端 · :${cl.port || 8444}</div>
        <div class="ops-card__val ${cl.healthy ? "ok" : "bad"}">${cl.healthy ? "健康" : "不可达"}</div>
        <div class="ops-card__hint">${cl.managed ? "受守护 · 重启 " + (cl.restarts || 0) + " 次" : "未受守护"}</div></div>`;
  };
  const refresh = async () => {
    try { renderCards(await api("GET", "/api/ops/status")); } catch (e) {}
  };
  await refresh();
  clearInterval(_opsTimer);
  _opsTimer = setInterval(() => { if ($("opsCards")) refresh(); else clearInterval(_opsTimer); }, 4000);

  const act = async (path, btn, busy) => {
    const orig = btn.textContent; btn.disabled = true; btn.textContent = busy;
    try {
      const r = await api("POST", path);
      $("opsAlert").innerHTML = `<div class="alert-box success">${esc(r.msg || "已完成")}</div>`;
    } catch (e) {
      $("opsAlert").innerHTML = `<div class="alert-box">操作失败:${esc(e.message)}</div>`;
    } finally { btn.disabled = false; btn.textContent = orig; await refresh(); }
  };
  $("opsEnable").addEventListener("click", e => act("/api/ops/autostart/enable", e.target, "启用中…"));
  $("opsDisable").addEventListener("click", e => { if (confirm("停用开机自启并停止守护?用户端会继续运行,但崩溃后不再自动重启。")) act("/api/ops/autostart/disable", e.target, "停用中…"); });
  $("opsStart").addEventListener("click", e => act("/api/ops/supervisor/start", e.target, "启动中…"));
  $("opsStop").addEventListener("click", e => act("/api/ops/supervisor/stop", e.target, "停止中…"));
}

// ============ 定时任务 Tab ============
// ============ 创建定时任务向导(内联在输入框上方 · 一步步补全)============
let _wizBound = false;
function bindWizard() {
  if (_wizBound) return;
  _wizBound = true;
  $("wizClose")?.addEventListener("click", closeTaskWizard);
  $("wizBack")?.addEventListener("click", wizBack);
  $("wizNext")?.addEventListener("click", wizNext);
}

function closeTaskWizard() {
  state.wizard = null;
  const bar = $("wizBar");
  if (bar) bar.hidden = true;
  _syncFooter();   // 向导关闭后,若仍在总览则重新隐藏 footer
}

// 新建定时任务统一走单页表单弹窗(取代旧的底部分步向导);prefill 来自普通会话的意图抽取
function openTaskWizard(prefill, fromMid) {
  openCreateTaskForm(prefill || {}, fromMid);
}

// 判断输入是否为"明确的任务/指令"(拒绝随便输入「1」「...」之类)
// 注意:JS 的 \W 会把中文当作非单词字符,绝不能用 \W 判"纯符号",否则中文任务全被误杀。
function _meaningfulTask(q) {
  q = (q || "").trim();
  if (q.length < 4) return false;
  // 必须至少含一个中文或英文字母(纯数字 / 纯标点 / 「1」「...」都会被挡)
  return /[一-龥a-zA-Z]/.test(q);
}

// 从第一个未填好的步骤开始
function _firstIncompleteStep(s) {
  if (!_meaningfulTask(s.question)) return "question";
  if (!s.cron) return "schedule";
  if (s.data_source === "folder" && !s.source_folder) return "data";
  if (s.data_source === "none") return "confirm";
  if (s.data_source !== "none" && !s.output_folder) return "output";
  return "confirm";
}

function wizActiveSteps() {
  const s = state.wizard.slots;
  const steps = ["question", "schedule", "data"];
  if (s.data_source !== "none") steps.push("output");
  steps.push("confirm");
  return steps;
}

function wizSaveCurrent() {
  const s = state.wizard.slots;
  const k = state.wizard.stepKey;
  if (k === "question") { const v = $("wizQuestion"); if (v) s.question = v.value.trim(); }
  if (k === "schedule") { const v = $("wizSchedule"); if (v) s.schedule_text = v.value.trim(); }
  if (k === "confirm") { const v = $("wizName"); if (v) s.name = v.value.trim(); }
}

function renderWizard() {
  const w = state.wizard; if (!w) return;
  const s = w.slots;
  const steps = wizActiveSteps();
  const idx = Math.max(0, steps.indexOf(w.stepKey));
  $("wizDots").innerHTML = steps.map((k, i) =>
    `<span class="wiz-dot ${i === idx ? "on" : ""} ${i < idx ? "done" : ""}"></span>`).join("");
  const body = $("wizBody");

  if (w.stepKey === "question") {
    body.innerHTML = `
      <div class="wiz-q">问题 / 指令</div>
      <textarea id="wizQuestion" rows="3" placeholder="例:按大区统计本月回款率 TOP10 / 查上海明天天气 / 汇总今天的行业新闻">${esc(s.question)}</textarea>
      <div class="wiz-hint">写清楚每次到点要做什么 —— 可以是对你数据的密态分析,也可以是查天气 / 新闻 / 资料等联网信息。</div>`;
  } else if (w.stepKey === "schedule") {
    body.innerHTML = `
      <div class="wiz-q">多久跑一次?</div>
      <input id="wizSchedule" placeholder="例:每天早上9点 / 每周一三五9点 / 每月1号" value="${esc(s.schedule_text)}">
      <div class="wiz-cron" id="wizCronPreview"></div>
      <div class="wiz-hint">用大白话写时间,系统自动转成排程。</div>`;
    const inp = $("wizSchedule"); const prev = $("wizCronPreview");
    const refresh = async () => {
      const txt = inp.value.trim();
      if (!txt) { prev.textContent = ""; s.cron = ""; return; }
      try {
        const r = await api("POST", "/api/scheduled_tasks/parse_schedule", { text: txt });
        if (r.ok) { s.cron = r.cron; s.cron_readable = r.readable; prev.innerHTML = `<span class="ok">✓ ${esc(r.readable)}</span> <code>${esc(r.cron)}</code>`; }
        else { s.cron = ""; prev.innerHTML = `<span class="bad">${esc(r.error || "没听懂")}</span>`; }
      } catch (e) { prev.textContent = ""; }
    };
    inp.addEventListener("input", () => { clearTimeout(w._t); w._t = setTimeout(refresh, 300); });
    if (inp.value.trim()) refresh();
  } else if (w.stepKey === "data") {
    body.innerHTML = `
      <div class="wiz-q">用哪份数据?</div>
      <label class="wiz-radio"><input type="radio" name="wizDS" value="folder" ${s.data_source === "folder" ? "checked" : ""}>
        <span>绑定数据文件夹(每次取最新文件自动加密分析)<b class="wiz-rec">推荐</b></span></label>
      <div class="wiz-sub" data-for="folder">
        <button class="wiz-pick" id="wizPickFolder">${FOLDER_ICON_SVG}选择文件夹</button>
        <span class="wiz-path" id="wizFolderPath">${s.source_folder ? esc(s.source_folder) : "未选择"}</span>
      </div>
      <label class="wiz-radio"><input type="radio" name="wizDS" value="none" ${s.data_source === "none" ? "checked" : ""}>
        <span>不需要数据(定期问答 / 查天气、新闻、资料等)</span></label>
      <div class="wiz-sub" style="border-top:1px solid var(--border); margin-top:6px; padding-top:10px; padding-left:0;">
        <label class="cw-switch">
          <input type="checkbox" id="wizWeb" ${s.web_search ? "checked" : ""}>
          <span class="cw-switch__body">
            <span class="cw-switch__head">联网搜索<span class="cw-switch__state"></span></span>
            <span class="hint-inline">${s.data_source === "none"
              ? "查实时天气 / 新闻 / 资料时建议开启"
              : "数据分析时,若没提供公式/口径,让 AI 上网查找标准计算方法"}</span>
          </span>
          <span class="cw-switch__track"><span class="cw-switch__thumb"></span></span>
        </label>
      </div>`;
    body.querySelectorAll('input[name="wizDS"]').forEach(r =>
      r.addEventListener("change", () => {
        s.data_source = r.value;
        s.web_search = (r.value === "none");   // 选「不需要数据」→ 自动勾选联网搜索;否则关闭
        renderWizard();
      }));
    const webCb = $("wizWeb");
    if (webCb) webCb.addEventListener("change", () => { s.web_search = webCb.checked; });
    const pf = $("wizPickFolder");
    if (pf) pf.addEventListener("click", async () => {
      try {
        const r = await api("POST", "/api/pick_folder", {});
        if (!r.cancelled && r.path) { s.source_folder = r.path; $("wizFolderPath").textContent = r.path; }
      } catch (e) { alert("选择失败:" + e.message); }
    });
  } else if (w.stepKey === "output") {
    body.innerHTML = `
      <div class="wiz-q">结果放哪个文件夹?</div>
      <div class="wiz-sub" style="padding-left:0;">
        <button class="wiz-pick" id="wizPickOut">${FOLDER_ICON_SVG}选择输出文件夹</button>
        <span class="wiz-path" id="wizOutPath">${s.output_folder ? esc(s.output_folder) : "未选择"}</span>
      </div>
      <div class="wiz-hint">系统会在该文件夹里自动建 <code>密文/</code> 和 <code>明文/</code>:
        每轮结果先以密文存入 <code>密文/</code>(未授权也可留存);你授权解密后,明文存入 <code>明文/</code>。</div>`;
    $("wizPickOut").addEventListener("click", async () => {
      try {
        const r = await api("POST", "/api/pick_folder", {});
        if (!r.cancelled && r.path) { s.output_folder = r.path; $("wizOutPath").textContent = r.path; }
      } catch (e) { alert("选择失败:" + e.message); }
    });
  } else if (w.stepKey === "confirm") {
    if (!s.name) s.name = (s.question || "定时任务").slice(0, 8);
    const dsLabel = (s.data_source === "folder" ? `数据文件夹:${s.source_folder || "(未选)"}` : "不需要数据")
      + (s.web_search ? " · 联网搜索" : "");
    body.innerHTML = `
      <div class="wiz-q">确认并创建</div>
      <label class="wiz-lbl">任务名</label>
      <input id="wizName" value="${esc(s.name)}" placeholder="给任务起个名">
      <ul class="wiz-summary">
        <li><b>执行</b>${esc(s.question || "(空)")}</li>
        <li><b>排程</b>${esc(s.cron_readable || s.schedule_text || "(未设)")} <code>${esc(s.cron || "")}</code></li>
        <li><b>数据</b>${esc(dsLabel)}</li>
        ${s.data_source !== "none" ? `<li><b>输出</b>${esc(s.output_folder || "(默认 下载/任务名)")} <span class="wiz-hint2">· 自动分 密文/明文</span></li>` : ""}
      </ul>`;
  }

  const isLast = (w.stepKey === "confirm");
  $("wizBack").style.visibility = (idx === 0) ? "hidden" : "visible";
  $("wizNext").disabled = false;   // 复位:上一次创建结束会把它置 disabled,重开时必须解锁
  $("wizNext").textContent = isLast ? "创建任务" : "下一步";
}

function wizBack() {
  const w = state.wizard; if (!w) return;
  wizSaveCurrent();
  const steps = wizActiveSteps();
  const idx = steps.indexOf(w.stepKey);
  if (idx > 0) { w.stepKey = steps[idx - 1]; renderWizard(); }
}

async function wizNext() {
  const w = state.wizard; if (!w) return;
  wizSaveCurrent();
  const s = w.slots;
  // 校验当前步
  if (w.stepKey === "question" && !_meaningfulTask(s.question))
    return alert("请输入明确的问题/指令,例如「按大区统计本月回款率 TOP10」(不能只填「1」之类)");
  if (w.stepKey === "schedule" && !s.cron) return alert("排程没识别成功 · 换个写法,如「每天早上9点」");
  if (w.stepKey === "data") {
    if (s.data_source === "folder" && !s.source_folder) return alert("请选择数据文件夹");
    if (s.data_source === "cipher" && !s.cipher_path) return alert("请选择一份已加密文件");
  }
  const steps = wizActiveSteps();
  const idx = steps.indexOf(w.stepKey);
  if (idx < steps.length - 1) { w.stepKey = steps[idx + 1]; renderWizard(); return; }
  // 最后一步 → 创建
  await submitWizard();
}

async function submitWizard() {
  const s = state.wizard.slots;
  const btn = $("wizNext");
  if (btn) { btn.disabled = true; btn.textContent = "创建中…"; }
  let created = false, newTask = null;
  try {
    newTask = await api("POST", "/api/scheduled_tasks", {
      name: s.name || (s.question || "定时任务").slice(0, 8),
      question: s.question,
      schedule_kind: "cron", cron_expr: s.cron, cron_readable: s.cron_readable,
      source_folder: s.data_source === "folder" ? s.source_folder : "",
      output_folder: s.data_source !== "none" ? s.output_folder : "",
      web_search: !!s.web_search,   // 无数据查实时信息,或有数据时上网找公式/口径
      enabled: true,
    });
    created = true;
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "创建任务"; }
    alert("创建失败:" + e.message);
    return;
  }
  // 创建已成功 —— 把触发这次向导的消息卡标记为「创建完成」(持久化 + 本地重渲)
  const fromMid = state.wizard?.fromMid;
  closeTaskWizard();
  if (fromMid && state.currentSid) {
    try { await api("POST", `/api/sessions/${state.currentSid}/messages/${fromMid}/wizard_done`); } catch (_) {}
    const msg = state.currentSession?.messages?.find(x => x.id === fromMid);
    if (msg) {
      msg.wizard = Object.assign({}, msg.wizard, { created: true });
      const node = document.querySelector(`.msg[data-mid="${fromMid}"]`);
      if (node) node.replaceWith(renderMessage(msg));
    }
  }
  try {
    await loadSessions();
    setSessionView("scheduled");   // 切到定时任务视图,展示新建的任务会话
    if (newTask && newTask.session_id) await selectSession(newTask.session_id);
  } catch (_) {}
  try { toast("定时任务已创建 ✓"); } catch (_) {}
}

function toast(msg) {
  let t = $("cwToast");
  if (!t) { t = document.createElement("div"); t.id = "cwToast"; t.className = "cw-toast"; document.body.appendChild(t); }
  t.textContent = msg; t.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 3200);
}

async function renderTasksTab() {
  $("modalBody").innerHTML = `
    <h2>${TABS.tasks.title}</h2>
    <p class="sub">到点自动触发 · 无密文任务在你登录时直接跑 · 带密文的分析进待批队列(你回来批准才解密)</p>
    <div id="tasksAlert"></div>

    <h3 style="font-size:14px; margin:18px 0 8px;">待批运行 <span id="pendBadge" class="badge no" style="display:none;">0</span></h3>
    <div id="pendingList"></div>

    <h3 style="font-size:14px; margin:22px 0 8px;">我的任务</h3>
    <div id="taskList"></div>

    <h3 style="font-size:14px; margin:22px 0 8px;">新建任务</h3>
    <div class="skill-form">
      <div class="form-grid">
        <div class="field"><label>任务名</label><input type="text" id="tkName" placeholder="每日回款率日报"></div>
        <div class="field"><label>周期</label>
          <select id="tkKind">
            <option value="daily">每天</option>
            <option value="weekly">每周</option>
            <option value="monthly">每月</option>
            <option value="interval">间隔</option>
            <option value="cron">自定义</option>
          </select>
        </div>
      </div>
      <div class="form-grid" id="tkTimeRow">
        <div class="field" id="tkWeekdayWrap" style="display:none;"><label>星期</label>
          <select id="tkWeekday">
            <option value="0">周一</option><option value="1">周二</option><option value="2">周三</option>
            <option value="3">周四</option><option value="4">周五</option><option value="5">周六</option><option value="6">周日</option>
          </select>
        </div>
        <div class="field" id="tkMonthDayWrap" style="display:none;"><label>每月几号</label>
          <input type="number" id="tkMonthDay" value="1" min="1" max="28">
        </div>
        <div class="field" id="tkClockWrap"><label>时间</label>
          <input type="time" id="tkTime" value="09:00">
        </div>
        <div class="field" id="tkIntervalWrap" style="display:none;"><label>每隔(分钟)</label>
          <input type="number" id="tkInterval" value="60" min="1">
        </div>
      </div>
      <div class="field" id="tkCronWrap" style="display:none;">
        <label>描述 <span class="hint-inline">如:每月1号、每周一三五9点、每天晚上8点</span></label>
        <input type="text" id="tkCronNL" placeholder="每月1号9点">
        <div class="cron-presets">
          <button type="button" class="cron-chip" data-nl="每月1号9点">每月1号</button>
          <button type="button" class="cron-chip" data-nl="工作日上午9点">工作日</button>
          <button type="button" class="cron-chip" data-nl="每周一三五9点">周一三五</button>
          <button type="button" class="cron-chip" data-nl="每天晚上8点">每天晚上8点</button>
          <button type="button" class="cron-chip" data-nl="每2小时">每2小时</button>
        </div>
        <div id="cronResult" class="cron-result">识别结果会显示在这里</div>
        <input type="hidden" id="tkCron" value="">
      </div>
      <div class="field"><label>数据源</label>
        <select id="tkSource">
          <option value="none">不绑定(自由问答 · 自动跑)</option>
          <option value="folder">绑定文件夹(每次取最新 · 数据自动刷新)</option>
        </select>
      </div>
      <div class="field" id="tkFolderWrap" style="display:none;">
        <label>数据文件夹 <span class="hint-inline">到点取最新 CSV/XLSX 自动加密再分析</span></label>
        <div class="folder-pick">
          <input type="text" id="tkFolder" placeholder="点右侧选择,或粘贴绝对路径">
          <button type="button" class="btn-ghost btn-sm" id="tkFolderBtn">选择文件夹</button>
        </div>
        <p class="hint">把每期新数据丢进这个文件夹,任务永远处理最新那份(密态计算 · 结果加密暂存待解密)</p>
      </div>
      <div class="field" id="tkOutputWrap" style="display:none;">
        <label>输出文件夹 <span class="hint-inline">留空=默认 下载/任务名</span></label>
        <div class="folder-pick">
          <input type="text" id="tkOutput" placeholder="点右侧选择,或粘贴绝对路径">
          <button type="button" class="btn-ghost btn-sm" id="tkOutputBtn">选择文件夹</button>
        </div>
        <p class="hint">结果落到这里,自动分 <code>密文/</code> 与 <code>明文/</code>:每轮密文存「密文/」,授权解密后明文存「明文/」</p>
      </div>
      <div class="field"><label>问题 / 指令</label>
        <textarea id="tkQuestion" rows="2" placeholder="按大区统计每位代表的回款率,降序导 Excel"></textarea>
      </div>
      <button class="btn-primary" id="tkAdd">创建任务</button>
    </div>

    <details class="hist-details" style="margin-top:22px;">
      <summary class="hist-summary">运行历史 <span id="histCount" class="badge ok"></span></summary>
      <div class="hist-filter">
        <label class="hint-inline" style="margin:0;">按日期</label>
        <input type="date" id="histDate" class="hist-sel">
        <button class="btn-ghost btn-sm" id="histClear">清除</button>
      </div>
      <div id="tkHistory"></div>
    </details>
  `;

  // 周期切换显隐
  const kindSel = $("tkKind");
  function syncKind() {
    const k = kindSel.value;
    $("tkWeekdayWrap").style.display = k === "weekly" ? "" : "none";
    $("tkMonthDayWrap").style.display = k === "monthly" ? "" : "none";
    $("tkClockWrap").style.display = ["daily", "weekly", "monthly"].includes(k) ? "" : "none";
    $("tkIntervalWrap").style.display = k === "interval" ? "" : "none";
    $("tkCronWrap").style.display = k === "cron" ? "" : "none";
    $("tkTimeRow").style.display = k === "cron" ? "none" : "";
  }
  kindSel.addEventListener("change", syncKind); syncKind();

  // 自定义(大白话)→ cron:实时解析(防抖)
  let cronTimer = null;
  async function parseCronNL() {
    const text = $("tkCronNL").value.trim();
    const box = $("cronResult");
    if (!text) { box.className = "cron-result"; box.textContent = "识别结果会显示在这里"; $("tkCron").value = ""; return; }
    try {
      const r = await api("POST", "/api/scheduled_tasks/parse_schedule", { text });
      if (r.ok) {
        box.className = "cron-result ok";
        box.innerHTML = `✓ <strong>${esc(r.readable)}</strong> <span class="cron-code">${esc(r.cron)}</span>`;
        $("tkCron").value = r.cron;
      } else {
        box.className = "cron-result bad";
        box.textContent = r.error || "没识别出来";
        $("tkCron").value = "";
      }
    } catch (e) {
      box.className = "cron-result bad"; box.textContent = "解析失败:" + e.message; $("tkCron").value = "";
    }
  }
  $("tkCronNL").addEventListener("input", () => {
    clearTimeout(cronTimer); cronTimer = setTimeout(parseCronNL, 350);
  });
  $("tkCronWrap").querySelectorAll(".cron-chip").forEach(b => {
    b.addEventListener("click", () => { $("tkCronNL").value = b.dataset.nl; parseCronNL(); });
  });

  // 数据源切换
  const srcSel = $("tkSource");
  function syncSource() {
    const isFolder = srcSel.value === "folder";
    $("tkFolderWrap").style.display = isFolder ? "" : "none";
    $("tkOutputWrap").style.display = isFolder ? "" : "none";   // 有数据才需输出夹
  }
  srcSel.addEventListener("change", syncSource); syncSource();

  // 原生选择文件夹(macOS / Windows / Linux)
  const bindFolderPick = (btnId, inputId) => {
    $(btnId).addEventListener("click", async () => {
      const btn = $(btnId); const orig = btn.textContent;
      btn.disabled = true; btn.textContent = "选择中…";
      try {
        const r = await api("POST", "/api/pick_folder");
        if (!r.cancelled && r.path) $(inputId).value = r.path;
      } catch (e) {
        $("tasksAlert").innerHTML = `<div class="alert-box">选择失败:${esc(e.message)} · 可手动粘贴路径</div>`;
      } finally {
        btn.disabled = false; btn.textContent = orig;
      }
    });
  };
  bindFolderPick("tkFolderBtn", "tkFolder");
  bindFolderPick("tkOutputBtn", "tkOutput");

  await loadTasksData();
  renderPendingList();
  renderTaskList();
  renderTaskHistory();

  // 运行历史:按日期筛选(空 = 全部)
  const histDate = $("histDate");
  histDate.value = state.histFilter.date || "";
  histDate.addEventListener("change", () => { state.histFilter.date = histDate.value; renderTaskHistory(); });
  $("histClear").addEventListener("click", () => {
    state.histFilter.date = ""; histDate.value = ""; renderTaskHistory();
  });

  $("tkAdd").addEventListener("click", async () => {
    const name = $("tkName").value.trim();
    const question = $("tkQuestion").value.trim();
    if (!name || !question) { $("tasksAlert").innerHTML = '<div class="alert-box">任务名和问题都要填</div>'; return; }
    const kind = $("tkKind").value;
    const [hh, mm] = ($("tkTime").value || "09:00").split(":").map(x => parseInt(x, 10));
    const src = $("tkSource").value;
    if (src === "folder" && !$("tkFolder").value.trim()) {
      $("tasksAlert").innerHTML = '<div class="alert-box">请填文件夹路径</div>'; return;
    }
    if (kind === "cron" && !$("tkCron").value.trim()) {
      $("tasksAlert").innerHTML = '<div class="alert-box">请输入能识别的排程描述(如「每月1号」)</div>'; return;
    }
    const body = {
      name, question, schedule_kind: kind,
      source_folder: src === "folder" ? $("tkFolder").value.trim() : "",
      output_folder: src === "folder" ? $("tkOutput").value.trim() : "",
      at_hour: hh || 0, at_minute: mm || 0,
      weekday: parseInt($("tkWeekday").value, 10) || 0,
      day_of_month: parseInt($("tkMonthDay").value, 10) || 1,
      interval_minutes: parseInt($("tkInterval").value, 10) || 60,
      cron_expr: kind === "cron" ? $("tkCron").value.trim() : "",
      cron_readable: kind === "cron" ? ($("cronResult").querySelector("strong")?.textContent || "") : "",
    };
    try {
      await api("POST", "/api/scheduled_tasks", body);
      $("tkName").value = ""; $("tkQuestion").value = ""; $("tkFolder").value = ""; if ($("tkOutput")) $("tkOutput").value = "";
      if ($("tkCronNL")) { $("tkCronNL").value = ""; $("tkCron").value = ""; const cr=$("cronResult"); if(cr){cr.className="cron-result";cr.textContent="识别结果会显示在这里";} }
      $("tasksAlert").innerHTML = '<div class="alert-box success">任务已创建</div>';
      await loadTasksData(); renderTaskList();
    } catch (e) {
      $("tasksAlert").innerHTML = `<div class="alert-box">创建失败:${esc(e.message)}</div>`;
    }
  });
}

async function loadTasksData() {
  try {
    const r = await api("GET", "/api/scheduled_tasks");
    state.tasks = r.tasks || [];
    state.tasksPendingCount = r.pending_count || 0;
  } catch { state.tasks = []; state.tasksPendingCount = 0; }
  try { state.tasksPending = await api("GET", "/api/scheduled_tasks/pending"); }
  catch { state.tasksPending = { runs: [], encrypted: [], missed: [] }; }
  try { state.tasksHistory = await api("GET", "/api/scheduled_tasks/history"); }
  catch { state.tasksHistory = []; }
}

const WEEK_CN = ["周一","周二","周三","周四","周五","周六","周日"];
function scheduleText(t) {
  const hm = `${String(t.at_hour).padStart(2,"0")}:${String(t.at_minute).padStart(2,"0")}`;
  if (t.schedule_kind === "cron") return t.cron_readable || `自定义 (${t.cron_expr})`;
  if (t.schedule_kind === "interval") return `每 ${t.interval_minutes} 分钟`;
  if (t.schedule_kind === "weekly") return `每${WEEK_CN[t.weekday]||"周一"} ${hm}`;
  if (t.schedule_kind === "monthly") return `每月 ${t.day_of_month} 号 ${hm}`;
  return `每天 ${hm}`;
}

function renderPendingList() {
  const box = $("pendingList"); if (!box) return;
  const data = state.tasksPending || { runs: [], encrypted: [], missed: [] };
  const runs = data.runs || [];
  const enc = data.encrypted || [];
  const missed = data.missed || [];
  const total = runs.length + enc.length + missed.length;
  const badge = $("pendBadge");
  if (badge) { badge.style.display = total ? "" : "none"; badge.textContent = total; }
  if (!total) { box.innerHTML = '<div class="alert-box info">没有待批运行</div>'; return; }

  let html = "";
  // 漏跑预警(最优先)—— 设定时间未执行(服务当时没运行 / 未登录 / 无数据)
  missed.forEach(m => {
    html += `
    <div class="list-item missed">
      <div class="grow">
        <div class="t">⚠ ${esc(m.task_name)} <span class="badge danger">漏跑</span></div>
        <div class="d">${esc((m.question||"").slice(0,80))}${(m.question||"").length>80?'…':''}</div>
        <div class="d" style="font-size:11px;">本应:${esc((m.due_at||"").slice(0,16).replace("T"," "))} · ${esc(m.reason||"")}</div>
      </div>
      <button class="btn-primary btn-sm" data-remediate="${esc(m.id)}" data-needs="${m.needs_data?1:0}">手动补救</button>
      <button class="btn-ghost btn-sm" data-missdismiss="${esc(m.id)}">忽略</button>
    </div>`;
  });
  // 密态任务聚合(1 任务 1 条,无论跑了几次)
  enc.forEach(a => {
    html += `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(a.task_name)} <span class="badge warn">密态 · ${a.count} 次待解密</span></div>
        <div class="d">${esc((a.question||"").slice(0,80))}${(a.question||"").length>80?'…':''}</div>
        <div class="d" style="font-size:11px;">最近:${esc((a.latest_run||"").slice(0,16).replace("T"," "))}</div>
      </div>
      <button class="btn-primary btn-sm" data-decrypt="${esc(a.task_id)}">解密 → 文件夹</button>
    </div>`;
  });
  // 自由问答待跑
  runs.forEach(p => {
    html += `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(p.task_name)} <span class="badge ok">待运行</span></div>
        <div class="d">${esc((p.question||"").slice(0,80))}${(p.question||"").length>80?'…':''}</div>
      </div>
      <button class="btn-primary btn-sm" data-approve="${esc(p.id)}">运行</button>
      <button class="btn-ghost btn-sm" data-dismiss="${esc(p.id)}">忽略</button>
    </div>`;
  });
  box.innerHTML = html;

  box.querySelectorAll("[data-decrypt]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "解密中…";
    try {
      const r = await api("POST", `/api/scheduled_tasks/decrypt/${b.dataset.decrypt}`);
      let msg = `<div class="alert-box success">✓ 已解密 ${r.count} 次运行 → 文件夹:<span class="mono">${esc(r.folder)}</span></div>`;
      if (r.failed) {
        const firstErr = (r.failures && r.failures[0] && r.failures[0].error) || "";
        msg += `<div class="alert-box">⚠ ${r.failed} 次运行解密失败,已保留待批可重试:${esc(firstErr)}</div>`;
      }
      $("tasksAlert").innerHTML = msg;
      await loadTasksData(); renderPendingList(); renderTaskHistory(); refreshTasksBadge();
    } catch (e) {
      $("tasksAlert").innerHTML = `<div class="alert-box">解密失败:${esc(e.message)}</div>`;
      b.disabled = false; b.textContent = "解密 → 文件夹";
    }
  }));
  box.querySelectorAll("[data-approve]").forEach(b => b.addEventListener("click", async () => {
    try {
      const r = await api("POST", `/api/scheduled_tasks/pending/${b.dataset.approve}/approve`);
      closeModal();
      await loadSessions();
      if (r.session_id) await selectSession(r.session_id);
      refreshTasksBadge();
    } catch (e) { $("tasksAlert").innerHTML = `<div class="alert-box">运行失败:${esc(e.message)}</div>`; }
  }));
  box.querySelectorAll("[data-dismiss]").forEach(b => b.addEventListener("click", async () => {
    await api("POST", `/api/scheduled_tasks/pending/${b.dataset.dismiss}/dismiss`);
    await loadTasksData(); renderPendingList(); refreshTasksBadge();
  }));
  // 漏跑:手动补救(需数据→先弹文件选择,每次手动指定该轮文件)/ 忽略
  box.querySelectorAll("[data-remediate]").forEach(b => b.addEventListener("click", async () => {
    const mid = b.dataset.remediate; const needs = b.dataset.needs === "1";
    let sourcePath = "";
    if (needs) {
      try {
        const pick = await api("POST", "/api/pick_file");
        if (pick.cancelled || !pick.path) return;       // 用户取消
        sourcePath = pick.path;
      } catch (e) { $("tasksAlert").innerHTML = `<div class="alert-box">选择文件失败:${esc(e.message)}</div>`; return; }
    }
    b.disabled = true; b.textContent = "补救中…";
    try {
      const r = await api("POST", `/api/scheduled_tasks/missed/${mid}/remediate`, { source_path: sourcePath });
      closeModal();
      await loadSessions();
      if (r.session_id) await selectSession(r.session_id);
      refreshTasksBadge();
      toast("已重跑该轮 · 见会话" + (r.needs_approval ? "(算完在待批里解密)" : ""));
    } catch (e) {
      $("tasksAlert").innerHTML = `<div class="alert-box">补救失败:${esc(e.message)}</div>`;
      b.disabled = false; b.textContent = "手动补救";
    }
  }));
  box.querySelectorAll("[data-missdismiss]").forEach(b => b.addEventListener("click", async () => {
    await api("POST", `/api/scheduled_tasks/missed/${b.dataset.missdismiss}/dismiss`);
    await loadTasksData(); renderPendingList(); refreshTasksBadge();
  }));
}

function renderTaskList() {
  const box = $("taskList"); if (!box) return;
  const list = state.tasks || [];
  if (!list.length) { box.innerHTML = '<div class="alert-box info">还没有定时任务</div>'; return; }
  box.innerHTML = list.map(t => `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(t.name)}
          ${t.needs_approval ? '<span class="badge warn">密态·需批准</span>' : '<span class="badge ok">自由问答</span>'}
          ${t.enabled ? '' : '<span class="badge no">已停用</span>'}</div>
        <div class="d">${esc(scheduleText(t))} · 下次:${esc((t.next_run||"").slice(0,16).replace("T"," "))}</div>
        ${t.source_folder
          ? `<div class="d mono" style="font-size:11px;">📁 文件夹(取最新):${esc(t.source_folder)}</div>`
          : (t.cipher_path ? `<div class="d mono" style="font-size:11px;">密文:${esc(t.cipher_path.split("/").pop())}</div>` : "")}
        ${t.output_folder ? `<div class="d mono" style="font-size:11px;">📤 输出:${esc(t.output_folder)} · 自动分 密文/明文</div>` : ""}
        <div class="d" style="white-space:normal;">${esc(t.question.slice(0,80))}${t.question.length>80?'…':''}</div>
      </div>
      <button class="btn-ghost btn-sm" data-run="${esc(t.id)}">立即跑</button>
      <button class="btn-ghost btn-sm" data-toggle="${esc(t.id)}" data-en="${t.enabled?1:0}">${t.enabled?'停用':'启用'}</button>
      <button class="btn-danger" data-del="${esc(t.id)}">删除</button>
    </div>
  `).join("");
  box.querySelectorAll("[data-run]").forEach(b => b.addEventListener("click", async () => {
    try {
      const r = await api("POST", `/api/scheduled_tasks/${b.dataset.run}/run_now`);
      if (r.session_id) {
        // 不论自由问答 / 密态,都已在「⏰」会话开跑 → 切过去看实时运行
        // (密态会算完后显示"结果已加密暂存",在待批里批量解密)
        closeModal();
        await loadSessions();
        await selectSession(r.session_id);
        refreshTasksBadge();
      } else {
        $("tasksAlert").innerHTML = '<div class="alert-box info">已触发 · 见「待批运行」</div>';
        await loadTasksData(); renderPendingList(); renderTaskHistory(); refreshTasksBadge();
      }
    } catch (e) { $("tasksAlert").innerHTML = `<div class="alert-box">触发失败:${esc(e.message)}</div>`; }
  }));
  box.querySelectorAll("[data-toggle]").forEach(b => b.addEventListener("click", async () => {
    await api("PATCH", `/api/scheduled_tasks/${b.dataset.toggle}`, { enabled: b.dataset.en !== "1" });
    await loadTasksData(); renderTaskList();
  }));
  box.querySelectorAll("[data-del]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("删除这个定时任务?")) return;
    await api("DELETE", `/api/scheduled_tasks/${b.dataset.del}`);
    await loadTasksData(); renderTaskList();
  }));
}

const HIST_STATUS_BADGE = {
  launched: "ok", decrypted: "ok", queued: "warn", skipped: "no", failed: "no", missed: "no",
};
const HIST_STATUS_CN = {
  launched: "已运行", decrypted: "已解密", queued: "已入队",
  skipped: "已跳过", failed: "失败", missed: "漏跑",
};
function _histStatusCN(s) { return HIST_STATUS_CN[s] || s; }
function renderTaskHistory() {
  const box = $("tkHistory"); if (!box) return;
  const all = state.tasksHistory || [];
  const f = state.histFilter;

  // 只按日期筛选(空 = 全部)
  let list = all;
  if (f.date) {
    list = all.filter(r => (r.ran_at || "").slice(0, 10) === f.date);
  }

  const cnt = $("histCount");
  if (cnt) cnt.textContent = String(list.length);

  if (!list.length) {
    box.innerHTML = '<div class="alert-box info">没有匹配的运行记录</div>';
    return;
  }
  box.innerHTML = list.slice(0, 60).map(r => `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(r.task_name)} <span class="badge ${HIST_STATUS_BADGE[r.status]||'no'}">${esc(r.status)}</span></div>
        <div class="d">${esc((r.ran_at||"").slice(0,16).replace("T"," "))} · ${esc(r.summary||"")}</div>
      </div>
    </div>
  `).join("");
}

// 顶栏「待批运行」红点 —— 显示**当前定时任务**的未处理数(待解密 + 漏跑)
// 顶栏两个红点 **独立计算**:待解密文件=密态待解密次数;漏跑=漏跑条数(互不相加)
// 轻量刷新「定时任务」总数(只取任务列表)→ 更新切换条数字。任务仅在创建/删除时变,故事件驱动调用。
async function refreshTaskCount() {
  try { const r = await api("GET", "/api/scheduled_tasks"); state.tasks = r.tasks || []; }
  catch (e) { return; }
  const b = document.querySelector('#sessToggle .sess-toggle__btn[data-view="scheduled"]');
  if (b) b.dataset.count = (state.tasks || []).length;
}

async function refreshTasksBadge() {
  const tb = $("tasksBadge"), mb = $("missedBadge");
  const missBtn = document.querySelector('#taskActions [data-tpanel="missed"]');
  const setDot = (el, n) => { if (el) { el.style.display = n ? "" : "none"; el.textContent = n > 99 ? "99+" : n; } };
  const tid = state.currentTaskId;
  if (!tid) { setDot(tb, 0); setDot(mb, 0); if (missBtn) missBtn.style.display = "none"; return; }
  try {
    const p = await api("GET", "/api/scheduled_tasks/pending");
    const enc = (p.encrypted || []).filter(a => a.task_id === tid).reduce((s, a) => s + (a.count || 0), 0);
    const missed = (p.missed || []).filter(m => m.task_id === tid).length;
    setDot(tb, enc);       // 待解密文件:只算密态待解密
    setDot(mb, missed);    // 漏跑:只算漏跑
    if (missBtn) missBtn.style.display = missed ? "" : "none";   // 漏跑按钮随漏跑数实时显隐
  } catch {}
}

// ============ 定时任务面板:可在弹窗(选中会话时顶栏入口)或「总概览」全屏内联复用 ============
const TASK_PANEL_TABS = [
  { key: "status",  label: "运行状态" },
  { key: "pending", label: "待解密文件" },
  { key: "missed",  label: "漏跑" },
  { key: "edit",    label: "编辑任务" },
  { key: "history", label: "运行历史" },
];

let _tpHost = "modal";   // "modal"=弹窗 | "overview"=总概览全屏内联;决定面板渲染到哪个容器
function _tpBody() { return _tpHost === "overview" ? $("ovBody") : $("taskPanelBody"); }
function _tpTabs() { return _tpHost === "overview" ? $("ovTabs") : $("taskPanelTabs"); }
let _gsTimer = null;

function closeTaskPanel() {
  if (_tpHost === "overview") { _tpHost = "modal"; renderOverviewList(); return; }  // 全屏内联 → 返回总概览
  $("taskPanelMask")?.classList.remove("open");
  document.querySelector("#taskPanelMask .task-panel")?.classList.remove("ct-mode");   // 复位创建表单的自适应高度
  const tabs = $("taskPanelTabs"); if (tabs) tabs.style.display = "";
}

// 顶栏入口(选中定时会话时)→ 弹窗形式的任务面板
async function openTaskPanel(view) {
  if (!state.currentTaskId) { toast("该会话未关联任务"); return; }
  _tpHost = "modal";
  const tabs = $("taskPanelTabs"); if (tabs) tabs.style.display = "";   // 复位(新建表单会把它隐藏)
  document.querySelector("#taskPanelMask .task-panel")?.classList.remove("ct-mode");
  $("taskPanelMask").classList.add("open");
  $("taskPanelBody").innerHTML = '<div class="alert-box info">加载中…</div>';
  await loadTasksData();
  renderTaskPanel(view || "status");
}
function openMissedPanel() { return openTaskPanel("missed"); }

function _curTask() { return (state.tasks || []).find(t => t.id === state.currentTaskId); }

// ============ 总概览(全屏,渲染进 #chat;非弹窗)============
async function openOverview() {
  state.ov = "list";
  state.currentSid = null; state.currentSession = null;
  // 记住正处于「定时任务管理」总览,刷新后恢复到这里(而不是跳去某个会话)
  try { localStorage.removeItem("cw_cur_sid"); localStorage.setItem("cw_ov", "1"); } catch (e) {}
  updateSessionChrome();             // 隐藏顶栏任务入口
  renderSessionList();               // 侧栏取消选中高亮
  $("chat").innerHTML = '<div class="ov-wrap"><div class="alert-box info">加载中…</div></div>';
  await loadSessions(); await loadTasksData();
  renderOverviewList();
  clearInterval(_gsTimer);
  _gsTimer = setInterval(async () => {
    if (state.ov !== "list") { clearInterval(_gsTimer); return; }
    if (state.ovComposing) return;   // 输入法组词中,别重渲毁掉搜索框
    await loadSessions(); await loadTasksData();
    if (state.ov === "list" && !state.ovComposing) renderOverviewList();
  }, 4000);
}

function renderOverviewList() {
  state.ov = "list"; _tpHost = "modal";
  $("chat").classList.add("ovmode");
  // 重渲前记住搜索框的焦点/光标位置(4s 自动刷新或输入时都不丢焦点)
  const _se = document.getElementById("ovSearch");
  const _seFocused = _se && document.activeElement === _se;
  const _seCaret = _se ? _se.selectionStart : null;
  const list = state.tasks || [];
  const sessById = {};
  (state.sessions || []).forEach(s => { if (s.task_id) sessById[s.task_id] = s; });
  // 待解密 / 漏跑 计数都按**任务**取(来自 /pending,与会话是否打开/隐藏无关)
  const encByTask = {};   // 每个任务的待解密密文条数
  (state.tasksPending?.encrypted || []).forEach(a => { encByTask[a.task_id] = a.count || 0; });
  const missedByTask = {};
  (state.tasksPending?.missed || []).forEach(m => { missedByTask[m.task_id] = (missedByTask[m.task_id] || 0) + 1; });
  const enabled = list.filter(t => t.enabled).length;
  const runningN = list.filter(t => (sessById[t.id] || {}).running).length;
  const missedN = (state.tasksPending?.missed || []).length;
  // 搜索过滤(任务名 / 问题指令);卡片统计仍用全量
  const q = (state.ovQuery || "").trim().toLowerCase();
  const shown = q ? list.filter(t =>
    (t.name || "").toLowerCase().includes(q) || (t.question || "").toLowerCase().includes(q)) : list;
  let html = `<div class="ov-wrap">
    <div class="ov-head"><h2>定时任务管理</h2>
      <div class="ov-search-wrap">
        <input type="text" id="ovSearch" class="ov-search" placeholder="搜索任务名 / 指令…" value="${esc(state.ovQuery || "")}">
        <button class="ov-search-x" id="ovSearchX" title="清空"${(state.ovQuery || "") ? "" : " hidden"}>✕</button>
      </div>
      <button class="btn-primary btn-sm" id="ovNew">+ 新建定时任务</button></div>
    <div class="ops-cards">
      <div class="ops-card"><div class="ops-card__label">任务总数</div><div class="ops-card__val">${list.length}</div><div class="ops-card__hint">${enabled} 启用 · ${list.length - enabled} 停用</div></div>
      <div class="ops-card"><div class="ops-card__label">正在执行</div><div class="ops-card__val ${runningN ? "ok" : ""}">${runningN}</div><div class="ops-card__hint">实时运行中</div></div>
      <div class="ops-card"><div class="ops-card__label">漏跑待处理</div><div class="ops-card__val ${missedN ? "bad" : ""}">${missedN}</div><div class="ops-card__hint">未按时执行</div></div>
    </div>
    <div id="opsAlertOv"></div>`;
  if (!list.length) html += '<div class="alert-box info">还没有定时任务 · 点右上角「新建定时任务」创建</div>';
  else if (!shown.length) html += `<div class="alert-box info">没有匹配「${esc(state.ovQuery || "")}」的任务</div>`;
  else html += shown.map(t => {
    const s = sessById[t.id] || {};
    const run = s.running ? '<span class="badge use">● 执行中…</span>'
      : (t.enabled ? '<span class="badge ok">空闲·待触发</span>' : '<span class="badge no">已停用</span>');
    const mc = missedByTask[t.id] || 0;
    const miss = mc > 0 ? `<span class="badge danger">漏跑 ${mc}</span>` : "";
    // 把详情里的功能直接铺到行上(单行展示);点击打开对应 tab 的弹窗面板
    // 顺序:查看会话 · 运行状态 · 漏跑 · 待解密文件 · 编辑任务 · 运行历史 · 删除任务
    const tid = esc(t.id);
    const encN = encByTask[t.id] || 0;
    const fns = [`<button class="btn-ghost btn-sm" data-ovsess="${tid}">查看会话</button>`];
    fns.push(`<button class="btn-ghost btn-sm" data-ovfn="status" data-tid="${tid}">运行状态</button>`);
    if (mc > 0) fns.push(`<button class="btn-ghost btn-sm" data-ovfn="missed" data-tid="${tid}" style="color:var(--danger);border-color:#fecaca;">漏跑<span class="ov-cnt danger">${mc}</span></button>`);
    if (t.needs_approval) fns.push(`<button class="btn-ghost btn-sm" data-ovfn="pending" data-tid="${tid}">待解密文件${encN > 0 ? `<span class="ov-cnt">${encN}</span>` : ""}</button>`);
    fns.push(`<button class="btn-ghost btn-sm" data-ovfn="edit" data-tid="${tid}">编辑任务</button>`);
    fns.push(`<button class="btn-ghost btn-sm" data-ovfn="history" data-tid="${tid}">运行历史</button>`);
    fns.push(`<button class="btn-ghost btn-sm ov-del" data-ovdel="${tid}" data-name="${esc(t.name)}">删除任务</button>`);
    return `
    <div class="list-item ov-row">
      <div class="grow">
        <div class="t">${esc(t.name)}
          ${t.needs_approval ? '<span class="badge warn">密态</span>' : '<span class="badge ok">问答</span>'}
          ${t.web_search ? '<span class="badge use">联网</span>' : ''} ${run} ${miss}</div>
        <div class="d">${esc(scheduleText(t))} · 下次:${esc((t.next_run||"").slice(0,16).replace("T"," ")||"—")} · 上次:${esc((t.last_fired||"").slice(0,16).replace("T"," ")||"未跑")}</div>
        <div class="ov-desc">${esc((t.question||"").slice(0,120))}${(t.question||"").length>120?'…':''}</div>
      </div>
      <div class="ov-fns">${fns.join("")}</div>
    </div>`;
  }).join("");
  html += "</div>";
  $("chat").innerHTML = html;
  // 搜索:输入即过滤(局部重渲并恢复焦点/光标);兼容中文输入法 —— 组词期间不重渲
  const se = $("ovSearch");
  if (se) {
    const doSearch = () => { state.ovQuery = se.value; renderOverviewList(); };
    se.addEventListener("compositionstart", () => { state.ovComposing = true; });
    se.addEventListener("compositionend", () => { state.ovComposing = false; doSearch(); });
    se.addEventListener("input", (e) => { if (e.isComposing || state.ovComposing) return; doSearch(); });
    $("ovSearchX")?.addEventListener("click", () => {
      state.ovQuery = ""; state.ovComposing = false; renderOverviewList();
      const n = $("ovSearch"); if (n) n.focus();
    });
    if (_seFocused) { se.focus(); if (_seCaret != null) try { se.setSelectionRange(_seCaret, _seCaret); } catch (e) {} }
  }
  $("ovNew")?.addEventListener("click", () => openTaskWizard({}));
  // 查看会话:确保任务有会话(历次运行累积于此,关闭/切走不清除),再跳转
  $("chat").querySelectorAll("[data-ovsess]").forEach(b => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      const r = await api("POST", `/api/scheduled_tasks/${b.dataset.ovsess}/session`);
      if (r && r.session_id) {
        await loadSessions();          // 新建/已存在的会话先纳入列表,selectSession 才能正确识别为定时会话
        setSessionView("scheduled");
        await selectSession(r.session_id);
      }
    } catch (err) { toast("打开会话失败:" + (err.message || err)); }
  }));
  // 删除任务:二次确认 → 删任务 + 关联会话/记录一并清除
  $("chat").querySelectorAll("[data-ovdel]").forEach(b => b.addEventListener("click", async (e) => {
    e.stopPropagation();
    const id = b.dataset.ovdel, nm = b.dataset.name || "该任务";
    if (!confirm(`确认删除定时任务「${nm}」?\n\n该任务、它的聊天会话与全部运行历史都会被清除,且不可恢复。`)) return;
    b.disabled = true; b.textContent = "删除中…";
    try {
      await api("DELETE", `/api/scheduled_tasks/${id}`);
      if (state.currentTaskId === id) state.currentTaskId = "";
      toast("任务及其会话已删除");
      await loadTasksData(); await loadSessions(); refreshTasksBadge();   // 先刷新任务 → 切换条数字立即更新
      renderOverviewList();
    } catch (err) { toast("删除失败:" + (err.message || err)); b.disabled = false; b.textContent = "删除任务"; }
  }));
  $("chat").querySelectorAll("[data-ovfn]").forEach(b => b.addEventListener("click", (e) => {
    e.stopPropagation();
    state.currentTaskId = b.dataset.tid;
    openTaskPanel(b.dataset.ovfn);   // 弹窗面板(之前的界面),定位到对应 tab
  }));
}

function renderTaskPanel(view) {
  const t = _curTask();
  // 无数据(问答/查询)任务没有"待解密文件",tab 里也不显示
  const isData = !!(t && t.needs_approval);
  const tabsList = TASK_PANEL_TABS.filter(tab => tab.key !== "pending" || isData);
  if (view === "pending" && !isData) view = "status";   // 兜底:无数据时不进待解密
  const tabs = _tpTabs();
  tabs.innerHTML = tabsList.map(tb =>
    `<button class="tp-tab ${tb.key === view ? "active" : ""}" data-tpv="${tb.key}">${tb.label}</button>`).join("");
  tabs.querySelectorAll("[data-tpv]").forEach(b =>
    b.addEventListener("click", () => renderTaskPanel(b.dataset.tpv)));
  if (!t) { _tpBody().innerHTML = '<div class="alert-box">任务不存在(可能已删除)</div>'; return; }
  ({ status: renderTPStatus, pending: renderTPPending, missed: renderTPMissed,
     history: renderTPHistory, edit: renderTPEdit }[view] || renderTPStatus)(t);
}

async function _reloadPanel(view) { await loadTasksData(); renderTaskPanel(view); refreshTasksBadge(); }


// —— 运行状态(= 我的任务那块)——
function renderTPStatus(t) {
  _tpBody().innerHTML = `
    <h2 class="tp-h">运行状态</h2>
    <div id="tpAlert"></div>
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(t.name)}
          ${t.needs_approval ? '<span class="badge warn">密态·需批准</span>' : '<span class="badge ok">自由问答</span>'}
          ${t.web_search ? '<span class="badge use">联网搜索·开</span>' : '<span class="badge no">联网搜索·关</span>'}
          ${t.enabled ? '<span class="badge ok">运行中</span>' : '<span class="badge no">已停用</span>'}</div>
        <div class="d">${esc(scheduleText(t))} · 下次:${esc((t.next_run||"").slice(0,16).replace("T"," ")||"—")}</div>
        <div class="d">上次运行:${esc((t.last_fired||"").slice(0,16).replace("T"," ")||"尚未运行")}</div>
        ${t.source_folder ? `<div class="d mono" style="font-size:11px;">📁 数据文件夹:${esc(t.source_folder)}</div>` : ""}
        ${t.output_folder ? `<div class="d mono" style="font-size:11px;">📤 输出:${esc(t.output_folder)} · 自动分 密文/明文</div>` : ""}
        <div class="d" style="white-space:normal;">${esc(t.question)}</div>
      </div>
    </div>
    <div style="display:flex; gap:10px; margin-top:16px; flex-wrap:wrap;">
      <button class="btn-primary" id="tpRun">立即运行一次</button>
      <button class="btn-ghost" id="tpToggle">${t.enabled ? "停用" : "启用"}</button>
      <button class="btn-danger" id="tpDel">删除任务</button>
    </div>`;
  const inOv = !!state.ov;   // 从「定时任务管理」打开的弹窗:操作后留在弹窗并刷新,关闭后回到管理列表
  $("tpRun").addEventListener("click", async () => {
    const b = $("tpRun"); b.disabled = true; b.textContent = "触发中…";
    try {
      await api("POST", `/api/scheduled_tasks/${t.id}/run_now`);
      await loadSessions();   // 刷新侧栏/总概览运行态(跳动的 …)
      toast("已触发运行一次");
      if (inOv) { await _reloadPanel("status"); }       // 弹窗:留在面板并刷新
      else { closeTaskPanel(); if (state.currentSid) await selectSession(state.currentSid); }
    } catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">触发失败:${esc(e.message)}</div>`; b.disabled = false; b.textContent = "立即运行一次"; }
  });
  $("tpToggle").addEventListener("click", async () => {
    try { await api("PATCH", `/api/scheduled_tasks/${t.id}`, { enabled: !t.enabled }); await _reloadPanel("status"); }
    catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">操作失败:${esc(e.message)}</div>`; }
  });
  $("tpDel").addEventListener("click", async () => {
    if (!confirm(`确认删除定时任务「${t.name}」?\n\n该任务、它的聊天会话与全部运行历史都会被清除,且不可恢复。`)) return;
    try {
      await api("DELETE", `/api/scheduled_tasks/${t.id}`);
      await refreshTaskCount(); await loadSessions(); refreshTasksBadge(); toast("任务已删除");
      closeTaskPanel();
      if (inOv) { await loadTasksData(); renderOverviewList(); }   // 回定时任务管理列表
      else { showWelcome(); }
    } catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">删除失败:${esc(e.message)}</div>`; }
  });
}

// —— 待解密文件(只放密态待解密结果;漏跑已独立成「漏跑」面板)——
function renderTPPending(t) {
  const enc = (state.tasksPending?.encrypted || []).filter(a => a.task_id === t.id);
  let html = '<h2 class="tp-h">待解密文件</h2><div id="tpAlert"></div>';
  if (!enc.length) html += '<div class="alert-box info">没有待解密的文件</div>';
  enc.forEach(a => {
    html += `<div class="list-item"><div class="grow">
        <div class="t">密态结果 <span class="badge warn">${a.count} 次待解密</span></div>
        <div class="d" style="font-size:11px;">最近:${esc((a.latest_run||"").slice(0,16).replace("T"," "))}</div>
      </div>
      <button class="btn-primary btn-sm" data-tpdec="${esc(a.task_id)}">解密 → 输出文件夹</button></div>`;
  });
  const body = _tpBody(); body.innerHTML = html;
  body.querySelectorAll("[data-tpdec]").forEach(b => b.addEventListener("click", async () => {
    b.disabled = true; b.textContent = "解密中…";
    try {
      const r = await api("POST", `/api/scheduled_tasks/decrypt/${b.dataset.tpdec}`);
      $("tpAlert").innerHTML = `<div class="alert-box success">✓ 已解密 ${r.count} 次 → ${esc(r.folder)}</div>`;
      await _reloadPanel("pending");
    } catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">解密失败:${esc(e.message)}</div>`; b.disabled = false; b.textContent = "解密 → 输出文件夹"; }
  }));
}

// —— 漏跑(作为任务面板的一个 tab)——
function renderTPMissed() {
  const t = _curTask();
  const tid = state.currentTaskId;
  const missed = (state.tasksPending?.missed || []).filter(m => m.task_id === tid);
  const isData = !!(t && t.needs_approval);
  let html = '<h2 class="tp-h">漏跑</h2><div id="tpAlert"></div>';
  html += `<div class="alert-box info" style="line-height:1.7;">
    「漏跑」指<strong>到了设定时间但没按时执行</strong>的运行 —— 通常是那一刻<strong>客户端没开 / 崩溃 / 电脑休眠</strong>,系统不会自作主张补跑,而是列在这里等你处理。<br>
    ${isData
      ? "· <strong>手动选择文件</strong>:弹出文件选择,挑这轮要处理的数据文件,再按密态流程算(算完进「待解密文件」);"
      : "· <strong>重新执行</strong>:立刻补跑这一轮(无需选文件);若问题含相对日期(如「今日」),会自动改成<strong>漏跑当天</strong>的日期再跑,避免跑成今天的;"}<br>
    · <strong>忽略</strong>:这轮不补了,清掉这条预警。
  </div>`;
  if (!missed.length) html += '<div class="alert-box info">当前没有漏跑 👍</div>';
  missed.forEach(m => {
    const btnLabel = m.needs_data ? "手动选择文件" : "重新执行";
    html += `<div class="list-item missed"><div class="grow">
        <div class="t">⚠ 漏跑 <span class="badge danger">未执行</span></div>
        <div class="d" style="font-size:11px;">本应执行:${esc((m.due_at||"").slice(0,16).replace("T"," "))} · ${esc(m.reason||"")}</div>
      </div>
      <button class="btn-primary btn-sm" data-tpremed="${esc(m.id)}" data-needs="${m.needs_data?1:0}">${btnLabel}</button>
      <button class="btn-ghost btn-sm" data-tpmissx="${esc(m.id)}">忽略</button></div>`;
  });
  const body = _tpBody(); body.innerHTML = html;
  const inOv = !!state.ov;   // 「定时任务管理」弹窗:补跑后留在面板刷新
  body.querySelectorAll("[data-tpremed]").forEach(b => b.addEventListener("click", async () => {
    let sp = "";
    if (b.dataset.needs === "1") {
      try { const p = await api("POST", "/api/pick_file"); if (p.cancelled || !p.path) return; sp = p.path; }
      catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">选文件失败:${esc(e.message)}</div>`; return; }
    }
    const orig = b.textContent;
    b.disabled = true; b.textContent = "补跑中…";
    try {
      await api("POST", `/api/scheduled_tasks/missed/${b.dataset.tpremed}/remediate`, { source_path: sp });
      await loadSessions(); refreshTasksBadge();   // 刷新侧栏/总概览:跳动…、红警示
      if (inOv) { await _reloadPanel("missed"); toast("已补跑该轮"); }
      else { closeTaskPanel(); if (state.currentSid) await selectSession(state.currentSid); toast("已补跑该轮 · 见会话"); }
    } catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">补跑失败:${esc(e.message)}</div>`; b.disabled = false; b.textContent = orig; }
  }));
  body.querySelectorAll("[data-tpmissx]").forEach(b => b.addEventListener("click", async () => {
    await api("POST", `/api/scheduled_tasks/missed/${b.dataset.tpmissx}/dismiss`);
    await loadTasksData(); await loadSessions(); refreshTasksBadge();
    const left = (state.tasksPending?.missed || []).filter(m => m.task_id === state.currentTaskId).length;
    renderTaskPanel(left ? "missed" : "status");   // 还有漏跑就留在漏跑,否则切到运行状态
  }));
}

// —— 运行历史 ——
const _tpHistFilter = { date: "", status: "" };
function renderTPHistory(t) {
  const all = (state.tasksHistory || []).filter(r => r.task_id === t.id);
  const statuses = [...new Set(all.map(r => r.status))];
  let list = all;
  if (_tpHistFilter.date) list = list.filter(r => (r.ran_at || "").slice(0, 10) === _tpHistFilter.date);
  if (_tpHistFilter.status) list = list.filter(r => r.status === _tpHistFilter.status);

  const opts = `<option value="">全部类型</option>` +
    statuses.map(s => `<option value="${esc(s)}" ${_tpHistFilter.status === s ? "selected" : ""}>${esc(_histStatusCN(s))}</option>`).join("");
  let html = `
    <h2 class="tp-h">运行历史</h2>
    <div class="tp-hist-filter">
      <label>按日期</label><input type="date" id="tpHistDate" value="${esc(_tpHistFilter.date)}">
      <label>按类型</label><select id="tpHistStatus">${opts}</select>
      <button class="btn-ghost btn-sm" id="tpHistClear">清除</button>
      <span class="tp-hist-count">${list.length} 条</span>
    </div>`;
  if (!list.length) html += '<div class="alert-box info">没有匹配的运行记录</div>';
  else html += list.slice(0, 100).map(r => `
    <div class="list-item"><div class="grow">
      <div class="t"><span class="badge ${HIST_STATUS_BADGE[r.status]||"no"}">${esc(_histStatusCN(r.status))}</span></div>
      <div class="d">${esc((r.ran_at||"").slice(0,16).replace("T"," "))} · ${esc(r.summary||"")}</div>
    </div></div>`).join("");
  _tpBody().innerHTML = html;
  $("tpHistDate").addEventListener("change", e => { _tpHistFilter.date = e.target.value; renderTPHistory(t); });
  $("tpHistStatus").addEventListener("change", e => { _tpHistFilter.status = e.target.value; renderTPHistory(t); });
  $("tpHistClear").addEventListener("click", () => { _tpHistFilter.date = ""; _tpHistFilter.status = ""; renderTPHistory(t); });
}

// —— 编辑定时任务(按"有数据绑定 / 无数据"区分,只显示对应字段)——
function renderTPEdit(t) {
  const isData = !!(t.source_folder || t.cipher_path);   // 有数据绑定 = 密态分析任务
  const common = `
    <div class="field"><label>任务名</label><input type="text" id="teName" value="${esc(t.name)}"></div>
    <div class="field"><label>问题 / 指令</label><textarea id="teQuestion" rows="2">${esc(t.question)}</textarea></div>
    <div class="field"><label>周期<span class="hint-inline">如 每天早上9点 / 每周一三五9点 / 每月1号</span></label>
      <input type="text" id="teCronNL" value="${esc(t.cron_readable || scheduleText(t))}">
      <div id="teCronResult" class="cron-result">识别结果会显示在这里</div>
      <input type="hidden" id="teCron" value="${esc(t.cron_expr || "")}">
    </div>`;
  const dataFields = `
    <div class="field"><label>数据文件夹 <span class="hint-inline">每次取最新文件加密分析</span></label>
      <div class="folder-pick">
        <input type="text" id="teFolder" value="${esc(t.source_folder || "")}" placeholder="点右侧选择,或粘贴绝对路径">
        <button type="button" class="wiz-pick" id="teFolderBtn">${FOLDER_ICON_SVG}选择文件夹</button>
      </div>
    </div>
    <div class="field"><label>输出文件夹 <span class="hint-inline">留空=默认 下载/任务名</span></label>
      <div class="folder-pick">
        <input type="text" id="teOutput" value="${esc(t.output_folder || "")}" placeholder="点右侧选择,或粘贴绝对路径">
        <button type="button" class="wiz-pick" id="teOutputBtn">${FOLDER_ICON_SVG}选择文件夹</button>
      </div>
    </div>`;
  const webField = `
    <div class="field">
      <label class="cw-switch">
        <input type="checkbox" id="teWeb" ${t.web_search ? "checked" : ""}>
        <span class="cw-switch__body">
          <span class="cw-switch__head">联网搜索<span class="cw-switch__state"></span></span>
          <span class="hint-inline">${isData
            ? "数据分析时,若没提供公式/口径,让 AI 上网查找标准计算方法"
            : "查天气 / 新闻 / 资料等实时信息"}</span>
        </span>
        <span class="cw-switch__track"><span class="cw-switch__thumb"></span></span>
      </label>
    </div>`;
  const typeTag = isData
    ? '<span class="badge warn">密态分析 · 有数据绑定</span>'
    : '<span class="badge ok">问答任务 · 无数据绑定</span>';
  _tpBody().innerHTML = `
    <h2 class="tp-h">编辑定时任务 ${typeTag}</h2>
    <div id="tpAlert"></div>
    ${common}
    ${isData ? dataFields : ""}
    ${webField}
    <button class="btn-primary" id="teSave">保存修改</button>`;
  // 大白话 → cron 实时解析
  let timer = null;
  const parse = async () => {
    const text = $("teCronNL").value.trim(); const box = $("teCronResult");
    if (!text) { box.className = "cron-result"; box.textContent = "识别结果会显示在这里"; return; }
    try {
      const r = await api("POST", "/api/scheduled_tasks/parse_schedule", { text });
      if (r.ok) { box.className = "cron-result ok"; box.innerHTML = `✓ <strong>${esc(r.readable)}</strong> <span class="cron-code">${esc(r.cron)}</span>`; $("teCron").value = r.cron; }
      else { box.className = "cron-result bad"; box.textContent = r.error || "没识别出来"; $("teCron").value = ""; }
    } catch (e) { box.className = "cron-result bad"; box.textContent = "解析失败"; }
  };
  $("teCronNL").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(parse, 350); });
  parse();
  if (isData) {
    const pick = (btn, inp) => $(btn).addEventListener("click", async () => {
      try { const r = await api("POST", "/api/pick_folder"); if (!r.cancelled && r.path) $(inp).value = r.path; }
      catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">选择失败:${esc(e.message)}</div>`; }
    });
    pick("teFolderBtn", "teFolder"); pick("teOutputBtn", "teOutput");
  }
  $("teSave").addEventListener("click", async () => {
    const name = $("teName").value.trim(), question = $("teQuestion").value.trim();
    if (!name || !question) { $("tpAlert").innerHTML = '<div class="alert-box">任务名和问题都要填</div>'; return; }
    const cron = $("teCron").value.trim();
    if (!cron) { $("tpAlert").innerHTML = '<div class="alert-box">排程没识别成功,换个写法</div>'; return; }
    const b = $("teSave"); b.disabled = true; b.textContent = "保存中…";
    // 只发该类型相关字段(精简)
    const patch = { name, question, schedule_kind: "cron", cron_expr: cron,
      cron_readable: ($("teCronResult").querySelector("strong")?.textContent || ""),
      web_search: !!($("teWeb") && $("teWeb").checked) };
    if (isData) { patch.source_folder = $("teFolder").value.trim(); patch.output_folder = $("teOutput").value.trim(); }
    try {
      await api("PATCH", `/api/scheduled_tasks/${t.id}`, patch);
      // 不重渲整个面板(那会让表单跳一下);只提示成功 + 复位按钮 + 静默刷新侧栏标题
      t.name = name; t.question = question; t.cron_expr = cron;
      $("tpAlert").innerHTML = '<div class="alert-box success">已保存</div>';
      b.disabled = false; b.textContent = "保存修改";
      loadSessions();   // 后台刷新侧栏(改名后子任务名跟着变),不阻塞、不动当前面板
    } catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">保存失败:${esc(e.message)}</div>`; b.disabled = false; b.textContent = "保存修改"; }
  });
}

// ============ 新建定时任务:单页表单(弹窗,覆盖输入框)============
// 取代原先底部一步步的向导 —— 所有内容一次性铺在一个表单里(类似「编辑任务」)。
function openCreateTaskForm(prefill, fromMid) {
  _tpHost = "modal";
  state._createFrom = fromMid || "";
  const tabs = $("taskPanelTabs");
  if (tabs) { tabs.innerHTML = ""; tabs.style.display = "none"; }   // 创建表单不需要 tab
  document.querySelector("#taskPanelMask .task-panel")?.classList.add("ct-mode");  // 弹窗自适应高度,一屏展示
  $("taskPanelMask").classList.add("open");
  renderCreateTaskForm(prefill || {});
}

function renderCreateTaskForm(s) {
  s = s || {};
  const dsNone = (s.needs_data === false);
  const body = $("taskPanelBody");
  body.innerHTML = `
    <div class="ct-form">
    <h2 class="tp-h">新建定时任务</h2>
    <div id="tpAlert"></div>
    <div class="field"><label>任务名 <span class="hint-inline">留空=自动取问题前几个字</span></label>
      <input type="text" id="ctName" value="${esc(s.name || "")}" placeholder="给任务起个名(可留空)"></div>
    <div class="field"><label>问题 / 指令</label>
      <textarea id="ctQuestion" rows="2" placeholder="例:按大区统计本月回款率 TOP10 / 查上海明天天气 / 汇总今天的行业新闻">${esc(s.question || "")}</textarea></div>
    <div class="field"><label>周期<span class="hint-inline">如 每天早上9点 / 每周一三五9点 / 每月1号</span></label>
      <input type="text" id="ctCronNL" value="${esc(s.schedule_text || s.cron_readable || "")}" placeholder="用大白话写时间,自动转成排程">
      <div id="ctCronResult" class="cron-result">识别结果会显示在这里</div>
      <input type="hidden" id="ctCron" value="${esc(s.cron || "")}">
    </div>
    <div class="field"><label>数据来源</label>
      <label class="wiz-radio"><input type="radio" name="ctDS" value="folder" ${dsNone ? "" : "checked"}>
        <span>绑定数据文件夹(每次取最新文件自动加密分析)<b class="wiz-rec">密态分析</b></span></label>
      <label class="wiz-radio"><input type="radio" name="ctDS" value="none" ${dsNone ? "checked" : ""}>
        <span>不需要数据(定期问答 / 查天气、新闻、资料等)</span></label>
    </div>
    <div id="ctDataFields">
      <div class="field"><label>数据文件夹 <span class="hint-inline">每次取最新文件加密分析</span></label>
        <div class="folder-pick">
          <input type="text" id="ctFolder" value="${esc(s.source_folder || "")}" placeholder="点右侧选择,或粘贴绝对路径">
          <button type="button" class="wiz-pick" id="ctFolderBtn">${FOLDER_ICON_SVG}选择文件夹</button>
        </div></div>
      <div class="field"><label>输出文件夹 <span class="hint-inline">留空=默认 下载/任务名</span></label>
        <div class="folder-pick">
          <input type="text" id="ctOutput" value="${esc(s.output_folder || "")}" placeholder="点右侧选择,或粘贴绝对路径">
          <button type="button" class="wiz-pick" id="ctOutputBtn">${FOLDER_ICON_SVG}选择文件夹</button>
        </div></div>
    </div>
    <div class="field">
      <label class="cw-switch">
        <input type="checkbox" id="ctWeb" ${s.web_search ? "checked" : ""}>
        <span class="cw-switch__body">
          <span class="cw-switch__head">联网搜索<span class="cw-switch__state"></span></span>
          <span class="hint-inline">无数据查实时天气/新闻/资料;有数据时让 AI 上网找公式/口径</span>
        </span>
        <span class="cw-switch__track"><span class="cw-switch__thumb"></span></span>
      </label>
    </div>
    <button class="btn-primary" id="ctCreate">创建任务</button>
    </div>`;
  // 数据来源切换:显隐 数据/输出 字段
  const syncDS = () => {
    const none = body.querySelector('input[name="ctDS"]:checked')?.value === "none";
    $("ctDataFields").style.display = none ? "none" : "";
  };
  body.querySelectorAll('input[name="ctDS"]').forEach(r => r.addEventListener("change", syncDS));
  syncDS();
  // 大白话 → cron 实时解析
  let timer = null;
  const parse = async () => {
    const text = $("ctCronNL").value.trim(); const box = $("ctCronResult");
    if (!text) { box.className = "cron-result"; box.textContent = "识别结果会显示在这里"; $("ctCron").value = ""; return; }
    try {
      const r = await api("POST", "/api/scheduled_tasks/parse_schedule", { text });
      if (r.ok) { box.className = "cron-result ok"; box.innerHTML = `✓ <strong>${esc(r.readable)}</strong> <span class="cron-code">${esc(r.cron)}</span>`; $("ctCron").value = r.cron; }
      else { box.className = "cron-result bad"; box.textContent = r.error || "没识别出来"; $("ctCron").value = ""; }
    } catch (e) { box.className = "cron-result bad"; box.textContent = "解析失败"; }
  };
  $("ctCronNL").addEventListener("input", () => { clearTimeout(timer); timer = setTimeout(parse, 350); });
  if ($("ctCronNL").value.trim()) parse();
  // 文件夹选择
  const pick = (btn, inp) => $(btn).addEventListener("click", async () => {
    try { const r = await api("POST", "/api/pick_folder"); if (!r.cancelled && r.path) $(inp).value = r.path; }
    catch (e) { $("tpAlert").innerHTML = `<div class="alert-box">选择失败:${esc(e.message)}</div>`; }
  });
  pick("ctFolderBtn", "ctFolder"); pick("ctOutputBtn", "ctOutput");
  $("ctCreate").addEventListener("click", submitCreateTask);
}

async function submitCreateTask() {
  const body = $("taskPanelBody");
  const name = $("ctName").value.trim();
  const question = $("ctQuestion").value.trim();
  const cron = $("ctCron").value.trim();
  const none = body.querySelector('input[name="ctDS"]:checked')?.value === "none";
  const source_folder = none ? "" : $("ctFolder").value.trim();
  const output_folder = none ? "" : $("ctOutput").value.trim();
  if (!question) { $("tpAlert").innerHTML = '<div class="alert-box">请填写问题 / 指令</div>'; return; }
  if (!cron) { $("tpAlert").innerHTML = '<div class="alert-box">排程没识别成功,换个写法,如「每天早上9点」</div>'; return; }
  if (!none && !source_folder) { $("tpAlert").innerHTML = '<div class="alert-box">请选择数据文件夹(或改选「不需要数据」)</div>'; return; }
  const b = $("ctCreate"); b.disabled = true; b.textContent = "创建中…";
  let newTask = null;
  try {
    newTask = await api("POST", "/api/scheduled_tasks", {
      name: name || question.slice(0, 8),
      question,
      schedule_kind: "cron", cron_expr: cron,
      cron_readable: ($("ctCronResult").querySelector("strong")?.textContent || ""),
      source_folder, output_folder,
      web_search: !!($("ctWeb") && $("ctWeb").checked),
      enabled: true,
    });
  } catch (e) {
    $("tpAlert").innerHTML = `<div class="alert-box">创建失败:${esc(e.message)}</div>`;
    b.disabled = false; b.textContent = "创建任务"; return;
  }
  const fromMid = state._createFrom; state._createFrom = "";
  const fromOverview = (state.ov === "list");
  closeTaskPanel();
  const tabs = $("taskPanelTabs"); if (tabs) tabs.style.display = "";   // 复位 tab 显隐
  // 来自普通会话的触发卡 → 标记「已创建」
  if (fromMid && state.currentSid) {
    try { await api("POST", `/api/sessions/${state.currentSid}/messages/${fromMid}/wizard_done`); } catch (_) {}
    const msg = state.currentSession?.messages?.find(x => x.id === fromMid);
    if (msg) {
      msg.wizard = Object.assign({}, msg.wizard, { created: true });
      const node = document.querySelector(`.msg[data-mid="${fromMid}"]`);
      if (node) node.replaceWith(renderMessage(msg));
    }
  }
  try {
    await refreshTaskCount();   // 切换条「定时任务」数字 +1
    await loadSessions();
    if (fromOverview) { await loadTasksData(); renderOverviewList(); }   // 在管理页创建 → 刷新列表
    else { setSessionView("scheduled"); if (newTask && newTask.session_id) await selectSession(newTask.session_id); }
  } catch (_) {}
  toast("定时任务已创建 ✓");
}

// ============ Skill 管理 Tab ============
async function renderSkillsTab() {
  $("modalBody").innerHTML = `
    <h2>${TABS.skills.title}</h2>
    <p class="sub">SKILL.md 技能教 AI 写密态计算代码 · 拖入技能包(SKILL.md + INDEX / docs / examples)即可添加</p>
    <div id="skillsAlert"></div>

    <div id="skillMdList"><div class="alert-box info">加载中…</div></div>

    <h3 style="font-size:14px; margin: 22px 0 8px;">添加技能</h3>
    <div class="sk-drop" id="skillDropZone" tabindex="0">
      <div class="sk-drop__t">拖入技能<strong>文件夹</strong> / <strong>.zip</strong> / <strong>SKILL.md</strong></div>
      <div class="sk-drop__s" id="skillDropHint">支持多文件嵌套包(SKILL.md + INDEX.md + docs/ + examples/)· 点击选文件夹</div>
      <input type="file" id="skillDirInput" webkitdirectory directory multiple hidden>
      <input type="file" id="skillFileInput" accept=".md,.zip" hidden>
    </div>
    <div id="skillUpStatus" style="margin-top:12px;"></div>
  `;

  await loadSkills();
  renderSkillMdList();
  bindSkillDrop();
}

async function loadSkills() {
  try { state.skills = await api("GET", "/api/skills"); }
  catch { state.skills = { skill_md: [], builtin: [], custom: [] }; }
}

// ── 拖拽 / 选择技能包上传 ──
function bindSkillDrop() {
  const zone = $("skillDropZone");
  const dirInp = $("skillDirInput");
  const fileInp = $("skillFileInput");
  if (!zone) return;

  // 点击:优先弹文件夹选择(webkitdirectory)
  zone.addEventListener("click", e => {
    if (e.target.closest("button, a")) return;
    dirInp.click();
  });
  zone.addEventListener("keydown", e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); dirInp.click(); }
  });
  dirInp.addEventListener("change", e => {
    const fs = Array.from(e.target.files || []);
    if (fs.length) uploadSkillFiles(fs.map(f => ({ file: f, path: f.webkitRelativePath || f.name })));
    e.target.value = "";
  });
  fileInp.addEventListener("change", e => {
    const f = e.target.files?.[0];
    if (f) uploadSkillFiles([{ file: f, path: f.name }]);
    e.target.value = "";
  });

  ["dragenter", "dragover"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.add("dragover"); })
  );
  ["dragleave"].forEach(ev =>
    zone.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); zone.classList.remove("dragover"); })
  );
  zone.addEventListener("drop", async e => {
    e.preventDefault(); e.stopPropagation(); zone.classList.remove("dragover");
    const items = e.dataTransfer.items;
    let collected = [];
    if (items && items.length && items[0].webkitGetAsEntry) {
      // 递归读目录树(支持文件夹嵌套)
      for (const it of items) {
        const entry = it.webkitGetAsEntry();
        if (entry) collected = collected.concat(await walkEntry(entry, ""));
      }
    }
    if (!collected.length && e.dataTransfer.files?.length) {
      collected = Array.from(e.dataTransfer.files).map(f => ({ file: f, path: f.name }));
    }
    if (collected.length) uploadSkillFiles(collected);
  });
}

// 递归读 FileSystemEntry → [{file, path}]
function walkEntry(entry, prefix) {
  return new Promise(resolve => {
    if (entry.isFile) {
      entry.file(f => resolve([{ file: f, path: prefix + entry.name }]), () => resolve([]));
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      const all = [];
      const readBatch = () => reader.readEntries(async entries => {
        if (!entries.length) {
          const nested = await Promise.all(all.map(en => walkEntry(en, prefix + entry.name + "/")));
          resolve(nested.flat());
        } else {
          all.push(...entries);
          readBatch();
        }
      }, () => resolve([]));
      readBatch();
    } else resolve([]);
  });
}

async function uploadSkillFiles(items) {
  // items: [{file, path}]
  const hasSkillMd = items.some(it => /(^|\/)SKILL\.md$/i.test(it.path) || /\.zip$/i.test(it.path));
  if (!hasSkillMd) {
    $("skillUpStatus").innerHTML = '<div class="alert-box">技能包里必须有 SKILL.md(或拖一个 .zip)</div>';
    return;
  }
  $("skillUpStatus").innerHTML = `<div class="alert-box info">上传中 · ${items.length} 个文件…</div>`;
  const fd = new FormData();
  items.forEach(it => {
    fd.append("files", it.file, it.file.name);
    fd.append("paths", it.path);
  });
  try {
    const res = await api("POST", "/api/skills/upload", fd, true);
    $("skillUpStatus").innerHTML = `<div class="alert-box success">✓ 已添加技能「${esc(res.name)}」· 下次提问 AI 可用</div>`;
    await loadSkills();
    renderSkillMdList();
  } catch (e) {
    $("skillUpStatus").innerHTML = `<div class="alert-box">添加失败:${esc(e.message)}</div>`;
  }
}

function renderSkillMdList() {
  const box = $("skillMdList");
  if (!box) return;
  const list = state.skills?.skill_md || [];
  if (!list.length) { box.innerHTML = '<div class="alert-box info">还没有技能 · 拖入技能包添加</div>'; return; }
  box.innerHTML = list.map(s => `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(s.name)}
          ${s.has_index ? '<span class="mono-tag">INDEX</span>' : ''}
          ${s.example_count ? `<span class="mono-tag">${s.example_count} 示例</span>` : ''}</div>
        <div class="d" style="white-space:normal;">${esc((s.description || '').slice(0, 160))}${(s.description||'').length>160?'…':''}</div>
      </div>
      <button class="btn-ghost btn-sm" data-view-md="${esc(s.slug)}">查看</button>
      ${s.is_user
        ? `<button class="btn-danger" data-del-md="${esc(s.slug)}">删除</button>`
        : `<span class="badge ok">内置</span>`}
    </div>
  `).join("");
  box.querySelectorAll("[data-view-md]").forEach(b => b.addEventListener("click", () => {
    showSkillMd(b.dataset.viewMd);
  }));
  box.querySelectorAll("[data-del-md]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("删除这个技能?")) return;
    try {
      await api("DELETE", `/api/skills/md/${encodeURIComponent(b.dataset.delMd)}`);
      await loadSkills();
      renderSkillMdList();
    } catch (e) {
      $("skillsAlert").innerHTML = `<div class="alert-box">删除失败:${esc(e.message)}</div>`;
    }
  }));
}

async function showSkillMd(slug) {
  const wrap = document.createElement("div");
  wrap.className = "modal-mask open"; wrap.style.zIndex = 40;
  wrap.innerHTML = `
    <div class="modal modal--single" style="position:relative; max-width:820px; height:auto; max-height:84vh;">
      <button class="modal__close" id="smClose">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
      </button>
      <div class="modal__body" style="padding:24px 30px;">
        <h2>${esc(slug)}</h2>
        <pre id="smBody" style="white-space:pre-wrap; font-size:12px; line-height:1.6; font-family:ui-monospace,Menlo,monospace; color:var(--text);">加载中…</pre>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  wrap.querySelector("#smClose").addEventListener("click", () => wrap.remove());
  wrap.addEventListener("click", e => { if (e.target === wrap) wrap.remove(); });
  try {
    const r = await api("GET", `/api/skills/md/${encodeURIComponent(slug)}`);
    wrap.querySelector("#smBody").textContent = r.body || "(空)";
  } catch (e) {
    wrap.querySelector("#smBody").textContent = "加载失败:" + e.message;
  }
}

function renderBuiltinSkills() {
  const box = $("builtinSkills");
  if (!box) return;
  const list = state.skills?.builtin || [];
  box.innerHTML = list.map(s => `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(s.name)} <span class="mono-tag">${esc(s.tool)}</span></div>
        <div class="d">${esc(s.desc)}</div>
        ${s.params && s.params.length
          ? `<div class="d mono" style="font-size:11px; white-space:normal; color:var(--text-muted);">params: ${s.params.map(esc).join(" · ")}</div>`
          : ""}
      </div>
      <span class="badge ok">内置</span>
    </div>
  `).join("");
}

function renderCustomSkills() {
  const box = $("customSkills");
  if (!box) return;
  const list = state.skills?.custom || [];
  if (!list.length) {
    box.innerHTML = '<div class="alert-box info">还没有自定义指标 · 在下方添加(如「边际贡献率」「人效比」等本企业口径)</div>';
    return;
  }
  box.innerHTML = list.map(s => `
    <div class="list-item">
      <div class="grow">
        <div class="t">${esc(s.name)}</div>
        ${s.description ? `<div class="d">${esc(s.description)}</div>` : ""}
        ${s.formula ? `<div class="d mono" style="white-space:normal;">公式:${esc(s.formula)}</div>` : ""}
      </div>
      <button class="btn-danger" data-del-skill="${esc(s.id)}">删除</button>
    </div>
  `).join("");
  box.querySelectorAll("[data-del-skill]").forEach(b => b.addEventListener("click", async () => {
    if (!confirm("删除这个自定义指标?")) return;
    try {
      await api("DELETE", `/api/skills/${encodeURIComponent(b.dataset.delSkill)}`);
      await loadSkills();
      renderCustomSkills();
    } catch (e) {
      $("skillsAlert").innerHTML = `<div class="alert-box">删除失败:${esc(e.message)}</div>`;
    }
  }));
}

// ============ 密文文件管理 Modal(顶栏入口) ============
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
      const dh = res.data_health || {};
      const dhLine = (dh.message && dh.message !== "数据干净,无需清洗。")
        ? `<br><span class="af-s">数据体检:${esc(dh.message)}</span>` : "";
      const ff = res.formula_filled || [];
      const ffLine = ff.length
        ? `<br><span class="af-s">已按源表公式补算派生列:${esc(ff.join("、"))}</span>` : "";
      $("fileUpStatus").innerHTML =
        `<div class="alert-box success">✓ 已加密入库:<strong>${esc(res.name)}</strong>
         <br>${enc.length} 列加密 · ${pt.length} 列身份标识 · ${res.row_count || "?"} 行${dhLine}${ffLine}</div>`;
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
