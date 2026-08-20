/* 会话消息渲染、交互卡片和打字动画。 */
(function initClawChatUI(global) {
  "use strict";
  function create({
    state, api, $, esc, mdToHtml, fileCardsHtml, ICON_SVG, CHECK_ICON_SVG,
    SESS_CLOCK_INLINE, databaseRequestBeforeAssistant, reconnectDatabaseRequest,
    restoreDatabaseRequest, openDatabasePicker, openTaskWizard, pollMessage,
    setRunning, returnToLogin,
  }) {
  function runMetaHtml(m) {
    const dt = m.created_at || "";
    const date = dt.slice(0, 10), time = dt.slice(11, 19);
    const dur = m.duration_sec ? ` · 耗时 ${m.duration_sec}s` : "";
    const tok = ` · 消耗 ${(m.tokens || 0).toLocaleString()} tokens`;
    const when = (date || time) ? `执行于 ${esc(date)} ${esc(time)}` : "已执行";
    return `<div class="run-meta">${when}${dur}${tok}</div>`;
  }

  async function returnToLogin() {
    try {
      await api("POST", "/logout");
    } catch (e) {
      // 即使清理请求失败，也允许用户回到登录页重新建立连接。
    }
    window.location.assign("/login");
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
      if (m.database_source_id) {
        lines.push(`<span class="att-piece database">${ICON_SVG.doc} 企业数据库 · ${esc(m.database_source_name || m.database_source_id)} · 已加密查询</span>`);
      } else if (m.attached_cipher) {
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
        const databaseRequest = databaseRequestBeforeAssistant(m.id);
        if (databaseRequest) {
          content += `<div class="db-recovery-actions">
            <button class="btn-primary" type="button" data-db-reconnect="1">重新连接数据库</button>
            <button class="btn-ghost" type="button" data-db-reselect="1">更换数据库</button>
            <span>已自动保留原问题，连接成功后可再次发送。</span>
          </div>`;
        }
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

    wrap.querySelector("[data-db-reconnect]")?.addEventListener("click", e => {
      reconnectDatabaseRequest(databaseRequestBeforeAssistant(m.id), e.currentTarget);
    });
    wrap.querySelector("[data-db-reselect]")?.addEventListener("click", async () => {
      const request = databaseRequestBeforeAssistant(m.id);
      if (request) restoreDatabaseRequest(request);
      await openDatabasePicker();
    });

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


    return { runMetaHtml, renderMessage, typewriter };
  }
  global.ClawChatUI = Object.freeze({ create });
}(window));
