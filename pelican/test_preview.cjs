// Local regression for the page's actual autosave handlers; no browser/API calls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const page = fs.readFileSync(require('node:path').join(__dirname, 'preview.html'), 'utf8');
const script = page.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\npoll\(\);\s*$/, '');
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value: '', dataset: {}, textContent: '', addEventListener() {},
    checkValidity() { return Number.isInteger(Number(this.value)) && Number(this.value) >= (id === 'limit' ? 256 : 1); },
  });
  return elements.get(id);
}
const writes = [];
const opened = [];
const menuButtons = ['explorer', 'browser', 'raw', 'remove'].map(action => ({
  dataset: {action}, disabled: false, focus() {context.document.activeElement = this;},
}));
const menu = element('record-menu');
menu.style = {};menu.hidden = true;menu.offsetWidth = 220;menu.offsetHeight = 180;
menu.querySelector = selector => selector === 'button:not(:disabled)'
  ? menuButtons.find(button => !button.disabled)
  : menuButtons.find(button => selector.includes('"' + button.dataset.action + '"'));
menu.querySelectorAll = () => menuButtons.filter(button => !button.disabled);
let release;
const context = vm.createContext({
  document: {getElementById: element, addEventListener() {}},
  window: {innerWidth: 800, innerHeight: 600, addEventListener() {}},
  setTimeout, clearTimeout,
  fetch: async (url, request) => {
    if (url.endsWith('/open')) {
      opened.push({url, ...JSON.parse(request.body)});
      return {ok: true, json: async () => ({opened: true})};
    }
    assert.equal(url, 'api/settings');
    writes.push(JSON.parse(request.body));
    if (writes.length === 1) await new Promise(resolve => {release = resolve;});
    return {ok: true, json: async () => ({saved: true})};
  },
});
vm.runInContext(script, context);
async function main() {
  // Every test uses the shared model list in the main app.
  assert.equal(page.includes('id="manual"'), false);
  vm.runInContext("context.providers=[{id:'current',name:'MOCK'}];context.selected_provider='current';context.selected_models=['selected-model'];updateSelection()", context);
  assert.equal(element('generate').disabled, false);
  assert.equal(vm.runInContext('models().join(",")', context), 'selected-model');
  vm.runInContext("context.selected_models=['manual-one','manual-two'];updateSelection()", context);
  assert.equal(vm.runInContext('models().join(",")', context), 'manual-one,manual-two');
  assert.match(element('selection').textContent, /左侧所选 2 个/);
  assert.equal(element('generate').textContent, '生成 2 个模型');
  vm.runInContext("context.selected_models=[];updateSelection()", context);
  assert.equal(element('generate').disabled, true);
  vm.runInContext("updateSelection();$('limit').value='49152';$('timeout').value='420';$('reasoning').onclick()", context);
  const pending = vm.runInContext('saveOptions()', context);
  // An additional edit during an in-flight save must be saved afterwards.
  vm.runInContext("$('limit').value='65536';changed();updateSelection()", context);
  assert.equal(element('limit').value, '65536');
  release();
  await pending;
  assert.deepEqual(writes, [
    {reasoning: '低', max_tokens: '49152', timeout: '420'},
    {reasoning: '低', max_tokens: '65536', timeout: '420'},
  ]);
  assert.equal(element('save-status').textContent, '已保存');
  // Edits in the main app must propagate back into the embedded controls.
  vm.runInContext("context.pelican_tokens='8192';context.pelican_reasoning='高';context.pelican_timeout='900';updateSelection()", context);
  assert.equal(element('limit').value, '8192');
  assert.equal(element('timeout').value, '900');
  assert.equal(element('reasoning').dataset.value, '高');
  // Invalid partial input must not send a save or claim success.
  vm.runInContext("$('limit').value='';changed()", context);
  await assert.rejects(vm.runInContext('saveOptions()', context));
  assert.equal(writes.length, 2);
  assert.equal(element('save-status').textContent, '保存失败');
  // Right-click menus stay inside the viewport and address the clicked card.
  vm.runInContext(`records=[{id:'abc',status:'complete'},{id:'running',status:'running'}];
    showRecordMenu({preventDefault(){},stopPropagation(){},clientX:799,clientY:599},records[0],null)`, context);
  assert.equal(menu.hidden, false);
  assert.equal(menu.style.left, '572px');
  assert.equal(menu.style.top, '412px');
  assert.equal(menuButtons[1].disabled, false);
  await vm.runInContext("recordAction('browser')", context);
  assert.deepEqual(opened, [{url: 'api/runs/abc/open', action: 'browser'}]);
  assert.equal(menu.hidden, true);
  vm.runInContext(`showRecordMenu({preventDefault(){},stopPropagation(){},clientX:0,clientY:0},records[1],null)`, context);
  assert.equal(menuButtons[1].disabled, true);
  assert.equal(menuButtons[2].disabled, true);
  assert.equal(menuButtons[0].disabled, false);
  console.log('PASS: autosave, settings sync, context menu targeting, placement and running state');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
