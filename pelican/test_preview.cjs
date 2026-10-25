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
    getAttribute(name) {return this[name];},
    children: [], replaceChildren() {this.children=[];}, append(child) {this.children.push(child);},
    classList: {toggle() {}},
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
  document: {getElementById: element, addEventListener() {}, createElement: () => ({})},
  window: {innerWidth: 800, innerHeight: 600, addEventListener() {}, confirm: () => true},
  setTimeout, clearTimeout,requestAnimationFrame: () => 1,
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
  // The benchmark page displays speed while generating, not only after finish.
  vm.runInContext(`records=[{id:'live',status:'running',progress_phase:'reasoning',characters:0,
    reasoning_characters:100,live_tps:23.5,live_estimated:true,elapsed:4}];selected='live';renderDetail()`, context);
  assert.match(element('detail-speed').textContent, /≈ 23.50 t\/s.*估算/);
  assert.match(element('detail-tokens').textContent, /思考中.*100/);
  assert.equal(element('detail-elapsed').textContent, '已用 4.0 秒');
  // Metrics-only updates must render even when the artwork/detail signature is unchanged.
  vm.runInContext('records[0].live_tps=18;records[0].elapsed=5;renderDetail()', context);
  assert.match(element('detail-speed').textContent, /18.00 t\/s/);
  assert.equal(element('detail-elapsed').textContent, '已用 5.0 秒');
  vm.runInContext(`records[0]={id:'live',status:'complete',tps:42,output_tokens:210,elapsed:5};renderDetail()`, context);
  assert.equal(element('detail-speed').textContent, '42.00 t/s');
  assert.equal(element('detail-tokens').textContent, '输出 210 token');
  const artwork = element('art').src;
  vm.runInContext('records[0].tps=43;renderDetail()', context);
  assert.equal(element('detail-speed').textContent, '43.00 t/s');
  assert.equal(element('art').src, artwork);
  vm.runInContext(`records[0]={id:'live',status:'running',progress_phase:'waiting',characters:0,elapsed:8};renderDetail()`, context);
  assert.match(element('detail-speed').textContent, /等待首字/);
  assert.equal(element('detail-elapsed').textContent, '已用 8.0 秒');
  vm.runInContext(`records[0].status='failed';renderDetail()`, context);
  assert.match(element('detail-speed').textContent, /未报告/);
  vm.runInContext("records=[];selected='';renderDetail()", context);
  assert.equal(element('run-metrics').hidden, true);
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
    {reasoning: '低', max_tokens: '49152', timeout: '420', prompt_mode: 'fixed', custom_prompt: ''},
    {reasoning: '低', max_tokens: '65536', timeout: '420', prompt_mode: 'fixed', custom_prompt: ''},
  ]);
  assert.equal(element('save-status').textContent, '已保存');
  // Edits in the main app must propagate back into the embedded controls.
  vm.runInContext("context.pelican_tokens='8192';context.pelican_reasoning='高';context.pelican_timeout='900';updateSelection()", context);
  assert.equal(element('limit').value, '8192');
  assert.equal(element('timeout').value, '900');
  assert.equal(element('reasoning').dataset.value, '高');
  // Custom text is saved verbatim; switching back preserves the draft.
  vm.runInContext("$('custom-prompt').value='画一个旋转的 SVG 星球';$('custom-mode').onclick()", context);
  await vm.runInContext('saveOptions()', context);
  assert.equal(writes[2].prompt_mode, 'custom');
  assert.equal(writes[2].custom_prompt, '画一个旋转的 SVG 星球');
  assert.equal(element('custom-editor').hidden, false);
  vm.runInContext("$('fixed-prompt').onclick()", context);
  await vm.runInContext('saveOptions()', context);
  assert.equal(writes[3].prompt_mode, 'fixed');
  assert.equal(writes[3].custom_prompt, '画一个旋转的 SVG 星球');
  assert.equal(element('custom-editor').hidden, true);
  // Invalid partial input must not send a save or claim success.
  vm.runInContext("$('limit').value='';changed()", context);
  await assert.rejects(vm.runInContext('saveOptions()', context));
  assert.equal(writes.length, 4);
  assert.equal(element('save-status').textContent, '保存失败');
  // Named templates persist separately from autosave, and using one only fills
  // the editor. A delayed old context response must not clobber a mutation.
  vm.runInContext("$('limit').value='32768';changed()", context);
  await vm.runInContext('saveOptions()', context);
  const originalFetch = context.fetch;
  const templateWrites = [];
  let savedTemplates = {};
  context.fetch = async (url, request) => {
    assert.equal(url, 'api/settings');
    const payload = JSON.parse(request.body);
    if (!payload.template_action) return originalFetch(url, request);
    templateWrites.push(payload);
    const {template_action:action,template_name:name,template_target:target} = payload;
    if(action==='save')savedTemplates[name]=payload.template_prompt;
    if(action==='rename'){savedTemplates[target]=savedTemplates[name];delete savedTemplates[name];}
    if(action==='delete')delete savedTemplates[name];
    return {ok:true,json:async()=>({saved:true,templates:{...savedTemplates}})};
  };
  vm.runInContext("$('template-name').value='旋转星球'", context);
  await vm.runInContext("changeTemplate('save')", context);
  assert.equal(savedTemplates['旋转星球'], '画一个旋转的 SVG 星球');
  assert.equal(element('template-list').value, '旋转星球');
  vm.runInContext("$('custom-prompt').value='别的草稿';useTemplate()", context);
  assert.equal(element('custom-prompt').value, '画一个旋转的 SVG 星球');
  assert.equal(element('custom-editor').hidden, false);
  vm.runInContext("$('template-name').value='星球二'", context);
  await vm.runInContext("changeTemplate('rename')", context);
  assert.deepEqual(Object.keys(savedTemplates), ['星球二']);
  await vm.runInContext("changeTemplate('delete')", context);
  assert.deepEqual(savedTemplates, {});
  assert.deepEqual(templateWrites.map(p=>p.template_action), ['save','rename','delete']);
  context.fetch = originalFetch;
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
  // Browsing a long history must release old frames, cap visible thumbnails,
  // and reuse existing frames when nothing changes.
  let scroll = 0;
  const areas = Array.from({length: 40}, () => ({
    children: [], get firstChild() {return this.children[0];},
    append(child) {this.children.push(child);},
    replaceChildren() {this.children = [];},
  }));
  context.document.createElement = tag => ({
    tag, children: [], setAttribute() {}, append(child) {this.children.push(child);},
  });
  element('records').getBoundingClientRect = () => ({top: 0, bottom: 1000});
  context.performanceCards = areas.map((area, i) => ({
    querySelector: () => area,
    getBoundingClientRect: () => ({top: i * 100 - scroll, bottom: (i + 1) * 100 - scroll}),
  }));
  vm.runInContext(`cards.clear();records=performanceCards.map((card,i)=>{
    cards.set(String(i),card);return {id:String(i),model:'mock',status:'complete'};
  });syncThumbnails()`, context);
  assert.equal(areas.filter(area => area.firstChild).length, 4);
  const firstThumb = areas[0].firstChild;
  vm.runInContext('syncThumbnails()', context);
  assert.equal(areas[0].firstChild, firstThumb);
  scroll = 3200;
  vm.runInContext('syncThumbnails()', context);
  assert.equal(areas[0].firstChild, undefined);
  assert.equal(areas.filter(area => area.firstChild).length, 4);
  assert.equal(areas[32].firstChild.children[0].src, 'thumbnail/32');
  element('art').src = 'artwork/0';
  vm.runInContext('setPageActive(false);syncThumbnails()', context);
  assert.equal(element('art').src, 'about:blank');
  assert.equal(areas.filter(area => area.firstChild).length, 0);
  vm.runInContext('setPageActive(true);syncThumbnails()', context);
  assert.equal(areas.filter(area => area.firstChild).length, 4);
  vm.runInContext("selected='0';rawMode=false;renderDetail()", context);
  assert.equal(element('art').src, 'artwork/0');
  // A running record's raw view needs no fetch and must unload the old artwork.
  vm.runInContext("records[0].status='running';rawMode=true;renderDetail()", context);
  assert.equal(element('art').src, 'about:blank');
  let contextReads = 0, runsReads = 0;
  context.fetch = async url => {
    if (url === 'api/context') {
      contextReads++;
      return {ok: true, json: async () => ({active: false})};
    }
    runsReads++;
    throw Error('Hidden page must not fetch runs');
  };
  await vm.runInContext('poll(false)', context);
  assert.equal(contextReads, 1);
  assert.equal(runsReads, 0);
  // Manual refreshes during another poll are serialized and stay scheduled.
  await vm.runInContext('Promise.all([poll(false),poll(false)])', context);
  assert.equal(contextReads, 3);
  assert.equal(vm.runInContext('Boolean(pollTimer)', context), true);
  vm.runInContext('clearTimeout(pollTimer)', context);
  console.log('PASS: autosave, context menus, bounded thumbnails, hidden/raw unloading and serial polling');
}
main().catch(error => {console.error(error); process.exitCode = 1;});
