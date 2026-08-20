/* 定时任务向导、概览、待运行、补跑和历史界面。 */
(function initClawTasksUI(global) {
  "use strict";
  function create({
    state, api, $, esc, FOLDER_ICON_SVG, title, loadSessions, selectSession,
    setSessionView, closeModal, renderSessionList, updateSessionChrome,
    syncFooter, renderMessage, showWelcome,
  }) {
  const _syncFooter = syncFooter;
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
      <h2>${title("tasks")}</h2>
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

    return {
      bindWizard, closeTaskWizard, openTaskWizard, renderWizard, wizBack, wizNext,
      submitWizard, toast, renderTasksTab, loadTasksData, scheduleText,
      renderPendingList, renderTaskList, renderTaskHistory, refreshTaskCount,
      refreshTasksBadge, closeTaskPanel, openTaskPanel, openMissedPanel,
      openOverview, renderOverviewList, renderTaskPanel, renderTPStatus,
      renderTPPending, renderTPMissed, renderTPHistory, renderTPEdit,
      openCreateTaskForm, renderCreateTaskForm, submitCreateTask,
    };
  }
  global.ClawTasksUI = Object.freeze({ create });
}(window));
