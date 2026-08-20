/* 连接设置中的密钥、审计和账户页面。 */
(function initClawSettingsUI(global) {
  "use strict";
  function create({ api, $, esc, title }) {
  async function renderKeysTab() {
    const k = await api("GET", "/api/keys");
    const sizeKb = (p) => p ? "(已沙盒化)" : "—";
    $("modalBody").innerHTML = `
      <h2>${title("keys")}</h2>
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
      <p class="sub">证明：结构化数值先加密 · 文档正文需明确确认 · 解密均经本机授权可追溯</p>
      <div class="alert-box ${s.zero_plaintext_holds ? "success" : ""}">${esc(s.statement || "暂无审计记录 —— 跑一次分析后这里会有台账。")}</div>
      <div class="key-row"><div class="key-meta af-s">
        LLM 暴露事件 <strong>${s.llm_exposures || 0}</strong> ·
        文档正文发送 <strong>${s.document_exposures || 0}</strong> ·
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
    if (e.type === "document_exposure") {
      const names = (e.documents || []).map(d => d.name).slice(0, 4).join("、");
      return `<div class="key-row"><div class="key-meta">
        <span class="badge warn">已确认发送</span>
        <span class="mono-tag">文档</span> <span class="af-s">${ts} · ${esc(names)} · ${e.total_chars || 0} 字</span>
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
      <h2>${title("account")}</h2>
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

    return { renderKeysTab, renderKeycheckResult, renderAuditTab, renderAuditEvent, bindKeyDrop, renderAccountTab };
  }
  global.ClawSettingsUI = Object.freeze({ create });
}(window));
