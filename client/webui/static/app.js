/* =========================================================
 * Clawworker Client v4 · skill-only 架构
 * - 会话侧栏(list / new / delete / switch)
 * - 主聊天区(消息 + 单行进度 + 轮询)
 * - 附件:本条消息附带一份密文(拖拽 / 点选)
 * - 设置 modal(连接 / 密文文件 / 同态密钥 / 账户)
 * ========================================================= */

const { $, esc, mdToHtml, api, ui } = window.ClawCore;

const {
  SESS_CLOCK_INLINE, FOLDER_ICON_SVG, CHECK_ICON_SVG, STOP_ICON_SVG, ICON_SVG, fileCardsHtml,
} = window.ClawRenderers;

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
  pendingDatabase: null, // {id, name, engine, tableCount}，本条消息选择的企业数据源
  files: [],
  skills: { skill_md: [], builtin: [], custom: [] },
  tasks: [], tasksPending: [], tasksHistory: [], tasksPendingCount: 0,
  histFilter: { date: "" },
  // 运行状态:有 assistant 消息在跑时锁住发送按钮 / 变停止
  running: false,
  submitting: false,
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

const {
  databaseRequestBeforeAssistant, restoreDatabaseRequest, reconnectDatabaseRequest,
  syncDatabaseComposer, closeDatabasePicker, openDatabasePicker,
} = window.ClawDatabaseUI.create({
  state, api, $, esc,
  toast: (...args) => toast(...args),
  renderAttachChips: (...args) => renderAttachChips(...args),
  closeFilesModal: (...args) => closeFilesModal(...args),
  returnToLogin: (...args) => returnToLogin(...args),
});

const {
  openFilesModal, closeFilesModal, renderFilesModal, loadFiles, renderFilesList,
  showFilePreview,
} = window.ClawFilesUI.create({
  state, api, $, esc, ui,
  pickExistingCipher: (...args) => pickExistingCipher(...args),
});

const {
  renderKeysTab, renderKeycheckResult, renderAuditTab, renderAuditEvent,
  bindKeyDrop, renderAccountTab,
} = window.ClawSettingsUI.create({
  api, $, esc, ui,
  title: (key) => TABS[key].title,
});

const {
  renderSkillsTab, loadSkills, bindSkillDrop, walkEntry, uploadSkillFiles,
  renderSkillMdList, showSkillMd, renderBuiltinSkills, renderCustomSkills,
} = window.ClawSkillsUI.create({
  state, api, $, esc, ui,
  title: (key) => TABS[key].title,
});

const {
  bindWizard, closeTaskWizard, openTaskWizard, renderWizard, wizBack, wizNext,
  submitWizard, toast, renderTasksTab, loadTasksData, scheduleText,
  renderPendingList, renderTaskList, renderTaskHistory, refreshTaskCount,
  refreshTasksBadge, closeTaskPanel, openTaskPanel, openMissedPanel,
  openOverview, leaveOverview, renderOverviewList, renderTaskPanel, renderTPStatus,
  renderTPPending, renderTPMissed, renderTPHistory, renderTPEdit,
  openCreateTaskForm, renderCreateTaskForm, submitCreateTask,
} = window.ClawTasksUI.create({
  state, api, $, esc, ui, FOLDER_ICON_SVG,
  title: (key) => TABS[key].title,
  loadSessions: (...args) => loadSessions(...args),
  selectSession: (...args) => selectSession(...args),
  setSessionView: (...args) => setSessionView(...args),
  closeModal: (...args) => closeModal(...args),
  renderSessionList: (...args) => renderSessionList(...args),
  updateSessionChrome: (...args) => updateSessionChrome(...args),
  syncFooter: (...args) => _syncFooter(...args),
  renderMessage: (...args) => renderMessage(...args),
  showWelcome: (...args) => showWelcome(...args),
});

const { runMetaHtml, renderMessage, typewriter, computeStageIndex, stepHtml, displaySteps } = window.ClawChatUI.create({
  restoreFailedRequest,
  state, api, $, esc, ui, mdToHtml, fileCardsHtml, ICON_SVG, CHECK_ICON_SVG,
  SESS_CLOCK_INLINE, databaseRequestBeforeAssistant, reconnectDatabaseRequest,
  restoreDatabaseRequest, openDatabasePicker, openTaskWizard,
  pollMessage: (...args) => pollMessage(...args),
  setRunning: (...args) => setRunning(...args),
  returnToLogin: (...args) => returnToLogin(...args),
});

// ============ Sidebar ============
async function restoreFailedRequest(mid) {
  if (state.running || state.submitting) { ui.toast("请先等待当前任务结束", "warning"); return; }
  const messages = state.currentSession?.messages || [];
  const index = messages.findIndex(m => m.id === mid);
  if (index < 1) return;
  const request = messages.slice(0, index).reverse().find(m => m.role === "user");
  if (!request) return;
  if ($("input").value.trim() || state.pendingCipher || state.pendingDatabase || state.pendingTexts.length) {
    if (!await ui.confirm("输入区已有内容，是否替换为这次任务的问题和数据来源？", { title: "恢复这次任务", confirmText: "替换内容" })) return;
  }
  state.pendingTexts = [];
  state.pendingCipher = null;
  state.pendingDatabase = null;
  $("input").value = request.content || "";
  $("input").dispatchEvent(new Event("input"));
  if (request.database_source_id) restoreDatabaseRequest(request);
  else if (request.attached_cipher) state.pendingCipher = {path: request.attached_cipher, name: request.attached_cipher.split(/[\\/]/).pop(), uploading: false};
  renderAttachChips();
  $("input").focus();
  ui.toast(request.text_attachment_names?.length ? "问题已恢复，请重新添加原文档，再点击发送" : "问题和数据来源已恢复，核对后点击发送");
}

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

function sessionPins() {
  try { const value = JSON.parse(localStorage.getItem("cw_session_pins") || "[]"); return new Set(Array.isArray(value) ? value : []); }
  catch (_) { return new Set(); }
}
let closeSessionMenu = () => {};
function toggleSessionPin(sid) {
  const pins = sessionPins();
  if (pins.has(sid)) pins.delete(sid); else pins.add(sid);
  localStorage.setItem("cw_session_pins", JSON.stringify([...pins]));
  renderSessionList();
}
async function deleteSessionFromMenu(sid) {
  const session = state.sessions.find(s => s.id === sid);
  if (!session) return;
  const scheduled = isSchedSess(session);
  const message = scheduled
    ? "从列表移除这个定时任务会话？任务仍会继续运行，可从任务的「查看会话」找回。"
    : "删除这个会话？其消息记录会一并丢失。";
  if (!await ui.confirm(message, { danger: !scheduled, confirmText: scheduled ? "从列表移除" : "删除会话" })) return;
  await api("DELETE", `/api/sessions/${sid}`);
  if (sid === state.currentSid) {
    state.currentSid = null; state.currentSession = null;
    showWelcome();
  }
  await loadSessions();
}
function openSessionMenu(event, sid) {
  event.preventDefault();
  closeSessionMenu();
  const session = state.sessions.find(s => s.id === sid);
  if (!session) return;
  const menu = document.createElement("div");
  menu.className = "session-menu";
  menu.setAttribute("role", "menu");
  menu.innerHTML = `<button type="button" role="menuitem" data-action="rename">重命名</button><button type="button" role="menuitem" data-action="pin">${sessionPins().has(sid) ? "取消置顶" : "置顶"}</button><button type="button" role="menuitem" data-action="delete">删除</button>`;
  document.body.appendChild(menu);
  menu.style.left = Math.max(8, Math.min(event.clientX, window.innerWidth - menu.offsetWidth - 8)) + "px";
  menu.style.top = Math.max(8, Math.min(event.clientY, window.innerHeight - menu.offsetHeight - 8)) + "px";
  const controller = new AbortController();
  closeSessionMenu = () => { controller.abort(); menu.remove(); };
  document.addEventListener("pointerdown", e => { if (!menu.contains(e.target)) closeSessionMenu(); }, { signal: controller.signal });
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeSessionMenu(); }, { signal: controller.signal });
  window.addEventListener("resize", closeSessionMenu, { signal: controller.signal });
  menu.addEventListener("click", async e => {
    const action = e.target.closest("button")?.dataset.action;
    if (!action) return;
    closeSessionMenu();
    try {
      if (action === "rename") {
        const title = await ui.prompt("输入会话名称", session.title || "", { title: "重命名会话" });
        if (title === null || title === undefined) return;
        if (!title.trim()) { ui.toast("会话名称不能为空"); return; }
        await api("POST", `/api/sessions/${sid}/title`, { title: title.trim().slice(0, 100) });
        if (state.currentSession?.id === sid) state.currentSession.title = title.trim().slice(0, 100);
        await loadSessions();
      } else if (action === "delete") await deleteSessionFromMenu(sid);
      else if (action === "pin") toggleSessionPin(sid);
    } catch (error) { ui.toast("操作失败：" + error.message, "error"); }
  });
  // 鼠标打开菜单不默认选中第一项；键盘打开时保留可访问的焦点入口。
  if (event.button !== 2 && event.clientX === 0 && event.clientY === 0) {
    menu.querySelector("button").focus();
  }
}

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
  const pins = sessionPins();
  list.sort((a, b) => Number(pins.has(b.id)) - Number(pins.has(a.id)));
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
      <button type="button" class="session-tool" data-session-more title="更多操作" aria-label="更多操作" aria-haspopup="menu"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/></svg></button>
      <button type="button" class="session-tool${pins.has(s.id) ? ' pinned' : ''}" data-session-pin title="${pins.has(s.id) ? '取消置顶' : '置顶'}" aria-label="${pins.has(s.id) ? '取消置顶' : '置顶'}" aria-pressed="${pins.has(s.id)}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M9 3h6l-1 7 4 4H6l4-4-1-7ZM12 14v7"/></svg></button>
    </div>`;
  };
  el.innerHTML = list.map(oneItem).join("");
  el.querySelectorAll(".session").forEach(node => {
    node.addEventListener("contextmenu", e => openSessionMenu(e, node.dataset.id));
    node.addEventListener("click", e => {
      if (e.target.closest(".session-tool")) return;
      selectSession(node.dataset.id);
    });
  });
  el.querySelectorAll(".session-tool").forEach(b => {
    b.addEventListener("click", e => {
      e.stopPropagation();
      const sid = b.closest(".session").dataset.id;
      if (b.hasAttribute("data-session-more")) {
        const rect = b.getBoundingClientRect();
        openSessionMenu({ preventDefault() {}, button: 2, clientX: rect.left, clientY: rect.bottom + 4 }, sid);
      } else {
        closeSessionMenu();
        try { toggleSessionPin(sid); } catch (error) { ui.toast("置顶失败：" + error.message, "error"); }
      }
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
  leaveOverview();   // 退出总概览全屏模式，并由任务模块停止自己的轮询器
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
  leaveOverview();
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
  // 每步至少展示 500ms，终态等待队列；授权面板独立显示，不改变播放进度。
  const STEP_DISPLAY_MS = 500;
  const STAGE_DISPLAY_MS = 1500;
  state.tracePlayback = state.tracePlayback || new Map();
  const rememberPlayback = (count) => state.tracePlayback.set(mid, { count, stage: displayedStage });
  let displayedStage = Number(document.querySelector(`.msg[data-mid="${mid}"] details.trace`)?.dataset?.stage || 0);
  let stageChangedAt = Date.now();
  let lastRevealedAt = 0;
  let latestSteps = [];
  let revealTimer = null;
  let stopped = false;
  let polling = false;
  let retryAfter = 0, connectionFailures = 0;
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
        // 下一条属于后续阶段时等待顶部切换，不能让详细记录抢先显示。
        const nextStage = computeStageIndex(latestSteps.slice(0, box.children.length + 1), "running");
        if (nextStage !== displayedStage) return;
        const s = latestSteps[box.children.length];   // 永远取"下一条",以 DOM 为准
        const wasNearBottom =
          ($("main").scrollHeight - $("main").scrollTop - $("main").clientHeight) < 80;
        const div = document.createElement("div");
        div.className = `step ${s.kind || "step"}`;
        div.innerHTML = stepHtml(s);
        // 新亮出的这步成为"当前活跃步"(脉冲),清掉上一步的活跃态
        box.querySelectorAll(".step.active").forEach(e => e.classList.remove("active"));
        div.classList.add("active");
        box.appendChild(div);
        rememberPlayback(box.children.length);
        lastRevealedAt = Date.now();
        if (wasNearBottom) $("main").scrollTop = $("main").scrollHeight;
        if (box.children.length < latestSteps.length) drain();
      }
    }, STEP_DISPLAY_MS);
  };
  const intv = setInterval(async () => {
    if (polling || Date.now() < retryAfter) return;
    polling = true;
    try {
      const m = await api("GET", `/api/sessions/${sid}/messages/${mid}`);
      connectionFailures = 0;
      let node = document.querySelector(`.msg[data-mid="${mid}"]`);
      const el = node?.querySelector(".run-time");
      if (el) el.textContent = (["done", "failed", "cancelled", "needs_cipher"].includes(m.status) && Number.isFinite(m.duration_sec)
        ? m.duration_sec : (Date.now() - since) / 1000).toFixed(0) + "s";
      const activity = node?.querySelector(".run-status");
      const displayedCount = node?.querySelector('.trace-steps')?.children.length || 0;
      latestSteps = displaySteps(m.steps);
      const hasQueuedSteps = displayedCount < latestSteps.length;
      if (activity) activity.textContent = "执行用时";

      const terminal = (m.status === "done" || m.status === "failed" ||
                        m.status === "needs_cipher" || m.status === "cancelled");
      const awaitingDecrypt = (m.status === "awaiting_decrypt");

      // 出现授权门 → 主动重渲一次(把浮卡渲出来),但不终止轮询;
      // 浮卡已在则不再重渲(避免每 tick 重置折叠/滚动状态)
      const targetStage = computeStageIndex(
        latestSteps.slice(0, displayedCount + 1),
        !hasQueuedSteps && m.status === "done" ? "done" : "running",
      );
      // 阶段只逐个推进，后台快速完成时也不能从第一阶段直接跳到最后。
      if (node && displayedStage !== targetStage && Date.now() - stageChangedAt >= STAGE_DISPLAY_MS) {
        displayedStage = targetStage < displayedStage ? targetStage : displayedStage + 1;
        stageChangedAt = Date.now();
      }
      const presentationPending = !!node && (hasQueuedSteps || Date.now() - lastRevealedAt < STEP_DISPLAY_MS
        || displayedStage !== targetStage || Date.now() - stageChangedAt < STAGE_DISPLAY_MS);
      if (node) rememberPlayback(displayedCount);
      // 授权需要及时响应，不能被展示队列延后；保留现有 trace，仍按原节奏播放。
      if (awaitingDecrypt && !presentationPending && node && !node.querySelector(".decrypt-card")) {
        const existingTrace = node.querySelector("details.trace");
        const fresh = renderMessage(m, { authorizationReady: true });
        if (existingTrace) fresh.querySelector("details.trace")?.replaceWith(existingTrace);
        node.replaceWith(fresh);
        node = fresh;
        $("main").scrollTop = $("main").scrollHeight;
      }
      const idx = state.currentSession?.messages?.findIndex(x => x.id === mid);
      if (idx >= 0) state.currentSession.messages[idx] = m;

      if (terminal && !presentationPending) {
        if (m.status === "failed" && !state.pendingDatabase && !state.pendingCipher) {
          const request = databaseRequestBeforeAssistant(mid);
          if (request && restoreDatabaseRequest(request)) {
            toast("数据库任务失败，已自动恢复原数据库和问题，可刷新连接后重试");
          }
        }
        stopped = true;
        state.tracePlayback.delete(mid);
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
      const steps = latestSteps;
      if (node && steps.length) {
        let stepsBox = node.querySelector(".trace-steps");
        let traceDetails = node.querySelector("details.trace");
        // 极少数情况:首批 step 抵达前 trace 还没渲;补建一个
        if (!stepsBox) {
          const content = node.querySelector(".msg__content");
          if (content) {
            traceDetails = document.createElement("details");
            traceDetails.className = "trace live";
            traceDetails.open = true;
            traceDetails.innerHTML =
              `<summary class="trace-summary"><span class="trace-state">加密计算过程 · 实时展示</span><span class="trace-live">实时</span></summary>
               <div class="trace-steps"></div>`;
            // 插入到 run-pill 前面(若有),否则放最前
            const pill = content.querySelector(".run-pill");
            content.insertBefore(traceDetails, pill || content.firstChild);
            stepsBox = traceDetails.querySelector(".trace-steps");
          }
        }
        if (traceDetails) {
          const visibleSteps = steps.slice(0, stepsBox?.children.length || 0);
          const currentStage = displayedStage;
          traceDetails.dataset.stage = String(currentStage);
          const stateText = traceDetails.querySelector(".trace-state");
          if (stateText) stateText.textContent = terminal ? "加密计算过程 · 正在展示执行记录" : "加密计算过程 · 执行中";
          traceDetails.querySelectorAll("[data-process-stage]").forEach(stageNode => {
            const index = Number(stageNode.dataset.processStage || 0);
            stageNode.classList.toggle("done", index < currentStage);
            stageNode.classList.toggle("active", index === currentStage);
            stageNode.classList.toggle("pending", index > currentStage);
            const number = stageNode.querySelector("span");
            if (number) number.textContent = index < currentStage ? "✓" : String(index + 1);
          });
        }
        if (stepsBox) {
          latestSteps = steps;
          if (stepsBox.children.length < steps.length) drain();
        }
      }
    } catch (e) {
      if (e.status === 401) {
        stopped = true;
        clearInterval(intv);
        if (revealTimer) clearTimeout(revealTimer);
        state.pollingMids.delete(mid);
        state.tracePlayback.delete(mid);
        setRunning(false);
        const activity = document.querySelector(`.msg[data-mid="${mid}"] .run-status`);
        if (activity) activity.textContent = "登录已过期，请重新登录";
        if (!state.loginExpiredNotice) {
          state.loginExpiredNotice = true;
          await ui.alert("登录已过期，无法继续获取任务进度。重新登录后可查看原会话；未完成的任务需要重新发送。", { title: "需要重新登录", confirmText: "前往登录" });
          window.location.assign("/login");
        }
        return;
      }
      connectionFailures++;
      retryAfter = Date.now() + Math.min(30000, 1500 * Math.pow(2, Math.min(connectionFailures, 5)));
      const node = document.querySelector(`.msg[data-mid="${mid}"]`);
      const activity = node?.querySelector(".run-status");
      if (activity) activity.textContent = "暂时无法获取进度，正在重新连接…";
    } finally {
      polling = false;
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
  if (state.submitting) return;
  // 运行中按了"停止"
  if (state.running) {
    await stopRunning();
    return;
  }

  const text = $("input").value.trim();
  if (!text) return;

  // 还在上传 cipher / 抽文本 阻塞
  if (state.pendingCipher && state.pendingCipher.uploading) {
    ui.toast("文件仍在加密，请稍候", "warning"); return;
  }
  if (state.pendingTexts.some(t => t.uploading)) {
    ui.toast("文档仍在读取，请稍候", "warning"); return;
  }

  state.submitting = true;
  $("sendBtn").disabled = true;
  try {
    if (!state.currentSid) await createSession();
  } catch (e) {
    state.submitting = false; $("sendBtn").disabled = false;
    ui.toast("无法创建会话：" + e.message, "error"); return;
  }
  $("input").value = "";
  $("input").style.height = "auto";

  const attached_cipher = state.pendingCipher?.path || "";
  const database = state.pendingDatabase;
  const text_attachments = state.pendingTexts
    .filter(t => !t.uploading && t.content)
    .map(t => ({ name: t.name, content: t.content }));
  const text_attachment_llm_consent = text_attachments.length > 0 &&
    state.pendingTexts.filter(t => !t.uploading && t.content).every(t => t.llmConsent === true);
  state.pendingCipher = null;
  state.pendingTexts = [];
  state.pendingDatabase = null;
  renderAttachChips();

  try {
    const res = await api("POST", `/api/sessions/${state.currentSid}/messages`, {
      content: text, attached_cipher, text_attachments, text_attachment_llm_consent,
      database_source_id: database?.id || "",
      database_source_name: database?.name || "",
      web_search: !!state.webSearch,
    });
    state.currentSession.messages.push(res.user_message, res.assistant_message);
    if (state.currentSession.messages.length === 2 && state.currentSession.title === "新会话") {
      state.currentSession.title = text.slice(0, 40);
    }
    renderChat();
    setRunning(true, res.assistant_message.id);
    pollMessage(state.currentSid, res.assistant_message.id);
    // 成功发送后退出输入状态，不再自动把光标放回输入框。
    $("input").blur();
    $("sendBtn").blur();
    loadSessions();
  } catch (e) {
    state.pendingCipher = attached_cipher ? {name: attached_cipher.split(/[\\/]/).pop(), path: attached_cipher, uploading: false} : null;
    state.pendingTexts = text_attachments.map(t => ({
      ...t, chars: t.content.length, uploading: false, llmConsent: text_attachment_llm_consent,
    }));
    state.pendingDatabase = database;
    renderAttachChips();
    if (!$("input").value.trim()) $("input").value = text;
    await ui.alert("发送失败：" + e.message, { title: "消息未发送，问题与附件已保留", kind: "danger" });
    setRunning(false);
    $("input").focus({ preventScroll: true });
  } finally {
    state.submitting = false;
    $("sendBtn").disabled = false;
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
  if (state.pendingDatabase) {
    const d = state.pendingDatabase;
    chips.push(`
      <div class="att-chip database">
        <svg class="ic-tiny" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg>
        <span class="nm">企业数据库 · ${esc(d.name)}</span>
        <span class="sz">${d.needsRefresh ? "待刷新连接" : `${d.tableCount || 0} 张授权表`}</span>
        <button class="rm" data-rm-database="1">×</button>
      </div>`);
  }
  state.pendingTexts.forEach((t, i) => {
    chips.push(`
      <div class="att-chip text ${t.uploading ? "uploading" : ""}">
        <svg class="ic-tiny" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>
        <span class="nm">${esc(t.name)}</span>
        ${t.chars ? `<span class="sz">${t.chars} 字 · 正文发送给模型</span>` : ""}
        <button class="rm" data-rm-text="${i}">×</button>
      </div>
    `);
  });
  box.innerHTML = chips.join("");
  box.querySelector("[data-rm-cipher]")?.addEventListener("click", () => {
    state.pendingCipher = null; renderAttachChips();
  });
  box.querySelector("[data-rm-database]")?.addEventListener("click", () => {
    state.pendingDatabase = null;
    syncDatabaseComposer();
    renderAttachChips();
  });
  box.querySelectorAll("[data-rm-text]").forEach(b => {
    b.addEventListener("click", () => {
      state.pendingTexts.splice(+b.dataset.rmText, 1);
      renderAttachChips();
    });
  });
  syncDatabaseComposer();
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
      await ui.alert(`暂不支持 .${ext} 文件。\n数据文件：${DATA_EXTS.join(" / ")}\n文档文件：${TEXT_EXTS.join(" / ")}`, { title: "无法添加此文件" });
    }
  }
}

async function _attachDataFile(file) {
  // 同消息最多一个密文(replace 旧的)
  if (state.pendingDatabase) {
    state.pendingDatabase = null;
    syncDatabaseComposer();
    toast("已切换为 Excel/CSV 数据附件");
  }
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
    await ui.alert("加密失败：" + e.message, { title: "附件处理失败", kind: "danger" });
  }
}

async function _attachTextFile(file) {
  const agreed = await ui.confirm(
    "文本附件安全提示\n\nWord、PDF、TXT 等文档正文需要发送给管理端配置的大模型，模型才能阅读其中的公式和内容。\n\nExcel、CSV 和企业数据库的数值数据仍会先在本机加密，不会发送明文数据行。\n\n是否继续添加该文档？"
    , { title: "添加文档前请确认", confirmText: "同意并添加" }
  );
  if (!agreed) return;
  const chip = {
    name: file.name + " (读取中…)", uploading: true, content: "", chars: 0,
    llmConsent: true,
  };
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
    await ui.alert("文本读取失败：" + e.message, { title: "附件处理失败", kind: "danger" });
  }
}

function pickExistingCipher(path, name) {
  state.pendingDatabase = null;
  syncDatabaseComposer();
  state.pendingCipher = { name: name || path.split("/").pop(), path, uploading: false };
  renderAttachChips();
  closeFilesModal();
  $("input")?.focus();
}

// ============ 会话数据源：企业数据库 ============
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

async function renderDatabaseTab() {
  $("modalBody").innerHTML = `<h2>${TABS.database.title}</h2>
    <p class="sub">仅显示管理员已授权的数据源、表和字段。数据库地址、账号和密码不会下发到本机。</p>
    <div id="dbAlert"></div><div id="dbBody"><div class="db-loading">正在读取授权目录…</div></div>`;
  let sources;
  try { sources = await api("GET", "/api/data/sources"); }
  catch (e) { $("dbBody").innerHTML = `<div class="alert-box">读取失败：${esc(e.message)}</div>`; return; }
  if (!sources.length) {
    $("dbBody").innerHTML = `<div class="db-empty"><strong>尚无数据库访问权限</strong><span>请联系管理员在“数据权限”中授权。</span></div>`; return;
  }
  $("dbBody").innerHTML = `<div class="field"><label>数据源</label><select id="dbSource">${sources.map(s=>`<option value="${esc(s.id)}">${esc(s.name)} · ${esc(s.engine)}</option>`).join("")}</select></div>
    <div class="db-catalog" id="dbCatalog"></div>
    <div class="db-natural"><div class="field"><label>用大白话描述要查什么</label><textarea id="dbIntent" rows="3" placeholder="例如：按客户统计今年订单金额，取金额最高的前 20 名"></textarea><p class="hint">AI 只会看到上方授权表结构，不会看到数据库地址、密码、行级策略或任何数据行。</p></div><button class="btn-ghost" id="dbPlan">生成候选查询</button></div>
    <div class="db-divider"><span>候选 SQL（可检查和修改）</span></div>
    <div class="field"><label>只读 SQL</label><textarea id="dbSql" rows="7" placeholder="例如：SELECT id, amount FROM orders WHERE order_date >= '2026-01-01'"></textarea><p class="hint">必须明确列名，禁止 SELECT *；AI 生成后仍会检查表、字段、行范围、脱敏和行数权限。</p></div>
    <div class="db-actions"><button class="btn-ghost" id="dbPreview">检查并预览</button><button class="btn-primary" id="dbExecute" disabled>提取数据</button></div>
    <div id="dbPreviewBox"></div><div id="dbResult"></div>`;
  const src=$("dbSource"), sql=$("dbSql"), intent=$("dbIntent"), execute=$("dbExecute"), plan=$("dbPlan");
  async function loadCatalog(){
    $("dbCatalog").innerHTML='<div class="db-loading">读取表结构…</div>';
    try { const rows=await api("GET",`/api/data/sources/${encodeURIComponent(src.value)}/catalog`);
      const maskNames={partial:'部分隐藏',hash:'哈希',null:'置空'};
      $("dbCatalog").innerHTML=rows.map(t=>`<div class="db-table"><div class="db-table__head"><strong>${esc(t.schema)}.${esc(t.table)}</strong><span>${t.row_restricted?'行范围受限 · ':''}最多 ${t.max_rows} 行 · ${t.operations.map(o=>esc(o)).join(' / ')}</span></div><div class="db-cols">${t.columns.map(c=>`<button type="button" class="db-col ${c.mask?'masked':''}" data-table="${esc(t.table)}" data-col="${esc(c.name)}" title="${esc(c.type)}${c.comment?' · '+esc(c.comment):''}${c.mask?' · '+maskNames[c.mask]+'脱敏':''}">${esc(c.name)} <small>${esc(c.type)}${c.mask?' · '+maskNames[c.mask]:''}</small></button>`).join('')}</div></div>`).join('') || '<div class="db-empty">没有可见字段</div>';
      document.querySelectorAll('.db-col').forEach(b=>b.addEventListener('click',()=>{ const txt=`SELECT ${b.dataset.col} FROM ${b.dataset.table}`; if(!sql.value.trim()) sql.value=txt; }));
    } catch(e){$("dbCatalog").innerHTML=`<div class="alert-box">${esc(e.message)}</div>`;}
  }
  src.addEventListener('change',()=>{execute.disabled=true; $("dbPreviewBox").innerHTML=''; loadCatalog();});
  sql.addEventListener('input',()=>{execute.disabled=true;});
  plan.addEventListener('click',async()=>{
    const q=intent.value.trim(); if(!q){intent.focus(); return;}
    plan.disabled=true; plan.textContent='正在生成…'; execute.disabled=true;
    $("dbPreviewBox").innerHTML='<div class="db-loading">AI 正在根据授权结构生成候选，并通过安全网关检查…</div>';
    try { const p=await api("POST","/api/data/query/plan",{data_source_id:src.value,intent:q,operation:'query'});
      sql.value=p.sql;
      $("dbPreviewBox").innerHTML=`<div class="db-preview db-ai-preview"><strong>候选查询已生成，尚未执行</strong><div>${esc(p.explanation)}</div><div>将访问：${p.tables.map(esc).join('、')} · 字段：${p.columns.map(esc).join('、')} · 最多 ${p.max_rows} 行</div><code>${esc(p.preview_sql||p.sql)}</code><div class="db-confirm-note">上方为安全网关将实际执行的预览。请检查编辑框和访问范围；确认无误后再点击“提取数据”。</div></div>`;
      execute.disabled=false;
    } catch(e){$("dbPreviewBox").innerHTML=`<div class="alert-box">生成失败：${esc(e.message)}</div>`;}
    finally {plan.disabled=false; plan.textContent='生成候选查询';}
  });
  $("dbPreview").addEventListener('click',async()=>{
    execute.disabled=true; $("dbPreviewBox").innerHTML='<div class="db-loading">正在检查权限与 SQL…</div>';
    try { const p=await api("POST","/api/data/query/preview",{data_source_id:src.value,sql:sql.value,operation:'query'});
      $("dbPreviewBox").innerHTML=`<div class="db-preview"><strong>检查通过</strong><div>将访问：${p.tables.map(esc).join('、')} · 字段：${p.columns.map(esc).join('、')} · 最多 ${p.max_rows} 行</div><code>${esc(p.sql)}</code></div>`; execute.disabled=false;
    } catch(e){$("dbPreviewBox").innerHTML=`<div class="alert-box">未通过：${esc(e.message)}</div>`;}
  });
  execute.addEventListener('click',async()=>{
    execute.disabled=true; execute.textContent='提取中…'; $("dbResult").innerHTML='';
    let taskId=''; let cancelled=false;
    try {
      const created=await api("POST","/api/data/query/execute",{data_source_id:src.value,sql:sql.value,operation:'query'});
      taskId=created.task_id;
      $("dbResult").innerHTML=`<div class="db-result-head"><strong id="dbTaskStatus">查询已进入队列</strong><span id="dbTaskElapsed">0.0 秒</span></div><div class="db-preview"><div>管理端正在受控执行；可以留在此页面查看进度，也可以主动取消。</div></div><button class="btn-danger" id="dbCancelQuery">取消查询</button>`;
      $("dbCancelQuery")?.addEventListener('click',async()=>{
        cancelled=true; const btn=$("dbCancelQuery"); if(btn){btn.disabled=true;btn.textContent='正在取消…';}
        try{await api("DELETE",`/api/data/query/tasks/${encodeURIComponent(taskId)}`);}catch(e){toast(`取消请求失败：${e.message}`);}
      });
      let status=created;
      while(!['success','failed','cancelled','timeout','denied'].includes(status.status)){
        await new Promise(resolve=>setTimeout(resolve,500));
        status=await api("GET",`/api/data/query/tasks/${encodeURIComponent(taskId)}`);
        const labels={queued:'等待执行',running:'正在查询',success:'查询完成',failed:'查询失败',cancelled:'已取消',timeout:'已超时',denied:'已拒绝'};
        if($("dbTaskStatus")) $("dbTaskStatus").textContent=labels[status.status]||status.status;
        if($("dbTaskElapsed")) $("dbTaskElapsed").textContent=`${(status.elapsed_ms/1000).toFixed(1)} 秒`;
      }
      if(status.status!=='success'){
        const labels={cancelled:'查询已取消',timeout:'查询已超过管理员设置的时间限制',denied:'查询被安全限制拒绝',failed:'查询执行失败'};
        throw new Error(`${labels[status.status]||'查询未完成'}${status.error?'：'+status.error:''}`);
      }
      if($("dbTaskStatus")) $("dbTaskStatus").textContent='查询完成，正在本地加密…';
      if($("dbCancelQuery")) $("dbCancelQuery").disabled=true;
      const r=await api("POST",`/api/data/query/tasks/${encodeURIComponent(taskId)}/result`);
      const enc=r.encrypted||{}; state.pendingCipher={name:enc.name,path:enc.path,uploading:false}; renderAttachChips();
      $("dbResult").innerHTML=`<div class="db-result-head"><strong>已提取并自动加密 ${r.row_count} 行</strong><span>耗时 ${r.duration_ms} ms</span></div><div class="db-preview"><strong>已加入当前会话附件</strong><div>密文文件：${esc(enc.name||'')} · 加密字段：${(enc.encrypted_columns||[]).map(esc).join('、')||'无数值字段'} · 明文标识字段：${(enc.plaintext_columns||[]).map(esc).join('、')||'无'}</div></div><button class="btn-primary" id="dbContinue">返回会话并分析</button>`;
      $("dbContinue")?.addEventListener('click',()=>{closeModal(); $("input")?.focus();}); loadFiles();
    } catch(e){$("dbResult").innerHTML=`<div class="alert-box">${cancelled?'查询已取消':`提取失败：${esc(e.message)}`}</div>`;}
    finally {execute.disabled=false; execute.textContent='提取数据';}
  });
  await loadCatalog();
}

// ============ 自启 / 运维 Tab(客户端自身开机自启 + 崩溃重启)============
let _opsTimer = null;
async function renderOpsTab() {
  $("modalBody").innerHTML = `
    <h2>${TABS.ops.title}</h2>
    <p class="sub">让本机的用户端(数据面 :8444)开机自动启动、崩溃后自动重启。仅作用于这台电脑。</p>
    <div id="opsAlert"></div>

    <div class="ops-cards" id="opsCards"></div>

    <div class="field form-action-row">
      <button class="btn-primary" id="opsEnable">启用开机自启 + 守护</button>
      <button class="btn-danger" id="opsDisable">停用</button>
      <button class="btn-ghost" id="opsStart">仅启动守护(本次)</button>
      <button class="btn-ghost" id="opsStop">停止守护</button>
    </div>

    <div class="alert-box info info-panel-spaced">
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
  $("opsDisable").addEventListener("click", async e => {
    if (await ui.confirm("停用后用户端仍会继续运行，但异常退出后不会自动恢复。", { title: "停用开机自启和守护？", danger: true, confirmText: "确认停用" })) {
      act("/api/ops/autostart/disable", e.target, "停用中…");
    }
  });
  $("opsStart").addEventListener("click", e => act("/api/ops/supervisor/start", e.target, "启动中…"));
  $("opsStop").addEventListener("click", e => act("/api/ops/supervisor/stop", e.target, "停止中…"));
}

// ============ 定时任务 Tab ============
// ============ 创建定时任务向导(内联在输入框上方 · 一步步补全)============
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

  $("databaseBtn")?.addEventListener("click", openDatabasePicker);
  $("databaseClose")?.addEventListener("click", closeDatabasePicker);
  $("databaseMask")?.addEventListener("click", e => {
    if (e.target === $("databaseMask")) closeDatabasePicker();
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

  // 统一键盘交互：Esc 关闭最上层面板；应用内确认框自身优先处理。
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || document.querySelector(".cw-dialog-mask.open")) return;
    if (state.noticeOpen) { closeNotices(); return; }
    const masks = Array.from(document.querySelectorAll(".modal-mask.open"));
    const top = masks[masks.length - 1];
    if (top) {
      if (top.id === "modalMask") closeModal();
      else if (top.id === "databaseMask") closeDatabasePicker();
      else if (top.id === "filesMask") closeFilesModal();
      else if (top.id === "taskPanelMask") closeTaskPanel();
      else top.remove();
      return;
    }
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
