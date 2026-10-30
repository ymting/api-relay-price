/* 阶段 A 页面验收：只使用合成数据，不启动服务或请求真实站点。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const element = () => ({style:{}, dataset:{}, classList:{toggle(){},add(){},remove(){}},
  addEventListener(){}, querySelector(){return element();}, querySelectorAll(){return [];}});
global.document = {addEventListener(){},querySelectorAll(){return [];},querySelector(){return element();},
  getElementById(){return element();},createElement(){return element();}};
global.localStorage = {getItem(){return null;},setItem(){}};
global.window = {};
global.alert = () => {};
global.fetch = async () => {throw new Error('This test must not use network');};
const source = fs.readFileSync(path.join(__dirname, '..', '中转站比价.html'), 'utf8');
const script = source.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/)[1];
new Function(script + '\nwindow.testA={state,rechargeOf,acctCardHtml,acctSummaryHtml,buildDetail,groupStateFor,siteCardHtml,modelHealthFor,groupHealthFor,healthBadge,accountFeatureIssues,topupRecordsHtml,parseBillingTiers,tierCondLabel,offerTiersHtml,detectServer,isFilePage};')();
const t = window.testA;
t.state.currency = 'USD';
t.state.accounts = [];
t.state.siteMeta = [{id:'fixture',name:'Fixture',enabled:true}];
t.state.server = {sites:[{id:'fixture',name:'Fixture',enabled:true}]};
let checks = 0;
function check(name, callback){callback(); checks++; console.log('PASS ' + name);}

check('充值成功零条显示真实零', () => {
  assert.deepEqual(t.rechargeOf({topups_complete:true,recharge_supported:true,recharge_usd:0,
    recharge_count:0,quota_usd:10,used_quota_usd:5}), {value:0,estimated:false});
});
check('充值失败显示未知，不倒推为充值或零', () => {
  assert.deepEqual(t.rechargeOf({topups_complete:false,recharge_usd:null,quota_usd:10,used_quota_usd:5}),
    {value:null,estimated:false});
});
check('充值失败但有旧完整快照时显示旧值', () => {
  assert.deepEqual(t.rechargeOf({topups_complete:false,recharge_usd:25}), {value:25,estimated:false});
});
check('旧快照继续保留显式估算行为', () => {
  assert.deepEqual(t.rechargeOf({quota_usd:10,used_quota_usd:5}), {value:15,estimated:true});
});
check('没有已用金额时不能把未知当零估算', () => {
  assert.deepEqual(t.rechargeOf({quota_usd:10}), {value:null,estimated:false});
});
const partial = {site:'fixture',ok:true,group:'default',wallet:{balance_kind:'wallet',unit:'USD',amount:10},quota_usd:10,used_quota_usd:null,
  topups_complete:false,keys_complete:false,recharge_usd:null,token_count:null,
  topups_error:'fixture topup error',keys_error:'fixture key error',fetched_at:'2026-10-15T00:00:00Z'};
check('部分刷新状态、未知金额和独立错误清楚可见', () => {
  const html = t.acctCardHtml(partial);
  assert.ok(html.includes('部分未更新'));
  assert.ok(html.includes('fixture topup error') && html.includes('fixture key error'));
  assert.ok(!html.includes('$0.00'));
  assert.ok(html.includes('data-home-action="refresh-account"'));
});
check('所有金额未知时汇总也显示未知', () => {
  const html = t.acctSummaryHtml([{...partial,quota_usd:null,wallet:{...partial.wallet,amount:null}}]);
  assert.ok(!html.includes('$0.00'));
  assert.ok(html.includes('0/1 个站点金额已知'));
});
check('只有充值未知时其汇总不显示零', () => {
  const html = t.acctSummaryHtml([partial]);
  assert.ok(html.includes('$10.00'));
  assert.ok(!html.includes('$0.00'));
});
t.state.accounts = [partial];
check('Key 读取失败不能显示账号分组无 Key', () => {
  assert.match(t.groupStateFor('fixture','default',{}).text, /待更新/);
});
const offer = {site:'fixture',site_name:'Fixture',model_raw:'fixture-model',model_key:'fixture-model',
  group:'default',input:.01,output:.02,per_call:null,group_ratio:1,pricing_verified:false,
  dynamic:true,billing_expr:'tier("<unsafe>", p*0.01)'};
check('待确认价格参与比价并标参考价，表达式说明正确转义', () => {
  const html = t.buildDetail({offers:[offer],site_count:1});
  assert.ok(html.includes('待确认 · 参考价'));
  assert.ok(html.includes('Key 状态未知'));
  assert.ok(html.includes('row-best')); // 2026-10-16 行为变更：参考价也参与比价，最低可由参考价获得
  assert.ok(!html.includes('<unsafe>'));
  assert.ok(html.includes('&lt;unsafe&gt;'));
});
check('未知表达式不声称已按短上下文计算价格', () => {
  const html = t.buildDetail({offers:[{...offer,input:null,output:null,pricing_basis:'unknown_expression'}],site_count:1});
  assert.ok(html.includes('系统未能解析出确定价格'));
  assert.ok(!html.includes('表内价格按短上下文'));
});
check('参考价与确认价格同场竞技，低价者得最低', () => {
  const html = t.buildDetail({offers:[offer,{...offer,site_name:'Confirmed',input:2,
    pricing_verified:true,dynamic:false}],site_count:1});
  // 纯按价格排序：Fixture(.01) 排在 Confirmed(2) 前面，最低徽标落在更便宜的参考价上
  assert.ok(html.indexOf('▸ Fixture') < html.indexOf('▸ Confirmed'));
  assert.ok(html.indexOf('row-best') < html.indexOf('▸ Confirmed'));
});
// —— 分档价格表：NewAPI 表达式静态解析 + Sub2API intervals 直接展示 ——
check('表达式解析出上下文档位，系数与阈值正确', () => {
  const parsed = t.parseBillingTiers('((len < 272000)) ? (tier("base", p * 2 + c * 10 + cr * 0.2)) : (((len >= 272000)) ? (tier("base", p * 4 + c * 15 + cr * 0.4)) : (tier("base", p * 2 + c * 10)))');
  assert.equal(parsed.kind, 'tiers');
  assert.equal(parsed.tiers.length, 2); // 不可达兜底分支与其他档同价，应被丢弃
  assert.deepEqual(parsed.tiers[0].conds, [['<', 272000]]);
  assert.deepEqual(parsed.tiers[1].conds, [['>=', 272000]]);
  assert.equal(parsed.tiers[1].coeffs.input, 4);
  assert.equal(parsed.tiers[1].coeffs.output, 15);
});
check('fixed 按次价与分时表达式分类正确', () => {
  const fixed = t.parseBillingTiers('tier("base", fixed(0.5))');
  assert.equal(fixed.kind, 'tiers');
  assert.equal(fixed.tiers[0].per_call, 0.5);
  assert.equal(t.parseBillingTiers('hour("Asia/Shanghai") >= 9 ? tier("peak", p * 9) : tier("off_peak", p * 4.5)').kind, 'time');
  // 裸线性系数（无 tier 包裹）宽容解析成单档，与 Python 侧 parse_tier_coefficients 口径一致；单档不渲染分档表
  const bare = t.parseBillingTiers('p * 1.5 + c * 4.5');
  assert.equal(bare.kind, 'tiers');
  assert.equal(bare.tiers.length, 1);
  assert.equal(t.offerTiersHtml({group_ratio: 1, billing_expr: 'p * 1.5 + c * 4.5'}), '');
});
check('分档表渲染：表达式档位乘分组倍率，标签转义', () => {
  const html = t.offerTiersHtml({group_ratio: 2, billing_expr: '((len <= 200000)) ? (tier("≤200K", p * 2 + c * 6)) : (tier("200k_plus", p * 4 + c * 12))'});
  assert.ok(html.includes('分档价格'));
  assert.ok(html.includes('≤200K'));
  assert.ok(html.includes('$4.00'));   // 输入 2 × 倍率 2
  assert.ok(html.includes('$24.00'));  // 输出 12 × 倍率 2
});
check('Sub2API 分档区间直接展示，每token价换算成每百万并乘倍率', () => {
  const html = t.offerTiersHtml({group_ratio: .5, pricing_intervals: [
    {min_tokens: 0, max_tokens: 200000, tier_label: '<x>&lt;200K', input_price: 2e-6, output_price: 6e-6},
    {min_tokens: 200000, max_tokens: null, tier_label: '>200K', input_price: 4e-6, output_price: 1.2e-5}]});
  assert.ok(html.includes('&lt;x&gt;&amp;lt;200K')); // 远端标签要转义
  assert.ok(html.includes('$2.00'));   // 2e-6 × 1e6 × 0.5
  assert.ok(html.includes('$3.00'));
  assert.ok(html.includes('&gt;200K')); // > 会被转义，正是想要的
});
check('无档位信息时不出分档表', () => {
  assert.equal(t.offerTiersHtml({group_ratio: 1, billing_expr: 'hour("Asia/Shanghai") >= 9 ? tier("peak", p * 9) : tier("off_peak", p * 4.5)'}), '');
  assert.equal(t.offerTiersHtml({group_ratio: 1, pricing_intervals: [{tier_label: 'only', input_price: 1e-6}]}), '');
  assert.equal(t.offerTiersHtml({group_ratio: 1}), '');
});
check('服务等级表达式仅展示已验证的未指定等级分档', () => {
  const base = 'len < 272000 ? tier("base", p * 2 + c * 10) : tier("long", p * 4 + c * 15)';
  const raw = `(${base}) * (param("service_tier") == "priority" ? 2 : 1)`;
  const priced = {...offer, pricing_verified:true, service_tier_basis:'unspecified',
    billing_expr:raw, billing_base_expr:base, group_ratio:.22};
  const html = t.buildDetail({offers:[priced],site_count:1});
  assert.ok(html.includes('未指定服务等级'));
  assert.ok(html.includes('表内价格按未指定 service_tier 计算'));
  const tiers = t.offerTiersHtml(priced);
  assert.ok(tiers.includes('未指定 service_tier 的计费表达式'));
  assert.ok(tiers.includes('$0.44'));
  assert.ok(tiers.includes('$0.88'));
  assert.equal(t.offerTiersHtml({...priced,service_tier_basis:null}), '');
});
// —— 全局设置：分档价格表是否默认展开 ——
const tierOffer = {group_ratio: 2, billing_expr: '((len <= 200000)) ? (tier("≤200K", p * 2 + c * 6)) : (tier("200k_plus", p * 4 + c * 12))'};
check('分档表默认收起，服务端设置可默认展开', () => {
  const collapsed = t.offerTiersHtml(tierOffer);
  assert.ok(collapsed.includes('<details class="tier-box"'));
  assert.ok(!collapsed.includes(' open')); // 无设置（走 localStorage 空值）时收起
  t.state.server.settings = {tier_expand_default: true};
  try {
    assert.ok(t.offerTiersHtml(tierOffer).includes('<details class="tier-box" open'));
    t.state.server.settings = {tier_expand_default: false};
    const html = t.offerTiersHtml(tierOffer);
    assert.ok(!/\bdetails[^>]*\bopen\b/.test(html.match(/<details class="tier-box"[^>]*>/)[0]));
  } finally { t.state.server.settings = {}; }
});
// 凭据「小眼睛」：令牌/密码这类输入框都要有；账户 ID 直接显示、不需要眼睛
check('令牌输入框带小眼睛', () => {
  const html = t.siteCardHtml({id:'fx',name:'Fx',base_url:'https://fx.example',type:'newapi',enabled:true,
    auth:{}, has_secret:{management_token:true}}, 0, ['newapi'], null);
  assert.ok(html.includes('data-act="eye"'));
  assert.ok(html.includes('data-cred="management_token"'));
  assert.ok(html.includes('data-act="clear"'));
});
check('账户 ID 直接显示，不带眼睛', () => {
  const html = t.siteCardHtml({id:'fx',name:'Fx',base_url:'https://fx.example',type:'newapi',enabled:true,
    auth:{}, has_secret:{management_token:true}}, 0, ['newapi'], null);
  const acid = html.match(/<input[^>]*data-f="auth\.account_id"[^>]*>/)[0];
  assert.ok(!acid.includes('type="password"'));    // 不掩码
  assert.ok(!/data-cred="account_id"/.test(html)); // 没有针对它的眼睛按钮
});
check('Cookie 用显式按钮、令牌用输入框内的小眼睛（可共存）', () => {
  const html = t.siteCardHtml({id:'fx',name:'Fx',base_url:'https://fx.example',type:'newapi',enabled:true,
    auth:{}, has_secret:{cookie:true, management_token:true}}, 0, ['newapi'], 'cookie');
  assert.ok(html.includes('data-act="reveal"') && html.includes('data-cred="cookie"'));   // textarea 没掩码可切
  assert.ok(html.includes('data-act="eye"') && html.includes('data-cred="management_token"'));
});
// 模型健康：兜底到"全组"时必须写明不代表当前分组（2026-10-16 修过一个只在真实数据上暴露的漏标）
check('报价快照自带的全组汇总也须注明不代表当前分组', () => {
  t.state.server = {health:{}};
  const h = t.groupHealthFor({site:'s', model_raw:'m', group:'g1',
    health:{model_name:'m', success_rate:98, avg_latency_ms:100, avg_tps:10,
            recent_success_series:[{ts:1, success_rate:98}], fetched_at:'2026-10-16T00:00:00Z'}});
  assert.equal(h.scope, 'model');
  assert.ok(h.text.includes('全组'));
  assert.ok(h.note.includes('g1') && h.note.includes('不代表当前分组'));
  assert.ok(h.tip.includes('不代表当前分组'));
});
check('有本分组精确样本时不标全组、也不加说明', () => {
  t.state.server = {health:{sites:{s:{m:{fetched_at:'2026-10-16T00:00:00Z',
    summary:{success_rate:90, avg_latency_ms:1, avg_tps:1},
    groups:[{group:'g1', success_rate:99, avg_latency_ms:1, avg_tps:1, request_count:5}]}}}}};
  const h = t.groupHealthFor({site:'s', model_raw:'m', group:'g1'});
  assert.equal(h.scope, 'group');
  assert.ok(!h.text.includes('全组'));
  assert.equal(h.note, '');
});
check('该分组确实无有效样本时也说明全组参考不代表当前分组', () => {
  t.state.server = {health:{sites:{s:{m:{fetched_at:'2026-10-16T00:00:00Z',
    summary:{success_rate:95, avg_latency_ms:1, avg_tps:1}, groups:[]}}}}};
  const h = t.groupHealthFor({site:'s', model_raw:'m', group:'g2'});
  assert.equal(h.scope, 'model');
  assert.ok(h.note.includes('g2') && h.note.includes('不代表当前分组'));
});
check('模型卡片汇总与本分组失败分别展示，格式与站点卡片一致', () => {
  const at = new Date().toISOString();
  t.state.server = {health:{sites:{s:{m:{health_schema_version:2, fetched_at:at,
    model_summary_checked_at:at,
    model_summary:{success_rate:100,avg_latency_ms:56190,avg_tps:17.4,fetched_at:at,
      window_end:1792116000,recent_success_series:[{ts:1792112400,success_rate:100}]},
    summary:{success_rate:99},groups:[{group:'g',success_rate:0,series:[{ts:1792112400,success_rate:0}]}]}}}}};
  const offer = {site:'s',model_raw:'m',group:'g'};
  const model = t.modelHealthFor(offer), group = t.groupHealthFor(offer);
  assert.equal(model.text, '100.00% · 全组');
  assert.equal(model.latencyText, '56.19s');
  assert.equal(model.throughputText, '17.4t/s');
  assert.equal(group.text, '0.0%');
  assert.equal(group.scope, 'group');
  const html = t.healthBadge(offer);
  assert.ok(html.includes('56.19s') && html.includes('17.4t/s'));
  assert.equal((html.match(/aria-hidden="true"/g) || []).length, 24);
  assert.equal(model.samples.filter(Boolean).length, 1);
});
check('近期历史采样不冒充 24 小时总体成功率', () => {
  const at = new Date().toISOString();
  t.state.server = {health:{sites:{s:{m:{health_schema_version:2,fetched_at:at,model_summary_checked_at:at,
    model_summary:{success_rate:null,avg_latency_ms:null,avg_tps:null,fetched_at:at,window_end:1792116000,
      recent_success_series:[{ts:1791900000,success_rate:100}]},groups:[]}}}}};
  const h = t.modelHealthFor({site:'s',model_raw:'m',group:'g'});
  assert.equal(h.text, '24h 暂无汇总');
  assert.equal(h.latencyText, '—');
  assert.equal(h.throughputText, '—');
  assert.ok(h.historicalSamples && h.note.includes('历史采样'));
});
check('成功读到空汇总后不能回退到更旧报价中的 100%', () => {
  const at = new Date().toISOString();
  t.state.server = {health:{sites:{s:{m:{health_schema_version:2,fetched_at:at,
    model_summary_checked_at:at,model_summary:null,groups:[]}}}}};
  const h = t.modelHealthFor({site:'s',model_raw:'m',group:'g',health:{success_rate:100,fetched_at:'2026-10-15T00:00:00Z'}});
  assert.equal(h.text, '暂无数据');
  assert.equal(h.scope, 'none');
});
check('失效值不会被当成 0%，真实零成功率仍保留', () => {
  t.state.server = {};
  for (const value of [null,false,'', ' ',101,-1,'NaN']) {
    const h = t.modelHealthFor({site:'s',model_raw:'m',health:{success_rate:value}});
    assert.ok(!h.text.includes('%'));
  }
  const h = t.modelHealthFor({site:'s',model_raw:'m',health:{success_rate:0}});
  assert.equal(h.text, '0.00% · 全组');
  assert.equal(h.kind, 'bad');
});
check('汇总接口失败时仍保留报价内的成功快照并标旧数据', () => {
  const at = new Date().toISOString();
  t.state.server = {health:{sites:{s:{m:{health_schema_version:2,fetched_at:at,model_summary_checked_at:at,
    model_summary:null,model_summary_error:'HTTP 403',groups:[]}}}}};
  const h = t.modelHealthFor({site:'s',model_raw:'m',health:{success_rate:98,
    fetched_at:'2026-10-15T00:00:00Z'}});
  assert.ok(h.text.startsWith('98.00%') && h.text.includes('过期'));
  assert.ok(h.note.includes('更新失败') && h.note.includes('保留上次成功数据'));
});
check('损坏的采样时间不会让模型状态渲染失败', () => {
  t.state.server = {};
  const offer = {site:'s',model_raw:'m',health:{success_rate:100,
    recent_success_series:[{ts:1e300,success_rate:100},{ts:1,success_rate:101}]}};
  assert.doesNotThrow(() => t.healthBadge(offer));
  assert.equal(t.modelHealthFor(offer).samples.filter(Boolean).length, 0);
});
check('未接入充值记录单独说明，不误报账户刷新失败', () => {
  const account = {...partial,topups_supported:false,recharge_supported:false,keys_complete:true,
    features:{topups:{status:'not_migrated',error_category:'NOT_MIGRATED',provider:'api'}},
    recharge_usd:null,used_quota_usd:3.5,request_count:17};
  const issues=t.accountFeatureIssues(account),html=t.acctCardHtml(account);
  assert.equal(issues.failed.length,0);
  assert.deepEqual(issues.unavailable,['充值记录']);
  assert.ok(!html.includes('部分未更新'));
  assert.ok(html.includes('暂未接入：充值记录') && html.includes('$3.50'));
  assert.deepEqual(t.rechargeOf(account),{value:null,estimated:false});
});
check('累计用量的真实请求失败仍显示部分未更新', () => {
  const account={...partial,topups_supported:false,keys_complete:true,usage_complete:false,usage_error:'HTTP 403',
    features:{usage:{status:'error',error_category:'PERMISSION_DENIED',provider:'api'},
      topups:{status:'not_migrated',error_category:'NOT_MIGRATED',provider:'api'}}};
  const issues=t.accountFeatureIssues(account),html=t.acctCardHtml(account);
  assert.equal(issues.failed[0].label,'累计用量');
  assert.ok(html.includes('部分未更新') && html.includes('累计用量未完整更新：HTTP 403'));
  assert.ok(html.includes('暂未接入：充值记录'));
});
check('Sub2API 兑换入账和支付金额分开说明，记录中的文本须转义', () => {
  const a={...partial,topups_complete:true,keys_complete:true,recharge_scope:'balance_credits_gross',recharge_usd:12,
    topup_records:{note:'含赠送，退款前',sources:{payments:{complete:true},redemptions:{complete:true}},
      payment_totals:{CNY:{paid:70}},refunded_credit_amount:5,credit_unit:'USD',items:[{source:'redemptions',type:'balance',status:'used',
        amount:12,unit:'USD',at:'2026-10-16T00:00:00Z'},{source:'payments',type:'<unsafe>',status:'completed',
        amount:10,unit:'USD',currency:'CNY',pay_amount:70,refund_amount:5,at:'2026-10-16T00:00:00Z'}]}};
  const html=t.acctCardHtml(a);
  assert.ok(html.includes('累计入账（含兑换与赠送，退款前）'));
  assert.ok(html.includes('CNY：实付 70.00（退款前）'));
  assert.ok(html.includes('70 CNY / 5 USD') && html.includes('不等同于实退现金'));
  assert.ok(html.includes('&lt;unsafe&gt;') && !html.includes('<unsafe>'));
});
check('Sub2API V2 标明可见分组，TPM 不冒充生成速度', () => {
  t.state.server={};
  const o={site:'s',model_raw:'m',health:{source:'sub2api_monitor_v2',success_rate:99,
    traffic_tpm:1200,avg_tps:null,avg_latency_ms:2100,window_label:'24h',scope_label:'可见分组'}};
  const h=t.modelHealthFor(o),html=t.healthBadge(o);
  assert.equal(h.text,'99.00% · 可见分组');
  assert.equal(h.throughputText,'1200.0t/min');
  assert.ok(html.includes('流量') && !html.includes('1200.0t/s'));
  assert.ok(!h.tip.includes('成功率排除业务拒绝'));
});
check('Sub2API V1 七天探测不标成全组 24h 请求成功率', () => {
  t.state.server={};
  const o={site:'s',model_raw:'m',health:{source:'sub2api_monitor_v1',success_rate:95,
    avg_latency_ms:300,window_label:'7天探测',scope_label:'监控「vip」'}};
  const h=t.modelHealthFor(o),html=t.healthBadge(o);
  assert.equal(h.text,'95.00% · 7天探测');
  assert.ok(h.note.includes('最近一次探测'));
  assert.ok(!h.tip.includes('近 24 小时成功率'));
  assert.ok(html.includes('探测明细采样未提供'));
});
check('Sub2API V2 真实分组零成功率不被脱敏后的零计数抹掉', () => {
  t.state.server={health:{sites:{s:{m:{source:'sub2api_monitor_v2',groups:[{group:'vip',group_id:8,
    success_rate:0,has_samples:true,source:'sub2api_monitor_v2',window_label:'24h'}]}}}}};
  const h=t.groupHealthFor({site:'s',model_raw:'m',group:'vip',group_id:8});
  assert.equal(h.scope,'group');
  assert.equal(h.kind,'bad');
  assert.ok(h.text.startsWith('0.0%'));
});
check('同名分组优先按 ID 匹配，多个平台重复项不任取一个', () => {
  const groups=[{group:'same',group_id:1,success_rate:100,has_samples:true},
    {group:'same',group_id:2,success_rate:0,has_samples:true}];
  t.state.server={health:{sites:{s:{m:{groups}}}}};
  assert.equal(t.groupHealthFor({site:'s',model_raw:'m',group:'same',group_id:2}).text,'0.0%');
  assert.equal(t.groupHealthFor({site:'s',model_raw:'m',group:'same'}).scope,'none');
});
check('Sub2API V2 聚合覆盖不足时明确说明', () => {
  t.state.server={};
  const h=t.modelHealthFor({site:'s',model_raw:'m',health:{source:'sub2api_monitor_v2',success_rate:100,coverage_complete:false}});
  assert.ok(h.text.includes('覆盖不足') && h.note.includes('尚未覆盖完整时间窗口'));
});
t.state.server = {};

/* ---- 今日用量：金额 + Token（M 为单位） ---- */
const dailyAccount = (daily) => ({site:'fixture', site_name:'Fixture', ok:true,
  wallet:{balance_kind:'wallet', unit:'USD', amount:10, quota_per_unit:500000, scope_id:'fixture:account:7'},
  quota_usd:10, used_quota_usd:5, request_count:17, token_count:1,
  topups_complete:true, keys_complete:true, recharge_usd:15, recharge_count:2,
  features:{}, fetched_at:'2026-10-16T08:00:00Z', daily_usage:daily});
t.state.server = {sites:[{id:'fixture', name:'Fixture', enabled:true}]};

check('今日用量列给出金额与 Token（M 为单位）', () => {
  const html = t.acctCardHtml(dailyAccount({date:'2026-10-16', amount:0.9853101375, unit:'USD',
    tokens:2441112, cache_tokens:6866743, requests:118, source:'dashboard.stats', complete:true}));
  // 今日金额常是几分钱：必须按量级给小数位，不能一律 $0.99 / $0.00
  assert.ok(html.includes('$0.985'), '金额未按美元显示');
  assert.ok(html.includes('2.44 M'), 'Token 未按 M 显示');
  assert.ok(html.includes('colspan="7"'), '详情行跨列未与新列同步');
});
check('今日 Token 不含缓存，缓存只在展开行另列', () => {
  const html = t.acctCardHtml(dailyAccount({date:'2026-10-16', amount:0.99, unit:'USD',
    tokens:2441112, cache_tokens:6866743, requests:118, source:'dashboard.stats', complete:true}));
  assert.ok(html.includes('2.44 M'));
  assert.ok(!html.includes('9.31 M'), '缓存 Token 不该并进主显示');
  assert.ok(html.includes('6.87 M'), '缓存应单独列出');
});
check('今日用量取不到时显示未知，不画成 0', () => {
  const html = t.acctCardHtml(dailyAccount(null));
  assert.ok(!html.includes('0.00 M'), '「没取到」不能渲染成「没用量」');
});
check('今日用量只统计到部分时标出下界', () => {
  const html = t.acctCardHtml(dailyAccount({date:'2026-10-16', amount:null, unit:null, tokens:2000,
    requests:5000, source:'log.self', complete:false, error:'Token:超过1000条，只统计到部分'}));
  assert.ok(html.includes('acct-daily-partial'), '未标记为不完整');
  assert.ok(html.includes('≥'), '未标出这只是下界');
  assert.ok(html.includes('只统计到部分'));
});
check('极小用量不能被两位小数压成 0', () => {
  // luna 今日真实数据：2 次请求、1005 token、$0.000056
  const html = t.acctCardHtml(dailyAccount({date:'2026-10-16', amount:0.000056, unit:'USD',
    tokens:1005, requests:2, source:'data.self', complete:true}));
  assert.ok(!html.includes('0.00 M'), 'Token 被压成 0.00 M，和「真没用量」无法区分');
  assert.ok(html.includes('$0.000056'), '金额被压成 $0.00');
  assert.ok(html.includes('精确 1,005'), '展开行应给出精确 Token 数');
});
check('汇总行给出今日用量合计并说明不含缓存', () => {
  const html = t.acctSummaryHtml([
    dailyAccount({date:'2026-10-16', amount:0.5, unit:'USD', tokens:2441112,
      cache_tokens:6866743, requests:118, source:'dashboard.stats', complete:true}),
    dailyAccount({date:'2026-10-16', amount:0.25, unit:'USD', tokens:1000000,
      requests:2, source:'data.self', complete:true})]);
  assert.ok(html.includes('今日用量'));
  assert.ok(html.includes('$0.75'), '金额合计不对');
  assert.ok(html.includes('3.44 M'), 'Token 合计不对');
  assert.ok(html.includes('不含缓存'), '未说明 Token 口径');
});

check('接口调用不自带 /api 前缀（state.apiBase 已含）', () => {
  // apiGet/apiPost 都会拼 state.apiBase（默认 '/api'），多写一层就变成 /api/api/xxx → 404。
  const bad = [...source.matchAll(/api(?:Get|Post)\(\s*'(\/api\/[^']*)'/g)].map(m => m[1]);
  assert.deepEqual(bad, [], '这些调用会拼成 /api/api/...：' + bad.join(', '));
});

// —— 连接策略（B5）：http 页面只认同源；file:// 只做无凭据探活 ——
async function checkAsync(name, callback){
  await callback(); checks++; console.log('PASS ' + name);
}
async function runConnectionChecks(){
await checkAsync('file:// 页面只探测 /api/ping 且不读取响应', async () => {
  const calls = [];
  const savedFetch = global.fetch, savedLocation = global.location;
  global.location = {protocol:'file:', origin:'null', replace(){}};
  global.fetch = async (url, options) => {
    calls.push({url, options});
    return {ok:true, status:200};
  };
  try {
    const found = await t.detectServer();
    assert.equal(found.probePort, 8765);
    // 命中第一个端口就停，不会继续扫后面的端口。
    assert.deepEqual(calls.map(c => c.url), ['http://127.0.0.1:8765/api/ping']);
    assert.ok(calls.every(c => c.options.mode === 'no-cors'));
    assert.ok(calls.every(c => !c.url.includes('/api/state')));
  } finally { global.fetch = savedFetch; global.location = savedLocation; }
});

await checkAsync('http 页面同源失败后不再扫描端口', async () => {
  const calls = [];
  const savedFetch = global.fetch, savedLocation = global.location;
  global.location = {protocol:'http:', origin:'http://127.0.0.1:8765', replace(){}};
  global.fetch = async (url, options) => { calls.push({url, options}); throw new Error('offline'); };
  try {
    assert.equal(await t.detectServer(), null);
    assert.deepEqual(calls.map(c => c.url), ['/api/state']);
    assert.equal(calls[0].options.credentials, 'same-origin');
  } finally { global.fetch = savedFetch; global.location = savedLocation; }
});

console.log(checks + ' checks passed');
}
runConnectionChecks().catch(err => { console.error(err); process.exit(1); });
