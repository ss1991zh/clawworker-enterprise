/* Skill 上传、查看和自定义指标管理。 */
(function initClawSkillsUI(global) {
  "use strict";
  function create({ state, api, $, esc, title }) {
  async function renderSkillsTab() {
    $("modalBody").innerHTML = `
      <h2>${title("skills")}</h2>
      <p class="sub">SKILL.md 技能教 AI 写密态计算代码 · 拖入技能包(SKILL.md + INDEX / docs / examples)即可添加</p>
      <div id="skillsAlert"></div>

      <div id="skillMdList"><div class="alert-box info">加载中…</div></div>

      <h3 class="section-heading">添加技能</h3>
      <div class="sk-drop" id="skillDropZone" tabindex="0">
        <div class="sk-drop__t">拖入技能<strong>文件夹</strong> / <strong>.zip</strong> / <strong>SKILL.md</strong></div>
        <div class="sk-drop__s" id="skillDropHint">支持多文件嵌套包(SKILL.md + INDEX.md + docs/ + examples/)· 点击选文件夹</div>
        <input type="file" id="skillDirInput" webkitdirectory directory multiple hidden>
        <input type="file" id="skillFileInput" accept=".md,.zip" hidden>
      </div>
      <div id="skillUpStatus" class="status-spaced"></div>
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
          <div class="d text-wrap">${esc((s.description || '').slice(0, 160))}${(s.description||'').length>160?'…':''}</div>
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
      <div class="modal modal--single preview-modal">
        <button class="modal__close" id="smClose">
          <svg class="ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
        </button>
        <div class="modal__body">
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

    return { renderSkillsTab, loadSkills, bindSkillDrop, walkEntry, uploadSkillFiles, renderSkillMdList, showSkillMd, renderBuiltinSkills, renderCustomSkills };
  }
  global.ClawSkillsUI = Object.freeze({ create });
}(window));
