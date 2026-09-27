/* 会话消息渲染、交互卡片和打字动画。 */
(function initClawChatUI(global) {
  "use strict";
  function create({
    state, api, $, esc, ui, mdToHtml, fileCardsHtml, ICON_SVG, CHECK_ICON_SVG,
    SESS_CLOCK_INLINE, databaseRequestBeforeAssistant, reconnectDatabaseRequest,
    restoreDatabaseRequest, openDatabasePicker, openTaskWizard, pollMessage,
    setRunning, returnToLogin, restoreFailedRequest,
  }) {
  function runMetaHtml(m) {
    const dt = m.created_at || "";
    const date = dt.slice(0, 10), time = dt.slice(11, 19);
    const dur = m.duration_sec ? ` · 耗时 ${m.duration_sec}s` : "";
    const tok = ` · 消耗 ${(m.tokens || 0).toLocaleString()} tokens`;
    const when = (date || time) ? `执行于 ${esc(date)} ${esc(time)}` : "已执行";
    return `<div class="run-meta">${when}${dur}${tok}</div>`;
  }

  const PROCESS_STAGES = ["理解任务", "LLM 分析", "载入密文", "加密计算", "本机解密", "生成结果"];
  state.tracePlayback = state.tracePlayback || new Map();

  function displaySteps(steps) {
    return (steps || []).filter((s, i, all) => s.kind === "error" ||
      (!/加载身份列 sidecar|代码生成 · 加载技能文档|本机执行计时|同态工具计时/.test(s.label || "") &&
       (i === 0 || s.label !== all[i - 1].label)));
  }

  function stepDescription(step) {
    const label = step.label || "";
    if (step.kind === "error") return "本环节遇到问题，具体原因见这条记录。是否重试或停止以随后状态为准。";
    if (/识别意图/.test(label)) return "根据你的问题和所选数据，确定这次需要处理的任务。";
    if (/锁定结果必需指标/.test(label)) return "确认结果必须包含哪些指标，供后续生成计算方案和检查输出使用。";
    if (/调用 LLM.*生成|重新生成/.test(label)) return "请求模型依据任务要求和字段结构编写计算代码；生成后还需检查并运行。";
    if (/安全扫描/.test(label)) return "检查生成代码中的操作是否符合安全限制，通过检查后才能继续执行。";
    if (/加载密文/.test(label)) return "打开已加密的数据文件，准备本次运算需要的输入。";
    if (/密文数据已载入/.test(label)) return "已读取密文数值字段，接下来校验计算方案；此步骤不向模型发送这些数值。";
    if (/小样本校验/.test(label)) return "先用小样本验证代码能否运行、输出是否满足要求，再处理完整数据。";
    if (/等待解密/.test(label)) return "计算已结束，等待你决定是否解密查看输出；也可以直接导出密文。";
    if (/受控解密/.test(label)) return "此处实际发生了本机解密，数据进入本机内存；这不是纯密文运算步骤。";
    if (/用户选择/.test(label)) return "记录你对结果展示方式的选择，后续按该选择处理，不自动代替你授权。";
    if (/密态计算完成|就绪/.test(label)) return "计算引擎已返回这部分结果，接下来处理授权或整理输出文件。";
    if (/加密数据分析|密态运算|实际运算/.test(label) || step.kind === "compute") return "计算引擎正在执行记录所列的操作；如涉及本机解密，会单独显示对应记录。";
    if (/产出 Excel|导出|完成 ·/.test(label)) return "将计算结果整理成文件，按所选方式提供明文或密文下载。";
    if (/跳过|不足|缺少/.test(label)) return "检查发现当前数据不足以完成此项，记录缺失条件，保留其他可完成的结果。";
    return "执行上方所列操作，并记录该环节的实际状态。";
  }

  function stepHtml(step) {
    const path = step.kind === "error" ? '<path d="m6 6 12 12M6 18 18 6"/>' : step.kind === "result" ? '<path d="m5 12 4 4L19 6"/>' : step.kind === "compute" ? '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>' : '<path d="M9 18h6M10 21h4M8 14a6 6 0 1 1 8 0l-1 2H9z"/>';
    return `<svg class="step-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg><svg class="step-spinner" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="8" opacity=".2"/><path d="M12 4a8 8 0 0 1 8 8"/></svg><span class="step-copy"><span class="step-label">${esc(step.label || "")}</span><small class="step-description">${esc(stepDescription(step))}</small></span>`;
  }

  function computeStageIndex(steps, status) {
    if (status === "done") return 5;
    let stage = 0;
    for (const step of steps || []) {
      const label = String(step.label || "");
      if (/带反馈重新生成|反馈 LLM|重生成一次|重新生成|重写/.test(label)) { stage = 0; continue; }
      let next = 0;
      if (/产出 Excel|生成结果|结果加密暂存/.test(label)) next = 5;
      else if (/等待解密|受控解密|用户选择.*解密|本机解密/.test(label)) next = 4;
      else if (/密态运算|密文.*运算|加密数据分析|实际运算|sheet.*就绪|密态计算完成/.test(label)) next = 3;
      else if (/加载密文|密文数据已载入|本机加密/.test(label)) next = 2;
      else if (/调用 LLM|安全扫描|重生成后/.test(label)) next = 1;
      stage = Math.max(stage, next);
    }
    return stage;
  }

  function processStagesHtml(steps, status, stage) {
    const current = stage ?? computeStageIndex(steps, status);
    return `<div class="compute-stages" aria-label="加密计算阶段">${PROCESS_STAGES.map((label, index) => {
      const stateClass = index < current ? "done" : index === current ? "active" : "pending";
      return `<div class="compute-stage ${stateClass}" data-process-stage="${index}"><span>${index < current ? "✓" : index + 1}</span><b>${label}</b></div>`;
    }).join('<i aria-hidden="true">→</i>')}</div>`;
  }

  async function returnToLogin() {
    try {
      await api("POST", "/logout");
    } catch (e) {
      // 即使清理请求失败，也允许用户回到登录页重新建立连接。
    }
    window.location.assign("/login");
  }

  function renderMessage(m, { authorizationReady = false } = {}) {
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
        const nm = m.attached_cipher.split(/[\\/]/).pop();
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
      // 运行中的首次渲染也交由轮询队列逐条展示，避免首批事件同时出现。
      if ((running || awaitingDecrypt) && !state.tracePlayback.has(m.id)) {
        state.tracePlayback.set(m.id, { count: 0, stage: 0 });
      }
      const playback = state.tracePlayback.get(m.id);
      const allSteps = displaySteps(m.steps);
      const steps = playback ? allSteps.slice(0, playback.count) : allSteps;
      const visibleStage = playback ? playback.stage : computeStageIndex(steps, m.status);

      // 进度行(running 时实时追加;done 时默认折叠)
      // 用 <details> 让用户可以折叠/展开;running 时默认打开,done 时默认收起
      if (steps.length || running || awaitingDecrypt) {
        const detailsOpen = (running || awaitingDecrypt || playback) ? " open" : "";
        const stateLabel = awaitingDecrypt
          ? "加密计算过程 · 计算完成，等待本机解密"
          : running
          ? "加密计算过程 · 执行中"
          : `加密计算过程 · ${failed ? "未完成" : cancelled ? "已停止" : "已完成"} · ${steps.length} 步`;
        content += `<details class="trace${running || awaitingDecrypt ? " live" : ""}" data-stage="${visibleStage}"${detailsOpen}>`;
        content += `<summary class="trace-summary"><span class="trace-state">${stateLabel}</span>${running ? '<span class="trace-live">实时</span>' : ""}</summary>`;
        content += processStagesHtml(steps, m.status, visibleStage);
        content += `<div class="trace-steps">`;
        steps.forEach((s, i) => {
          const cls = s.kind || "step";
          // running 时,最后一步是"当前正在跑"的阶段 → 加 active 脉冲,告诉用户卡在哪一步
          const act = (running && i === steps.length - 1) ? " active" : "";
          content += `<div class="step ${esc(cls)}${act}">${stepHtml(s)}</div>`;
        });
        content += `</div></details>`;
      }

      if (running) {
        content += `
          <div class="run-pill">
            <span class="run-status">执行用时</span>
            <span class="run-time" data-since="${m.created_at}">0s</span>
          </div>`;
      } else if (failed) {
        content += `<div class="err-card">${ICON_SVG.warn}<span class="err-text">${esc(m.error || "未知错误")}</span></div>`;
        content += `<div class="db-recovery-actions"><button type="button" class="btn-ghost" data-restore-request>恢复问题与数据，修改后重试</button></div>`;
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
        const waitingForDecrypt = allSteps.some(step => /等待解密.*授权/.test(step.label || ""));
        const cancelText = waitingForDecrypt
          ? "解密授权未完成，任务已取消（可能已超时）。请重新发起任务，再选择解密查看或保留密文。"
          : "任务已停止，可恢复问题后重新发送。";
        content += `<div class="cancel-notice" role="status">${cancelText}</div>`;
        content += `<div class="db-recovery-actions"><button type="button" class="btn-ghost" data-restore-request>恢复问题与数据</button></div>`;
      } else if (awaitingDecrypt && !authorizationReady) {
        content += `<div class="run-meta">正在展示计算过程，完成后请选择结果的展示方式。</div>`;
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
            <div class="dc-title">结果已准备好</div>
            <div class="dc-body">
              解密查看结果，或保留密文导出。
              ${readyLines.length ? `已生成 ${readyLines.length} 张工作表。` : ""}
              ${skillLines}
            </div>
            <div class="dc-actions">
              <button class="dc-btn primary" data-choice="decrypt">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="dc-ic"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 9.9-1"/></svg>
                解密查看
              </button>
              <button class="dc-btn ghost" data-choice="keep_encrypted">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="dc-ic"><rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                保留密文
              </button>
            </div>
            <div class="dc-hint">保留密文将导出未解密的 Excel。5 分钟未操作自动取消。</div>
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
        const timings = (m.steps || []).map(s => /同态工具计时 · ([\d.]+) 秒 · (\d+) 次/.exec(s.label || "")).filter(Boolean);
        const elapsed = timings.reduce((sum, match) => sum + Number(match[1]), 0);
        const calls = timings.reduce((sum, match) => sum + Number(match[2]), 0);
        const timingText = !timings.length ? "同态工具耗时：未单独计时" : !calls ? "未调用已计时的同态工具" : `同态工具耗时：${elapsed < .001 ? "＜0.001" : elapsed.toFixed(3)} 秒 · ${calls} 次调用`;
        const totalTime = Number.isFinite(m.duration_sec) ? `${m.duration_sec.toFixed(2)} 秒` : "未记录";
        content += `<div class="run-meta" title="总用时采用后台任务耗时，包含模型请求和等待授权，不含界面播放延迟。同态工具耗时统计 synth、window、groupby 调用累计时间（含重试），不代表整个任务均为纯密文计算。">Token：${Number(m.tokens || 0).toLocaleString()} · 总用时：${totalTime} · ${timingText}</div>`;
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
    const copyText = m.role === "user" ? (m.content || "") : (m.summary || (m.status === "failed" ? m.error : "") || "");
    const messageTime = String(m.created_at || "").replace("T", " ").slice(0, 19);
    if (m.role === "user" || m.status === "done") {
    content += `<div class="message-actions"><time>${m.role === "user" ? "发送" : "接收"}时间：${esc(messageTime || "未记录")}</time><button type="button" class="message-copy" data-copy-message title="复制消息" aria-label="复制消息"${copyText ? "" : " disabled"}><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/></svg><span>复制</span></button></div>`;
    }
    content += `</div>`;
    wrap.innerHTML = avatar + content;
    wrap.querySelector('[data-copy-message]')?.addEventListener('click', async (event) => {
      const button = event.currentTarget;
      try {
        let copied = false;
        if (navigator.clipboard?.writeText) {
          try { await navigator.clipboard.writeText(copyText); copied = true; } catch (_) { /* WebView fallback below */ }
        }
        if (!copied) {
          const input = document.createElement("textarea");
          input.value = copyText;
          input.setAttribute("readonly", "");
          input.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
          document.body.appendChild(input);
          try { input.select(); copied = document.execCommand("copy"); }
          finally { input.remove(); button.focus({ preventScroll: true }); }
        }
        if (!copied) throw new Error("系统未允许访问剪贴板");
        button.querySelector("span").textContent = "已复制";
        setTimeout(() => { if (button.isConnected) button.querySelector("span").textContent = "复制"; }, 1500);
      } catch (_) {
        ui.toast("复制失败，请选中消息文字后手动复制", "error");
      }
    });
    wrap.querySelector('[data-restore-request]')?.addEventListener('click', () => restoreFailedRequest(m.id));

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
          await ui.alert("操作失败：" + e.message, { title: "操作未完成", kind: "danger" });
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
        const originalHint = card.querySelector(".dc-hint").textContent;
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
          await ui.alert("提交选择失败：" + e.message, { title: "选择未提交", kind: "danger" });
          card.querySelectorAll(".dc-btn").forEach(x => x.disabled = false);
          card.querySelector(".dc-hint").textContent = originalHint;
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
          await ui.alert("解密失败：" + e.message, { title: "文件未能解密", kind: "danger" });
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

    return { runMetaHtml, renderMessage, typewriter, computeStageIndex, stepHtml, displaySteps };
  }
  global.ClawChatUI = Object.freeze({ create });
}(window));
