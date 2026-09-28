/* 连接设置中的密钥、审计和账户页面。 */
(function initClawSettingsUI(global) {
  "use strict";
  function create({ api, $, esc, ui, title }) {
  async function renderKeysTab() {
    const k = await api("GET", "/api/keys");
    const keyCard = (name, code, present, zone, input, hint) => `
      <details class="key-file-card" ${present ? "" : "open"}>
        <summary><span class="key-file-name">${name} <span class="mono-tag">${code}</span></span>
          <span class="badge ${present ? "ok" : "warn"}">${present ? "已导入" : "未导入"}</span>
          <span class="key-file-action"><span class="key-file-closed">${present ? "更换" : "导入"}</span><span class="key-file-open">收起</span></span>
        </summary>
        <div class="key-upload-body">
          <div class="sk-drop key-drop-compact" id="${zone}">
            <span>拖入文件，或 <button type="button" class="sk-pick" data-pick="${code}">选择文件</button></span>
            <input type="file" id="${input}" hidden>
          </div>
          <p class="key-upload-hint">${hint}</p>
        </div>
      </details>`;
    $("modalBody").innerHTML = `
      <h2>${title("keys")}</h2>
      <p class="sub">私钥和计算字典仅保存在本机，用户授权由管理端签发。</p>
      <div class="key-files">
        ${keyCard("私钥", "sk", k.sk_present, "dropSk", "skFile", "用于授权后的本机解密。更换前请确认与计算字典配套。")}
        ${keyCard("计算字典", "evk · dictf", k.evk_present, "dropEvk", "evkFile", "用于密文计算。仅在首次配置或更新字典时导入，大文件需要稍候。")}
      </div>
      <section class="key-setting-card">
        <div class="key-setting-row"><div class="key-setting-copy"><h3>用户授权 <span class="badge ${k.user_auth_present ? "ok" : "warn"}">${k.user_auth_present ? "已同步" : "未获取"}</span></h3><p>由管理端统一签发，无需手动上传。</p></div>
          <button class="btn-ghost btn-sm" id="fetchAuthBtn">${k.user_auth_present ? "重新同步" : "获取授权"}</button></div>
      </section>
      <section class="key-setting-card key-check-card">
        <div class="key-setting-row"><div class="key-setting-copy"><h3>计算能力检查</h3><p>检查当前环境的计算能力，导入或更新后建议运行一次。</p></div>
          <button class="btn-ghost btn-sm" id="keycheckBtn" ${k.sk_present && k.evk_present ? "" : "disabled"}>开始检查</button></div>
        ${k.sk_present && k.evk_present ? "" : '<p class="hint">请先导入私钥和计算字典。</p>'}
        <div id="keycheckResult" aria-live="polite"></div>
      </section>
      <div id="kStatus" role="status"></div>
    `;
    bindKeyDrop("dropSk", "skFile", "私钥", "/api/keys/sk");
    bindKeyDrop("dropEvk", "evkFile", "计算字典", "/api/keys/evk");

    $("keycheckBtn").addEventListener("click", async () => {
      const btn = $("keycheckBtn");
      const resultBox = $("keycheckResult");
      btn.disabled = true; btn.textContent = "检查中…";
      resultBox.innerHTML = '<p class="key-check-pending" role="status">正在检查，请稍候…</p>';
      try {
        const rep = await api("GET", "/api/keycheck?quick=true");
        resultBox.innerHTML = renderKeycheckResult(rep);
      } catch (e) {
        resultBox.innerHTML = `<div class="alert-box">检查失败：${esc(e.message)}</div>`;
      } finally {
        btn.disabled = false; btn.textContent = "重新检查";
      }
    });

    $("fetchAuthBtn").addEventListener("click", async () => {
      const statusBox = $("kStatus"), btn = $("fetchAuthBtn");
      btn.disabled = true;
      statusBox.innerHTML = '<div class="alert-box info">正在同步用户授权…</div>';
      try {
        const res = await api("POST", "/api/keys/fetch_auth");
        statusBox.innerHTML = '<div class="alert-box success">用户授权已同步</div>';
        setTimeout(() => { if ($("kStatus") === statusBox) renderKeysTab(); }, 600);
      } catch (e) {
        statusBox.innerHTML = `<div class="alert-box">同步失败:${esc(e.message)}</div>`;
      } finally { btn.disabled = false; }
    });
  }

  function renderKeycheckResult(rep) {
    const suites = rep.suites || [];
    const descriptions = [
      [/数组级/, "基础数值计算", "检查加减乘除等数值运算"],
      [/表级/, "表格计算", "检查表格中的列计算与汇总"],
      [/分组/, "分类汇总", "检查按类别分组后的统计计算"],
      [/窗口|多条件/, "连续数据与条件统计", "检查前后期比较、滚动统计和多条件计算"],
      [/规模/, "较大数据量测试", "检查较多数据下的计算结果与速度"],
      [/模型级/, "预测模型", "检查本次选取的预测模型"],
      [/深度/, "连续运算稳定性", "检查多次连续计算后的精度"],
      [/有效域/, "数值范围检查", "检查不同数值范围下的计算误差"],
      [/规划器/, "计算方案安全检查", "检查是否拦截不合规方案、标记需要授权解密的步骤"],
    ];
    const info = s => descriptions.find(([pattern]) => pattern.test(s.name || "")) || [null, "其他检查", "检查当前计算环境"];
    const explain = s => {
      const detail = String(s.detail || "");
      const count = detail.match(/(\d+)\/(\d+)/);
      let text = info(s)[2] + "。";
      if (count) text += `本次 ${count[2]} 项中 ${count[1]} 项通过。`;
      if (count && Number(count[1]) < Number(count[2]) && s.ok) {
        text += "部分项目属于已知限制，不计入本次通过判定；不代表所有操作均可用。";
      } else if (!s.ok) text += "本项未通过，建议重新检查配套文件；仍失败请联系管理员。";
      else if (!count) text += "本次检查通过。";
      return text;
    };
    const limited = suites.some(s => { const m = String(s.detail || "").match(/(\d+)\/(\d+)/); return s.ok && m && +m[1] < +m[2]; });
    const head = `<div class="key-check-summary"><span class="badge ${rep.ok ? "ok" : "warn"}">${rep.ok ? "检查通过" : "需要处理"}</span><span>${rep.ok ? (limited ? "按当前判定标准通过，部分操作存在限制" : "本次计算检查通过") : "部分检查未通过，请查看详情"}</span></div>`;
    const lic = rep.license || {};
    const rows = suites.map(s => `<div class="key-check-item">
      <span class="badge ${s.ok ? "ok" : "warn"}">${s.ok ? "通过" : "未通过"}</span>
      <div><div class="key-check-name">${esc(info(s)[1])}</div><p>${esc(explain(s))}</p></div>
    </div>`).join("");
    const tier = rep.scale_tier || {};
    const size = Number(tier.max_smooth_n);
    const scale = Number.isFinite(size) && size > 0 ? `<p class="key-check-note">本次测试中，分类汇总完成了 ${size.toLocaleString()} 行数据的处理。这是当前测试结果，不代表所有任务都能达到同样速度或规模。</p>` : "";
    const raw = suites.map(s => `${s.name || ""}：${s.detail || ""}`).join("\n") + (lic.message ? "\n\n授权状态：" + lic.message : "") + (rep.capability_brief ? "\n\n" + rep.capability_brief : "");
    const technical = `<details class="key-technical"><summary>技术记录（供管理员排查）</summary><pre class="keycheck-brief">${esc(raw)}</pre></details>`;
    return head + `<details class="key-check-details"><summary>查看检查详情（${suites.length} 项）</summary>${rows}${scale}${technical}</details>`;
  }

async function renderAuditTab() {
    const body = $("modalBody");
    body.innerHTML = '<h2>可信审计</h2><p id="auditLoading" class="sub">正在读取记录…</p>';
    const loading = $("auditLoading");
    let data;
    try { data = await api("GET", "/api/audit?limit=200"); }
    catch (e) { if ($("auditLoading") === loading) body.innerHTML = '<h2>可信审计</h2><div class="alert-box">读取失败：' + esc(e.message) + '</div>'; return; }
    if ($("auditLoading") !== loading) return;
    const s = data.summary || {}, chain = data.chain || {};
    const events = (data.events || []).slice().reverse();
    const categories = [["all","全部"],["llm_exposure","模型访问"],["cipher_compute","密文计算"],["decrypt_auth","解密授权"],["document_exposure","文档发送"],["llm_bypass","本地复用"],["other","其他"]];
    const known = categories.slice(1,-1).map(c => c[0]);
    const matches = (e, type) => type === "all" || (type === "other" ? !known.includes(e.type) : e.type === type);
    const stats = [["模型访问",s.llm_exposures],["密文计算",s.cipher_computations],["解密授权",s.decrypt_authorizations],["文档发送",s.document_exposures]];
    body.innerHTML = `
      <div class="audit-heading"><h2>可信审计</h2><button class="btn-ghost btn-sm" id="auditExportBtn">导出合规报告</button></div>
      <p class="sub">查看数据使用、计算及解密授权记录。</p>
      <div class="audit-stats">${stats.map(([name,n])=>`<div><span>${name}</span><strong>${Number(n)||0}</strong></div>`).join("")}</div>
      <div class="audit-integrity ${chain.ok ? "is-ok" : "is-warning"}">
        <strong>${chain.ok ? "审计链完整" : "审计链需要复核"}</strong>
        <p>${chain.ok ? "记录已通过完整性校验。" : "部分记录未通过完整性校验，请导出报告交由管理员排查。"}
        ${Number(s.plaintext_breaches) ? `发现 ${Number(s.plaintext_breaches)} 次疑似明文外发。` : "记录中未发现疑似结构化明文外发。"}</p>
        ${chain.ok ? "" : `<details><summary>查看异常原因</summary><p>${esc(chain.reason || "原因未记录")}</p></details>`}
      </div>
      <div class="audit-records-head"><h3>事件记录</h3><span>最近 ${events.length} 条 · 总记录 ${Number(s.total_events)||events.length} 条</span></div>
      <div id="auditFilters" class="audit-filters" role="group" aria-label="事件分类">
        ${categories.map(([type,label])=>`<button class="audit-filter" data-audit-type="${type}" aria-pressed="${type==="all"}">${label} <span>${events.filter(e=>matches(e,type)).length}</span></button>`).join("")}
      </div>
      <div id="auditEvents"></div>
      <div class="audit-pagination"><span id="auditPageInfo" role="status"></span><div><button class="btn-ghost btn-sm" id="auditPrev">上一页</button><button class="btn-ghost btn-sm" id="auditNext">下一页</button></div></div>
      <p class="audit-scope">分类和分页针对最近 200 条记录；报告导出不受当前分类和页码影响。</p>`;
    let category="all", page=1;
    const filters=$("auditFilters");
    const renderPage=()=>{
      const filtered=events.filter(e=>matches(e,category)), pages=Math.max(1,Math.ceil(filtered.length/8));
      page=Math.max(1,Math.min(page,pages));
      $("auditEvents").innerHTML=filtered.slice((page-1)*8,page*8).map(e=>{
        const name=categories.find(c=>c[0]===e.type)?.[1]||"其他事件";
        const detail=renderAuditEvent(e)||`<p>${esc(e.detail || e.type || "暂无说明")}</p>`;
        const warning=(e.type==="llm_exposure" && !e.no_plaintext)||(e.type==="cipher_compute" && !e.no_structured_plaintext_to_llm);
        const status=e.type==="decrypt_auth" ? ({granted:"已授权",denied:"已拒绝",keep_encrypted:"保留密文"})[e.decision]||"已取消" : warning ? "需要复核" : "已记录";
        return `<details class="audit-event"><summary><span>${name}</span><span class="badge ${warning?"warn":""}">${status}</span><time>${esc((e.ts||"").replace("T"," ").slice(0,19))}</time></summary><div class="audit-event-detail">${detail}</div></details>`;
      }).join("")||'<p class="audit-empty">此分类暂无记录</p>';
      $("auditPageInfo").textContent=`第 ${page} / ${pages} 页 · ${filtered.length} 条`;
      $("auditPrev").disabled=page===1; $("auditNext").disabled=page===pages;
      filters.querySelectorAll("[data-audit-type]").forEach(btn=>btn.setAttribute("aria-pressed",String(btn.dataset.auditType===category)));
    };
    filters.querySelectorAll("[data-audit-type]").forEach(btn=>btn.addEventListener("click",()=>{category=btn.dataset.auditType;page=1;renderPage();}));
    $("auditPrev").addEventListener("click",()=>{page--;renderPage();});
    $("auditNext").addEventListener("click",()=>{page++;renderPage();});
    $("auditExportBtn").addEventListener("click",()=>{
      const a=document.createElement("a"); a.href="/api/audit/export";a.download="";
      document.body.appendChild(a);a.click();a.remove();
    });
    renderPage();
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
    if (e.type === "llm_bypass") {
      return `<div class="key-row"><div class="key-meta">
        <span class="badge ok">未调用模型</span>
        <span class="mono-tag">本地</span> <span class="af-s">${ts} · 复用已固化的本地分析代码</span>
      </div></div>`;
    }
    if (e.type === "cipher_compute") {
      const ok = e.no_structured_plaintext_to_llm;
      const fp = String(e.input_fingerprint || "");
      const shortFp = fp ? `${fp.slice(0, 12)}…${fp.slice(-8)}` : "无指纹";
      return `<div class="key-row"><div class="key-meta">
        <span class="badge ${ok ? "ok" : "warn"}">${ok ? "密文计算" : "待复核"}</span>
        <span class="mono-tag">证据</span> <span class="af-s">${ts} · ${esc(e.engine || "ZFHE")} · 输入 ${esc(shortFp)} · ${(e.skill_calls || []).map(item => esc(item)).join("、")}</span>
      </div></div>`;
    }
    return "";
  }

  function bindKeyDrop(zoneId, inputId, label, endpoint) {
    const zone = $(zoneId), inp = $(inputId);
    if (!zone || !inp) return;
    let uploading = false;
    async function upload(file) {
      if (uploading) return;
      uploading = true; inp.disabled = true;
      const statusBox = $("kStatus");
      const fd = new FormData(); fd.append("file", file);
      statusBox.innerHTML = `<div class="alert-box info">正在导入${esc(label)}…</div>`;
      try {
        const res = await api("POST", endpoint, fd, true);
        statusBox.innerHTML = `<div class="alert-box success">${esc(label)}已导入</div>`;
        setTimeout(() => { if ($("kStatus") === statusBox) renderKeysTab(); }, 600);
      } catch (e) {
        statusBox.innerHTML = `<div class="alert-box">${esc(label)}导入失败：${esc(e.message)}</div>`;
      } finally { uploading = false; inp.disabled = false; inp.value = ""; }
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
      if (!await ui.confirm("当前会话会保留，下次登录后仍可继续查看。", { title: "退出登录？", confirmText: "退出" })) return;
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
