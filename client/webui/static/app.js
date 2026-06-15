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
  renderSessionList();
}

function renderSessionList() {
  const el = $("sessionList");
  if (!state.sessions.length) {
    el.innerHTML = '<div class="session-empty">还没有会话<br>点击上方"新建会话"</div>';
    return;
  }
  const SESS_CLOCK_SVG = `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>`;
  const SESS_CHAT_SVG = `<svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`;
  el.innerHTML = state.sessions.map(s => {
    const isSched = (s.title || "").startsWith("⏰");
    const title = isSched ? s.title.replace(/^⏰\s*/, "") : s.title;
    return `
    <div class="session ${s.id === state.currentSid ? "active" : ""} ${isSched ? "sched" : ""}" data-id="${s.id}">
      ${isSched ? SESS_CLOCK_SVG : SESS_CHAT_SVG}
      <span class="session__title">${esc(title)}</span>
      <button class="session__del" data-del="${s.id}" title="删除会话">
        <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="3 6 5 6 21 6"/><path d="M19 6l-2 14a2 2 0 0 1-2 2H9a2 2 0 0 1-2-2L5 6"/>
        </svg>
      </button>
    </div>
  `;
  }).join("");
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
  // 切会话时把"运行中"状态复位 —— 进的新会话单独跟踪
  setRunning(false);
  const data = await api("GET", `/api/sessions/${sid}/messages`);
  state.currentSession = data.session;
  state.currentSession.messages = data.messages;
  // 已经在视图里的 assistant summary 不再播打字机(只对新消息播)
  (state.currentSession.messages || []).forEach(m => {
    if (m.role === "assistant" && m.status === "done") state.typedMids.add(m.id);
  });
  renderSessionList();
  renderChat();
  enableComposer();
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
      steps.forEach(s => {
        const cls = s.kind || "step";
        content += `<div class="step ${cls}">${esc(s.label)}</div>`;
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
    }
  }
  content += `</div>`;
  wrap.innerHTML = avatar + content;

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
  renderChat();
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
  tasks:   { title: "定时任务", render: renderTasksTab },
  skills:  { title: "Skill 管理", render: renderSkillsTab },
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

// ============ 定时任务 Tab ============
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
    $("tkFolderWrap").style.display = srcSel.value === "folder" ? "" : "none";
  }
  srcSel.addEventListener("change", syncSource); syncSource();

  // 原生选择文件夹(macOS / Windows / Linux)
  $("tkFolderBtn").addEventListener("click", async () => {
    const btn = $("tkFolderBtn");
    btn.disabled = true; btn.textContent = "选择中…";
    try {
      const r = await api("POST", "/api/pick_folder");
      if (!r.cancelled && r.path) $("tkFolder").value = r.path;
    } catch (e) {
      $("tasksAlert").innerHTML = `<div class="alert-box">选择失败:${esc(e.message)} · 可手动粘贴路径</div>`;
    } finally {
      btn.disabled = false; btn.textContent = "选择文件夹";
    }
  });

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
      at_hour: hh || 0, at_minute: mm || 0,
      weekday: parseInt($("tkWeekday").value, 10) || 0,
      day_of_month: parseInt($("tkMonthDay").value, 10) || 1,
      interval_minutes: parseInt($("tkInterval").value, 10) || 60,
      cron_expr: kind === "cron" ? $("tkCron").value.trim() : "",
      cron_readable: kind === "cron" ? ($("cronResult").querySelector("strong")?.textContent || "") : "",
    };
    try {
      await api("POST", "/api/scheduled_tasks", body);
      $("tkName").value = ""; $("tkQuestion").value = ""; $("tkFolder").value = "";
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
  catch { state.tasksPending = { runs: [], encrypted: [] }; }
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
  const data = state.tasksPending || { runs: [], encrypted: [] };
  const runs = data.runs || [];
  const enc = data.encrypted || [];
  const total = runs.length + enc.length;
  const badge = $("pendBadge");
  if (badge) { badge.style.display = total ? "" : "none"; badge.textContent = total; }
  if (!total) { box.innerHTML = '<div class="alert-box info">没有待批运行</div>'; return; }

  let html = "";
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
  launched: "ok", decrypted: "ok", queued: "warn", skipped: "no", failed: "no",
};
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

// 顶栏待批红点
async function refreshTasksBadge() {
  try {
    const r = await api("GET", "/api/scheduled_tasks");
    const n = r.pending_count || 0;
    const badge = $("tasksBadge");
    if (badge) { badge.style.display = n ? "" : "none"; badge.textContent = n > 99 ? "99+" : n; }
  } catch {}
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

  // settings
  $("settingsBtn").addEventListener("click", () => openModal("general"));
  $("modalClose").addEventListener("click", closeModal);
  $("modalMask").addEventListener("click", e => {
    if (e.target === $("modalMask")) closeModal();
  });
  document.querySelectorAll(".tab-btn").forEach(b => {
    b.addEventListener("click", () => openModal(b.dataset.tab));
  });

  // 密文文件管理(顶栏入口)
  $("filesBtn")?.addEventListener("click", openFilesModal);
  $("filesClose")?.addEventListener("click", closeFilesModal);
  $("filesMask")?.addEventListener("click", e => {
    if (e.target === $("filesMask")) closeFilesModal();
  });

  // 定时任务(顶栏入口)
  $("tasksBtn")?.addEventListener("click", () => openModal("tasks"));

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
  // 定时任务待批红点:首刷 + 每 30s 轮询
  refreshTasksBadge();
  setInterval(refreshTasksBadge, 30000);
  // 当前会话后台同步:每 4s 接住服务端(定时任务)注入的新消息
  setInterval(syncCurrentSession, 4000);
  // 侧栏每 15s 刷一次,捕获定时任务新建的「⏰」会话
  setInterval(() => { if (!state.running && !state.pollingMids.size) loadSessions(); }, 15000);
})();
