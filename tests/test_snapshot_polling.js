/* 验证首页自动同步仅使用模拟服务，不采集真实站点。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const handlers = {};
global.document = {hidden:false, addEventListener(name, fn){handlers[name] = fn;}};
global.localStorage = {getItem(){return null;}};
global.window = {};
const html = fs.readFileSync(path.join(__dirname, '..', '中转站比价.html'), 'utf8');
const script = html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/)[1];
new Function(script + `
  let renders = 0, headers = 0;
  afterDataLoaded = () => { renders++; };
  updateHeaderTips = () => { headers++; };
  window.snapshotTest = {state, checkSnapshotUpdates, applyIndex,
    get renders(){return renders;}, get headers(){return headers;}};
`)();
const t = window.snapshotTest;
t.state.mode = 'server';
t.state.server = {task:{logs:['kept']}, schedule:{presets:['08:00']}};
t.state.meta.generated_at = 'old';
let requests = [];
let responses = [];
global.fetch = async url => {
  requests.push(url);
  const value = responses.shift();
  if (value instanceof Error) throw value;
  return {status:200, json:async () => await value};
};
const status = generated_at => ({ok:true, generated_at,
  task:{running:false, summary:{ok_sites:1}}, schedule:{enabled:true, time:'08:00', next_run:'tomorrow'}});
const index = generated_at => ({ok:true, generated_at, models:[], sites:[]});

(async () => {
  responses = [status('old')];
  await t.checkSnapshotUpdates();
  assert.deepEqual(requests, ['/api/snapshot-status']);
  assert.equal(t.renders, 0);
  assert.deepEqual(t.state.server.task.logs, ['kept']);

  requests = [];
  t.state.detailCache.set('model', {old:true});
  responses = [status('new'), index('new')];
  await t.checkSnapshotUpdates();
  assert.deepEqual(requests, ['/api/snapshot-status', '/api/index']);
  assert.equal(t.state.meta.generated_at, 'new');
  assert.equal(t.state.detailCache.size, 0);
  assert.equal(t.renders, 1);

  requests = [];
  responses = [new Error('offline')];
  await t.checkSnapshotUpdates();
  assert.equal(t.state.meta.generated_at, 'new');
  assert.equal(t.renders, 1);
  document.hidden = true;
  await t.checkSnapshotUpdates();
  assert.equal(requests.length, 1);
  document.hidden = false;

  let release;
  const delayed = new Promise(resolve => {release = resolve;});
  requests = [];
  responses = [status('other'), delayed];
  const pending = t.checkSnapshotUpdates();
  await new Promise(resolve => setImmediate(resolve));
  await t.checkSnapshotUpdates(); // 只允许一个状态检查在途
  assert.equal(requests.length, 2);
  t.applyIndex(index('manual'));
  release(index('other'));
  await pending;
  assert.equal(t.state.meta.generated_at, 'manual'); // 旧响应不能覆盖手动刷新
  assert.equal(t.renders, 2);
  console.log('PASS 快照轮询：变化、断线、隐藏页面和并发保护');
})().catch(err => {console.error(err); process.exitCode = 1;});
