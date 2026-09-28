"use strict";
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const dir = path.join(__dirname, '../client/webui/static');
const app = fs.readFileSync(path.join(dir, 'app.js'), 'utf8');
const general = app.slice(app.indexOf('async function renderGeneralTab()'), app.indexOf('async function renderDatabaseTab()'));
const ops = app.slice(app.indexOf('async function renderOpsTab()'), app.indexOf('// ============ 定时任务 Tab'));
assert(!ops.includes('opsStart'));
assert(!ops.includes('opsStop'));
assert(ops.includes('/api/ops/autostart/enable'));
assert(ops.includes('/api/ops/autostart/disable'));
const body = { innerHTML: '' };
const box = { innerHTML: '', querySelector: () => null, querySelectorAll: () => [] };
const esc = s => String(s).replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const context = { window: {}, TABS: { general: { title: '连接 / 计算' } }, esc,
  $: id => id === 'skillMdList' ? box : body,
  api: async () => ({ host_url: '<unsafe>', backend: 'real' }) };
vm.createContext(context);
vm.runInContext(general, context);
vm.runInContext(fs.readFileSync(path.join(dir, 'skills-ui.js'), 'utf8'), context);
(async () => {
  await context.renderGeneralTab();
  assert(body.innerHTML.includes('&lt;unsafe&gt;'));
  assert(body.innerHTML.includes('真实同态加密'));
  assert(!/<input|<select|<button/.test(body.innerHTML));
  context.api = async () => ({ backend: 'stub' });
  await context.renderGeneralTab();
  assert(body.innerHTML.includes('未启用真实同态加密'));
  const skills = context.window.ClawSkillsUI.create({ ...context, state: { skills: {
    skill_md: [{ name: '内置A', slug: 'a' }, { name: '我的B', slug: 'b', is_user: true }]
  } } });
  skills.renderSkillMdList();
  assert(/<details class="builtin-skill-group"\s*>/.test(box.innerHTML));
  assert(box.innerHTML.includes('skill-count">1</span>'));
  assert(box.innerHTML.indexOf('我的B') > box.innerHTML.indexOf('</details>'));
  assert(box.innerHTML.includes('data-view-md="a"'));
  assert(box.innerHTML.includes('data-del-md="b"'));
  box.querySelector = () => ({ open: true });
  skills.renderSkillMdList();
  assert(box.innerHTML.includes('class="builtin-skill-group" open'));
  // One action toggles based on autostart, not the independently running guard.
  const elements = Object.fromEntries(['modalBody', 'opsCards', 'opsAlert', 'opsToggle'].map(id => [id, {
    innerHTML: '', textContent: '', addEventListener(event, fn) { this[event] = fn; }
  }]));
  let installed = false, confirm = true;
  const posts = [];
  const opsContext = {
    TABS: { ops: { title: '自启 / 运维' } }, esc, _opsTimer: null,
    $: id => elements[id], clearInterval() {}, setInterval() { return 1; },
    ui: { confirm: async () => confirm },
    api: async (method, url) => {
      if (method === 'POST') { posts.push(url); installed = url.endsWith('/enable'); return {}; }
      return { autostart: { installed }, supervisor: { running: true }, client: { healthy: true } };
    }
  };
  vm.createContext(opsContext);
  vm.runInContext(ops, opsContext);
  await opsContext.renderOpsTab();
  assert(!elements.modalBody.innerHTML.includes('id="opsEnable"'));
  assert(!elements.modalBody.innerHTML.includes('id="opsDisable"'));
  assert.equal(elements.opsToggle.textContent, '启用');
  assert(elements.opsCards.innerHTML.includes('ops-state is-off'));
  assert(elements.opsCards.innerHTML.includes('ops-state is-on'));
  await elements.opsToggle.click();
  assert.equal(elements.opsToggle.textContent, '停用');
  confirm = false;
  await elements.opsToggle.click();
  assert.equal(posts.length, 1);
  confirm = true;
  await elements.opsToggle.click();
  assert.equal(elements.opsToggle.textContent, '启用');
  assert.deepEqual(posts, ['/api/ops/autostart/enable', '/api/ops/autostart/disable']);
  console.log('Settings checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
