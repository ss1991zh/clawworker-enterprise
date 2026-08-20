/* 企业数据库选择、恢复和重连交互。 */
(function initClawDatabaseUI(global) {
  "use strict";
  function create(deps) {
    const { state, api, $, esc, toast, renderAttachChips, closeFilesModal, returnToLogin } = deps;

  function databaseRequestBeforeAssistant(mid) {
    const messages = state.currentSession?.messages || [];
    const index = messages.findIndex(item => item.id === mid);
    if (index <= 0) return null;
    for (let i = index - 1; i >= 0; i--) {
      const item = messages[i];
      if (item.role === "user") return item.database_source_id ? item : null;
      if (item.role === "assistant") break;
    }
    return null;
  }

  function restoreDatabaseRequest(request, source = null, catalog = null) {
    if (!request?.database_source_id) return false;
    state.pendingCipher = null;
    state.pendingDatabase = {
      id: request.database_source_id,
      name: source?.name || request.database_source_name || request.database_source_id,
      engine: source?.engine || "",
      tableCount: Array.isArray(catalog) ? catalog.length : 0,
      needsRefresh: !Array.isArray(catalog),
    };
    const input = $("input");
    if (input && !input.value.trim()) {
      input.value = request.content || "";
      input.dispatchEvent(new Event("input"));
    }
    renderAttachChips();
    input?.focus();
    return true;
  }

  async function reconnectDatabaseRequest(request, button = null) {
    if (!request?.database_source_id) return;
    const oldText = button?.textContent || "";
    if (button) { button.disabled = true; button.textContent = "正在重新连接…"; }
    try {
      const sources = await api("GET", "/api/data/sources");
      const source = (sources || []).find(item => item.id === request.database_source_id);
      if (!source) {
        toast("原数据库已停用或权限已变更，请重新选择");
        await openDatabasePicker();
        return;
      }
      const catalog = await api(
        "GET", `/api/data/sources/${encodeURIComponent(source.id)}/catalog`,
      );
      if (!catalog?.length) {
        toast("该数据库当前没有可用授权表，请联系管理员");
        return;
      }
      restoreDatabaseRequest(request, source, catalog);
      toast("数据库已重新连接，可以再次发送");
    } catch (e) {
      if (String(e.message || "").includes("正在重新连接")) return;
      toast("重新连接失败：" + e.message);
    } finally {
      if (button && document.body.contains(button)) {
        button.disabled = false;
        button.textContent = oldText;
      }
    }
  }

  function syncDatabaseComposer() {
    const btn = $("databaseBtn");
    const input = $("input");
    if (btn) {
      const active = !!state.pendingDatabase;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-pressed", active ? "true" : "false");
      btn.title = active
        ? `已选择 ${state.pendingDatabase.name}，点击可更换`
        : "选择企业数据库，在当前会话中按权限查询并先加密后分析";
    }
    if (input) input.placeholder = state.pendingDatabase
      ? "直接输入要查询和分析的问题，例如：按地区比较今年销售额和毛利率"
      : "问问看 · 回车发送 · Shift+回车换行";
  }

  function closeDatabasePicker() {
    $("databaseMask")?.classList.remove("open");
    $("input")?.focus();
  }

  async function openDatabasePicker() {
    const mask = $("databaseMask"), body = $("databasePickerBody");
    if (!mask || !body) return;
    mask.classList.add("open");
    body.innerHTML = `<h2>选择企业数据库</h2>
      <p class="sub">选好后回到会话，直接在输入框里说要查什么、怎么算或要输出什么。</p>
      <div class="db-safety-note"><strong>先加密，再分析</strong><span>模型规划查询时只看你获准访问的表结构；查询结果返回本机后立即加密，原始数据行不会进入大模型提示词。</span></div>
      <div class="db-loading">正在读取你的数据库权限…</div>`;
    let sources = [];
    try { sources = await api("GET", "/api/data/sources"); }
    catch (e) {
      const loginExpired = /登录|401|token|unauthorized/i.test(String(e.message || ""));
      body.innerHTML += `<div class="alert-box">读取失败：${esc(e.message)}</div>
        <div class="db-picker-actions"><button class="btn-primary" id="dbSourceReconnect" type="button">${loginExpired ? "重新登录" : "重新连接"}</button></div>`;
      $("dbSourceReconnect")?.addEventListener("click", loginExpired ? returnToLogin : openDatabasePicker);
      return;
    }
    if (!sources.length) {
      body.innerHTML += `<div class="db-empty"><strong>暂时没有可用的企业数据库</strong><span>请联系管理员为当前账号分配数据库、表和字段权限。</span></div>`;
      return;
    }

    let selectedId = state.pendingDatabase?.id || sources[0].id;
    body.innerHTML = `<h2>选择企业数据库</h2>
      <p class="sub">选好后回到会话，直接在输入框里说要查什么、怎么算或要输出什么。</p>
      <div class="db-safety-note"><strong>先加密，再分析</strong><span>模型规划查询时只看你获准访问的表结构；查询结果返回本机后立即加密，原始数据行不会进入大模型提示词。</span></div>
      <div class="db-source-list" id="dbSourceList">${sources.map(s => `
        <button class="db-source-card ${s.id === selectedId ? "selected" : ""}" type="button" data-source-id="${esc(s.id)}">
          <span class="db-source-icon">DB</span><span class="db-source-copy"><strong>${esc(s.name)}</strong><small>${esc(s.engine || "企业数据库")}</small></span><span class="db-source-check">✓</span>
        </button>`).join("")}</div>
      <div class="db-picker-catalog" id="dbPickerCatalog"><div class="db-loading">正在读取授权表…</div></div>
      <div class="db-picker-actions"><button class="btn-ghost" id="dbPickerCancel">取消</button><button class="btn-ghost" id="dbPickerRefresh">刷新连接</button><button class="btn-primary" id="dbPickerUse" disabled>用于本次会话</button></div>`;

    let selectedCatalog = [];
    const loadCatalog = async () => {
      $("dbPickerUse").disabled = true;
      $("dbPickerCatalog").innerHTML = '<div class="db-loading">正在读取授权表…</div>';
      try {
        selectedCatalog = await api("GET", `/api/data/sources/${encodeURIComponent(selectedId)}/catalog`);
        const count = selectedCatalog.length;
        $("dbPickerCatalog").innerHTML = `<div class="db-picker-catalog__head"><strong>当前可用范围</strong><span>${count} 张表，仅显示已授权字段</span></div>${selectedCatalog.map(t => `
          <div class="db-table compact"><div class="db-table__head"><strong>${esc(t.schema)}.${esc(t.table)}</strong><span>${t.columns.length} 个字段${t.row_restricted ? " · 行范围受限" : ""}</span></div><div class="db-cols">${t.columns.map(c => `<span class="db-col ${c.mask ? "masked" : ""}">${esc(c.name)}${c.mask ? " · 已脱敏" : ""}</span>`).join("")}</div></div>`).join("") || '<div class="db-empty">没有可用于查询的表</div>'}`;
        $("dbPickerUse").disabled = !count;
      } catch (e) {
        $("dbPickerCatalog").innerHTML = `<div class="alert-box">读取授权范围失败：${esc(e.message)}</div>
          <button class="btn-ghost" id="dbCatalogRetry" type="button">重新读取授权范围</button>`;
        $("dbCatalogRetry")?.addEventListener("click", loadCatalog);
      }
    };
    $("dbSourceList").querySelectorAll("[data-source-id]").forEach(card => card.addEventListener("click", async () => {
      selectedId = card.dataset.sourceId;
      $("dbSourceList").querySelectorAll("[data-source-id]").forEach(c => c.classList.toggle("selected", c === card));
      await loadCatalog();
    }));
    $("dbPickerCancel").addEventListener("click", closeDatabasePicker);
    $("dbPickerRefresh").addEventListener("click", openDatabasePicker);
    $("dbPickerUse").addEventListener("click", () => {
      const source = sources.find(s => s.id === selectedId);
      if (!source || !selectedCatalog.length) return;
      if (state.pendingCipher) {
        state.pendingCipher = null;
        toast("已切换为企业数据库数据源");
      }
      state.pendingDatabase = {
        id: source.id, name: source.name, engine: source.engine || "", tableCount: selectedCatalog.length,
      };
      renderAttachChips();
      closeDatabasePicker();
    });
    await loadCatalog();
  }

    return {
      databaseRequestBeforeAssistant, restoreDatabaseRequest, reconnectDatabaseRequest,
      syncDatabaseComposer, closeDatabasePicker, openDatabasePicker,
    };
  }
  global.ClawDatabaseUI = Object.freeze({ create });
}(window));
