/* 框架切换的真实事件处理器 + 表单往返，不联网、不写用户配置。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const empty = () => ({style:{},dataset:{},classList:{toggle(){}},addEventListener(){},querySelectorAll(){return [];}});
global.document = {addEventListener(){},querySelectorAll(){return [];},getElementById:empty,createElement:empty};
global.window = {};
global.localStorage = {getItem(){return null;},setItem(){throw new Error('Must not persist drafts');}};
global.fetch = async () => {throw new Error('Framework switching must not use network');};
const html = fs.readFileSync(path.join(__dirname,'..','中转站比价.html'),'utf8');
const script = html.match(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/)[1];
new Function(script + '\nwindow.siteTest={state,newSiteDraft,siteCardHtml,bindSiteCards,readSitesFromForm,siteSecretKey};')();
const t = window.siteTest;
t.state.accounts = [];
t.state.server = {sites:[]};
const decode = s => s.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
function fieldsFrom(cardHtml){
  return [...cardHtml.matchAll(/<input\b[^>]*>|<select\b[^>]*>[\s\S]*?<\/select>|<textarea\b[^>]*>[\s\S]*?<\/textarea>/g)]
    .map(([source]) => {
      const opening = source.slice(0,source.indexOf('>')+1);
      const attrs = Object.fromEntries([...opening.matchAll(/([\w-]+)="([^"]*)"/g)].map(m => [m[1],decode(m[2])]));
      if (!attrs['data-f']) return null;
      const tagName = source.match(/^<(\w+)/)[1].toUpperCase();
      const dataset = {};
      for (const [key,value] of Object.entries(attrs)) if (key.startsWith('data-'))
        dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())] = value;
      let value = attrs.value || '';
      if (tagName === 'SELECT') {
        const selected = source.match(/<option value="([^"]*)" selected/);
        value = decode((selected || source.match(/<option value="([^"]*)"/) || [,''])[1]);
      }
      if (tagName === 'TEXTAREA') value = decode(source.slice(source.indexOf('>')+1,source.lastIndexOf('</')));
      const handlers = {};
      return {tagName,dataset,value,type:attrs.type || (tagName === 'SELECT' ? 'select-one' : 'text'),
        checked:/\schecked(?:\s|>)/.test(opening),readOnly:false,focused:false,
        addEventListener(event,fn){handlers[event]=fn;},fire(event){handlers[event]?.();},focus(){this.focused=true;}};
    }).filter(Boolean);
}
function matches(field,selector){
  if (selector === '[data-f]') return true;
  if (selector === '[data-f^="auth."]') return field.dataset.f.startsWith('auth.');
  const f = selector.match(/\[data-f=(?:"([^"]+)"|([^\]]+))\]/);
  return !!f && field.dataset.f === (f[1] || f[2]) && (!selector.startsWith('select') || field.tagName === 'SELECT');
}
function editor(sites){
  const list = {children:[],querySelectorAll(selector){return this.children.flatMap(card=>card.querySelectorAll(selector));}};
  let saves=0;
  function render(){
    list.children = sites.map((site,i) => {
      const cardHtml=t.siteCardHtml(site,i,['newapi','sub2api'],null),fields=fieldsFrom(cardHtml);
      return {html:cardHtml,querySelectorAll(selector){return fields.filter(f=>matches(f,selector));},
        querySelector(selector){return this.querySelectorAll(selector)[0] || null;}};
    });
    t.bindSiteCards(list,sites,{},render,()=>{saves++;});
  }
  render();
  return {list,render,read(){t.readSitesFromForm(list,sites);},saves:()=>saves,
    field(i,key){return list.children[i].querySelector(`[data-f="${key}"]`);},
    change(i,type){this.field(i,'type').value=type;this.field(i,'type').fire('change');}};
}
let checks=0;
function check(name,fn){fn();checks++;console.log('PASS '+name);}

check('新站点切换框架立即更换字段与说明，不需要保存',()=>{
  const site=t.newSiteDraft('newapi'),ed=editor([site]);
  assert.ok(ed.field(0,'auth.management_token'));
  ed.change(0,'sub2api');
  assert.ok(ed.field(0,'auth.access_token') && ed.field(0,'auth.refresh_token'));
  assert.equal(ed.field(0,'auth.management_token'),null);
  assert.ok(ed.list.children[0].html.includes('只填访问令牌 access token 即可'));
  assert.ok(ed.field(0,'type').focused);
  assert.equal(ed.saves(),0);
});
check('往返切换保留两种框架的未保存令牌与明文状态，首次保存仍提交新值',()=>{
  const site=t.newSiteDraft('newapi'),ed=editor([site]);
  ed.field(0,'id').value='candidate';
  ed.field(0,'name').value='Draft name';
  ed.field(0,'base_url').value='https://draft.invalid';
  ed.field(0,'auth.management_token').value='synthetic-new-management';
  ed.field(0,'auth.management_token').fire('input');
  ed.change(0,'sub2api');
  const access=ed.field(0,'auth.access_token');
  access.value='synthetic-new-access';access.type='text';access.dataset.eyeShown='1';access.fire('input');
  ed.change(0,'newapi');
  assert.equal(ed.field(0,'auth.management_token').value,'synthetic-new-management');
  assert.equal(ed.field(0,'name').value,'Draft name');
  assert.equal(ed.field(0,'base_url').value,'https://draft.invalid');
  assert.equal(ed.field(0,'id').readOnly,false);
  ed.change(0,'sub2api');
  assert.equal(ed.field(0,'auth.access_token').value,'synthetic-new-access');
  assert.equal(ed.field(0,'auth.access_token').type,'text');
  ed.read();
  assert.equal(site.auth.access_token,'synthetic-new-access');
  assert.equal(site.secret_actions.access_token,'replace');
  assert.equal(site.auth.management_token,'synthetic-new-management');
  assert.equal(site.secret_actions.management_token,'replace');
});
check('切换一站保留另一站未保存的输入和密码显示状态',()=>{
  const sites=[t.newSiteDraft('newapi'),{id:'saved',name:'Saved',base_url:'https://saved.invalid',type:'newapi',auth:{username:'u'}}];
  const ed=editor(sites),password=ed.field(1,'auth.password');
  ed.field(1,'name').value='Other draft';
  password.value='synthetic-other-password';password.type='text';password.dataset.eyeShown='1';password.fire('input');
  ed.change(0,'sub2api');
  assert.equal(ed.field(1,'name').value,'Other draft');
  assert.equal(ed.field(1,'auth.password').value,'synthetic-other-password');
  assert.equal(ed.field(1,'auth.password').type,'text');
  assert.equal(ed.field(1,'id').readOnly,true);
});
check('多个尚无 ID 的新站点凭据互不串用',()=>{
  const sites=[t.newSiteDraft('newapi'),t.newSiteDraft('newapi')],ed=editor(sites);
  ed.field(0,'auth.management_token').value='synthetic-first';ed.field(0,'auth.management_token').fire('input');
  ed.field(1,'auth.management_token').value='synthetic-second';ed.field(1,'auth.management_token').fire('input');
  ed.change(0,'sub2api');ed.change(0,'newapi');
  assert.equal(ed.field(0,'auth.management_token').value,'synthetic-first');
  assert.equal(ed.field(1,'auth.management_token').value,'synthetic-second');
  assert.notEqual(t.siteSecretKey(sites[0]),t.siteSecretKey(sites[1]));
});
check('已保存凭据改成新值后不能因眼睛或重绘被误当作保留旧值',()=>{
  const site={id:'existing',type:'sub2api',auth:{},has_secret:{access_token:true}},ed=editor([site]);
  const input=ed.field(0,'auth.access_token');
  input.value='synthetic-replacement';input.fire('input');
  input.dataset.revealed=input.value;
  ed.change(0,'newapi');ed.change(0,'sub2api');ed.read();
  assert.equal(site.auth.access_token,'synthetic-replacement');
  assert.equal(site.secret_actions.access_token,'replace');
});
check('仅查看已保存凭据并切换框架时仍按保留提交',()=>{
  const site={id:'reveal-existing',type:'sub2api',auth:{},has_secret:{access_token:true}},ed=editor([site]);
  const input=ed.field(0,'auth.access_token');
  input.value='synthetic-saved-original';input.dataset.revealed=input.value;
  ed.change(0,'newapi');ed.change(0,'sub2api');ed.read();
  assert.equal(ed.field(0,'auth.access_token').value,'synthetic-saved-original');
  assert.equal(site.auth.access_token,'');
  assert.notEqual(site.secret_actions?.access_token,'replace');
});
check('Sub2API 说明写明可选字段、获取位置及默认接口前缀',()=>{
  const ed=editor([t.newSiteDraft('sub2api')]),text=ed.list.children[0].html;
  for(const phrase of ['refresh token（选填','模型 API Key（选填','auth_token','refresh_token','Local Storage',
    'Network','/api/v1/user/profile','不要连同 Bearer','到期时间都可留空','高级设置（通常无需填写）']) assert.ok(text.includes(phrase),phrase);
  assert.equal(ed.field(0,'api_prefix').value,'/api/v1');
  assert.equal(ed.field(0,'auth.expires_at').value,'');
});
check('每站代理独立填写并随该站提交，留空表示跟随全局',()=>{
  const sites=[t.newSiteDraft('newapi'),
    {id:'proxied',name:'Proxied',base_url:'https://proxied.invalid',type:'newapi',auth:{},proxy:'http://127.0.0.1:7890'}];
  const ed=editor(sites);
  assert.equal(ed.field(1,'proxy').value,'http://127.0.0.1:7890');
  assert.equal(ed.field(0,'proxy').value,'');
  ed.field(1,'proxy').value='127.0.0.1:1080';
  ed.read();
  assert.equal(sites[1].proxy,'127.0.0.1:1080');
  assert.equal(sites[0].proxy,'');
});
check('Sub2API 长步骤收进折叠区，首屏只留结论',()=>{
  const text=editor([t.newSiteDraft('sub2api')]).list.children[0].html;
  assert.ok(text.includes('令牌从浏览器哪里取'));
  assert.ok(text.indexOf('<details')<text.indexOf('令牌从浏览器哪里取'));
  // 步骤细节必须在折叠区之后，才不会首屏就占掉半屏
  assert.ok(text.indexOf('本地存储（Local Storage）')>text.indexOf('令牌从浏览器哪里取'));
});
check('CCS 图标读取已有值，修改后往返切换框架仍按字符串提交',()=>{
  const site={id:'icon-site',type:'newapi',auth:{},icon:'openai',icon_url:'https://images.example.com/old.png'},ed=editor([site]);
  assert.equal(ed.field(0,'icon').value,'openai');
  assert.equal(ed.field(0,'icon_url').value,'https://images.example.com/old.png');
  ed.field(0,'icon').value='custom-icon';
  ed.field(0,'icon_url').value='https://images.example.com/icon.png?v=2&size=64';
  ed.change(0,'sub2api');ed.change(0,'newapi');ed.read();
  const submitted=JSON.parse(JSON.stringify(site));
  assert.equal(submitted.icon,'custom-icon');
  assert.equal(submitted.icon_url,'https://images.example.com/icon.png?v=2&size=64');
  assert.equal(ed.field(0,'icon_url').value,submitted.icon_url);
  assert.equal(ed.saves(),0);
});
check('清空 CCS 图标后重绘不会恢复旧值，也不影响其他站点',()=>{
  const sites=[{id:'clear-icon',type:'sub2api',auth:{},icon:'openai',icon_url:'https://images.example.com/old.png'},
    {id:'other-icon',type:'newapi',auth:{},icon:'anthropic',icon_url:''}],ed=editor(sites);
  ed.field(0,'icon').value='';ed.field(0,'icon_url').value='';
  ed.read();ed.render();ed.read();
  assert.equal(sites[0].icon,'');
  assert.equal(sites[0].icon_url,'');
  assert.equal(ed.field(0,'icon').value,'');
  assert.equal(ed.field(0,'icon_url').value,'');
  assert.equal(sites[1].icon,'anthropic');
});
console.log(checks+' checks passed');
