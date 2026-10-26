"""Real headless Edge: secret actions, per-site drafts and policy controls."""
import html
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
def _find_edge() -> Path:
    """优先用 RELAY_EDGE_PATH，其次查两个标准安装位置；找不到就明确失败。"""
    candidates = []
    override = os.environ.get("RELAY_EDGE_PATH")
    if override:
        candidates.append(Path(override))
    candidates += [Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
                   Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise SystemExit("找不到 Microsoft Edge；请设置 RELAY_EDGE_PATH 指向 msedge.exe")


EDGE = _find_edge()
scratch = Path(tempfile.mkdtemp(prefix="relay-stage-d-browser-"))
bootstrap = r'''
window.qaCalls=[]; window.qaErrors=[];
window.qaSites=[{id:'a',name:'A',type:'newapi',enabled:true,base_url:'https://a.invalid',auth:{username:'u'},has_secret:{management_token:true,cookie:true,password:true}},
 {id:'b',name:'B',type:'sub2api',enabled:true,base_url:'https://b.invalid',auth:{},has_secret:{access_token:true,refresh_token:true}}];
window.qaSettings={currency:'USD',rate:7.2,connection_policy:{mode:'api_preferred',compat_enabled:true},schedule:{enabled:false,time:'04:30'},auto_refresh_model_health:false};
window.addEventListener('error',e=>qaErrors.push(e.message));
window.addEventListener('unhandledrejection',e=>qaErrors.push(String(e.reason)));
window.fetch=async(url,init={})=>{
 const route=new URL(url,location.href).pathname,body=init.body?JSON.parse(init.body):{};
 qaCalls.push({route,body}); let result;
 if(route.endsWith('/state'))result={ok:true,mode:'server',sites:qaSites,site_types:['newapi','sub2api'],settings:qaSettings,accounts:{accounts:[]},health:{sites:{}},task:{running:false},account_refresh:{running:false},schedule:{enabled:false}};
 else if(route.endsWith('/index'))result={ok:true,models:[],sites:[],model_count:0,offer_count:0,site_count:0};
 else if(route.endsWith('/sites')){
  qaSites=JSON.parse(JSON.stringify(body.sites));
  qaSites.forEach(s=>{
   s.has_secret=s.has_secret||{};
   ['cookie','password','management_token','access_token','refresh_token','model_api_key'].forEach(k=>{
    if(s.secret_actions?.[k]==='clear')s.has_secret[k]=false;
    else if(s.auth?.[k])s.has_secret[k]=true;
    delete s.auth[k];
   }); delete s.secret_actions;
  });result={ok:true,sites:qaSites};
 }else if(route.endsWith('/settings')){qaSettings=body.settings;result={ok:true,settings:qaSettings};}
 else if(route.endsWith('/accounts')){
  window.qaAccountPolls=(window.qaAccountPolls||0)+1;
  const running=qaAccountPolls===1;
  result={ok:true,accounts:[{site:'a',site_name:'A',ok:true,quota_usd:running?1:5,wallet:{balance_kind:'wallet',unit:'USD',amount:running?1:5}},
   {site:'b',site_name:'B',ok:true,quota_usd:9,wallet:{balance_kind:'wallet',unit:'USD',amount:9}}],
   refresh:{running,done:running?1:2,total:2,current:'B',sites:['a','b']}};
 }
 else if(route.endsWith('/site/secret')){result={ok:true,site:body.site,field:body.field,value:'qa-secret-'+body.field};}
 else throw new Error('Unexpected route: '+route);
 return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(result))};
};
'''
checks = r'''
// 测试专用：浏览器直接打开 file:// 时只允许探活；这里把它当作服务托管的页面，
// 以便继续验证同源 API 的完整交互。生产代码不读取这个覆盖。
window.isFilePage = () => false;
document.addEventListener('DOMContentLoaded',async()=>{
 const results=[];
 const check=(name,ok)=>{results.push({name,pass:!!ok});if(!ok)throw new Error(name);};
 const until=async(fn)=>{for(let i=0;i<300;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw new Error('Timed out');};
 const card=i=>document.getElementById('siteList').children[i];
 const field=(i,f)=>card(i).querySelector(`[data-f="${f}"]`);
 try{
  await until(()=>state.mode==='server');
  check('Opening page never collects',!qaCalls.some(c=>/refresh|task\/start/.test(c.route)));
  openSystemSettings('general');
  check('Default policy is API preferred',document.getElementById('connectionMode').value==='api_preferred');
  document.getElementById('connectionMode').value='api_only';
  document.getElementById('compatEnabled').checked=false;
  document.getElementById('saveGeneralSettings').click();
  await until(()=>qaSettings.connection_policy.mode==='api_only');
  check('Global compat switch saves',qaSettings.connection_policy.compat_enabled===false);
  openSystemSettings('sites');
  check('已保存令牌默认显示星号，星号不进入字段值',field(0,'auth.management_token').value===''&&field(0,'auth.management_token').placeholder==='********'&&field(0,'auth.management_token').dataset.secretSaved==='1');
  check('已保存面板令牌均默认显示星号',['access_token','refresh_token'].every(k=>field(1,'auth.'+k).placeholder==='********'));
  check('未保存的模型 Key 保留输入提示',field(1,'auth.model_api_key').placeholder!=='********'&&field(1,'auth.model_api_key').value==='');
  const masked=field(0,'auth.management_token');
  masked.focus();masked.dispatchEvent(new MouseEvent('mouseenter'));masked.dispatchEvent(new MouseEvent('mouseleave'));
  check('星号无需悬停或移开鼠标就可见',masked.matches(':placeholder-shown')&&getComputedStyle(masked,'::placeholder').opacity==='1');
  check('显示星号不提前读取凭据',!qaCalls.some(c=>c.route.endsWith('/site/secret')));
  check('Saved Cookie is blank with presence placeholder',field(0,'auth.cookie').value===''&&field(0,'auth.cookie').placeholder.includes('已保存'));
  check('Sub2 has separate access and refresh inputs',!!field(1,'auth.access_token')&&!!field(1,'auth.refresh_token'));
  // —— 本轮修复：Sub2API 卡片的排版与说明体积 ——
  check('Sub2API 说明默认折叠，只留结论不留大段步骤',
        !!card(1).querySelector('.info-box details')&&card(1).querySelector('.info-box details').open===false);
  // 同一行（label 顶边相同）的字段，输入框顶边必须一致：长标签 + 「清空」按钮折行会把输入框顶下去
  const rows={};
  [...card(1).querySelectorAll('.form-grid .field')].forEach(f=>{
    const label=f.querySelector('label'),input=f.querySelector('input,select,textarea');
    if(!label||!input)return;
    const top=label.getBoundingClientRect().top.toFixed(1);
    (rows[top]=rows[top]||[]).push(input.getBoundingClientRect().top.toFixed(1));
  });
  check('同一行字段的输入框顶边对齐（Sub2API 的令牌字段不再下沉）',
        Object.values(rows).some(list=>list.length>1)&&Object.values(rows).every(list=>new Set(list).size===1));
  // —— 本站代理：单独站点的代理可覆盖全局 ——
  check('每站都有代理输入框',!!field(1,'proxy')&&!!field(0,'proxy'));
  field(1,'proxy').value='127.0.0.1:7890';
  const proxySaves=qaCalls.filter(c=>c.route.endsWith('/sites')).length;
  card(1).querySelector('[data-act="save"]').click();
  await until(()=>qaCalls.filter(c=>c.route.endsWith('/sites')).length>proxySaves
                 &&!card(1).querySelector('[data-act="save"]').disabled);
  check('本站代理随该站保存、不影响别的站点',
        qaSites[1].proxy==='127.0.0.1:7890'&&!qaSites[0].proxy);
  check('保存后本站代理仍留在输入框里',field(1,'proxy').value==='127.0.0.1:7890');
  field(0,'auth.management_token').value='new-synthetic-management';
  field(1,'name').value='Unsaved B';
  field(0,'connection.mode').value='api_preferred';
  field(0,'connection.features.wallet').value='api_only';
  card(0).querySelector('[data-cred="account"]').click();
  check('Switching credential tabs preserves API draft',field(0,'auth.management_token').value==='new-synthetic-management');
  check('已保存密码默认显示星号且不回填原值',field(0,'auth.password').value===''&&field(0,'auth.password').placeholder==='********');
  card(0).querySelector('[data-act="save"]').click();
  await until(()=>qaCalls.some(c=>c.route.endsWith('/sites'))&&!card(0).querySelector('[data-act="save"]').disabled);
  const saved=qaCalls.filter(c=>c.route.endsWith('/sites')).at(-1).body.sites;
  check('Only selected card is saved',saved[1].name==='B');
  check('Other card draft survives save',field(1,'name').value==='Unsaved B');
  check('Replacement has explicit action',saved[0].secret_actions.management_token==='replace');
  check('保存其它配置时星号不会提交或覆盖已存密码',saved[0].auth.password===''&&!saved[0].secret_actions.password&&!JSON.stringify(saved).includes('********'));
  check('Feature override and site policy round trip',qaSites[0].connection.features.wallet==='api_only'&&qaSites[0].connection.mode==='api_preferred');
  // 保存后不该把用户刚填的内容清空（服务端不回填明文，所以由页面会话把值留住）
  check('保存后输入框不再变空、值留着',field(0,'auth.management_token').value==='new-synthetic-management');
  check('保存后沿用用户当时的显示状态',field(0,'auth.management_token').type==='password');
  // 没人点眼睛时不该自己冒出已保存的明文
  // 注意：只能查卡片区域。检查脚本自己是注入到 body 里的，字面量 'qa-secret' 会出现在 body.innerHTML 里。
  check('没人点眼睛时不出现已保存明文',!card(0).innerHTML.includes('qa-secret')&&!card(1).innerHTML.includes('qa-secret'));
  const eyeBtn=()=>card(0).querySelector('[data-act="eye"][data-cred="management_token"]');
  check('令牌输入框带小眼睛',!!eyeBtn());
  check('账户 ID 直接显示、不带小眼睛',!card(0).querySelector('[data-act="eye"][data-cred="account_id"]')
        &&!card(0).querySelector('[data-f="auth.account_id"]').type.includes('password'));
  // 主场景：自己输入/粘贴一段，点眼睛核对是否填对 —— 纯前端，不该产生任何请求
  const secretCallsBefore=qaCalls.filter(c=>c.route.endsWith('/site/secret')).length;
  eyeBtn().click();
  check('点眼睛即可看见明文',field(0,'auth.management_token').type==='text'
        &&field(0,'auth.management_token').value==='new-synthetic-management'&&eyeBtn().getAttribute('aria-pressed')==='true');
  check('切换显示不发任何请求',qaCalls.filter(c=>c.route.endsWith('/site/secret')).length===secretCallsBefore);
  eyeBtn().click();
  check('再点眼睛收回掩码但值不被抹掉',field(0,'auth.management_token').type==='password'
        &&field(0,'auth.management_token').value==='new-synthetic-management');
  // 空框但已保存：点眼睛才去取这一个字段（显式动作）
  field(0,'auth.management_token').value='';
  eyeBtn().click();
  await until(()=>field(0,'auth.management_token').value==='qa-secret-management_token');
  check('空框且已保存时点眼睛取回该字段',field(0,'auth.management_token').type==='text');
  const secretCall=qaCalls.filter(c=>c.route.endsWith('/site/secret')).at(-1);
  check('取凭据的请求只带站点与字段名，不回传明文',!!secretCall&&secretCall.body.field==='management_token'
        &&!JSON.stringify(secretCall.body).includes('qa-secret'));
  // 用户改过之后：点眼睛收回掩码 → 保存 → 重绘后值仍在，且保持"掩码"这个显示状态
  field(0,'auth.management_token').value='qa-typed-token';
  eyeBtn().click();                                  // 当前是显示态 → 点一下收回
  check('收回掩码但值留着（不抹掉）',field(0,'auth.management_token').value==='qa-typed-token'
        &&field(0,'auth.management_token').type==='password');
  const savesBefore=qaCalls.filter(c=>c.route.endsWith('/sites')).length;
  card(0).querySelector('[data-act="save"]').click();
  await until(()=>qaCalls.filter(c=>c.route.endsWith('/sites')).length>savesBefore
                 &&!card(0).querySelector('[data-act="save"]').disabled);
  check('保存+重绘后值仍在，并保持掩码状态',
        field(0,'auth.management_token').value==='qa-typed-token'
        &&field(0,'auth.management_token').type==='password');
  card(0).querySelector('[data-act="clear"][data-cred="management_token"]').click();
  document.getElementById('dialogOk').click();
  check('Explicit clear updates presence state',!hasSiteSecret({auth:{},has_secret:{management_token:true},secret_actions:{management_token:'clear'}},'management_token'));
  check('清空后输入框也空了（会话里不再留着）',field(0,'auth.management_token').value==='');
  check('显式清空后不再显示已保存星号',field(0,'auth.management_token').placeholder!=='********');
  const savesBefore2=qaCalls.filter(c=>c.route.endsWith('/sites')).length;
  card(0).querySelector('[data-act="save"]').click();
  await until(()=>qaCalls.filter(c=>c.route.endsWith('/sites')).length>savesBefore2
                 &&!card(0).querySelector('[data-act="save"]').disabled);
  check('Clear is sent independently from empty keep fields',qaCalls.filter(c=>c.route.endsWith('/sites')).at(-1).body.sites[0].secret_actions.management_token==='clear');
  check('Clear only affects selected secret',qaSites[0].has_secret.management_token===false&&qaSites[0].has_secret.cookie===true);
  check('No remote business actions while editing',!qaCalls.some(c=>/refresh|task\/start|token\//.test(c.route)));
  state.currency='USD';state.rate=7;
  const wallet={site:'a',ok:true,wallet:{scope_id:'a:7',balance_kind:'wallet',unit:'CNY',amount:70}};
  check('Verified CNY wallet converts and deduplicates',acctSummaryHtml([wallet,{...wallet}]).includes('$10.00')&&!acctSummaryHtml([wallet,{...wallet}]).includes('$20.00'));
  check('Legacy unverified money is excluded from totals',!acctSummaryHtml([{site:'a',ok:true,quota_usd:99}]).includes('$99.00'));
  let finished=false;
  const refresh=followAccountRefresh(document.getElementById('acctMsg'),['a','b']).then(()=>{finished=true;});
  await until(()=>window.qaAccountPolls===1&&state.accounts.some(a=>a.site==='a')&&state.server.account_refresh?.running);
  check('慢站未结束时快站金额已显示',!finished&&state.accounts.find(a=>a.site==='a').quota_usd===1
       &&document.querySelector('[data-acct-details="b"]').previousElementSibling.textContent.includes('9.00'));
  check('并发进度显示已完成站点而非正在请求它',document.getElementById('acctMsg').textContent.includes('已处理「B」'));
  await refresh;
  check('后续轮询补齐慢站并保留快站结果',state.accounts.find(a=>a.site==='a').quota_usd===5
       &&state.accounts.find(a=>a.site==='b').quota_usd===9&&document.getElementById('acctMsg').textContent.includes('已刷新 2 个站点'));
  check('No script errors',qaErrors.length===0);
  document.body.dataset.qa='PASS';
 }catch(e){results.push({error:String(e.stack||e)});document.body.dataset.qa='FAIL';}
 const pre=document.createElement('pre');pre.id='qaResult';pre.textContent=JSON.stringify({results,errors:qaErrors});document.body.appendChild(pre);
});
'''
source = (ROOT / "中转站比价.html").read_text(encoding="utf-8")
source = source.replace('<script src="data/prices.js" onerror="window.__DATA_LOAD_FAILED__=true"></script>', "<script>" + bootstrap + "</script>")
source = source.replace("</body>", "<script>" + checks + "</script></body>")
page = scratch / "stage-d-check.html"
page.write_text(source, encoding="utf-8")


proc = subprocess.run([str(EDGE), "--headless=new", "--disable-gpu", "--in-process-gpu", "--no-sandbox",
    "--disable-background-networking", "--no-first-run", "--no-default-browser-check", "--disable-extensions",
    f"--user-data-dir={scratch / 'profile'}", "--window-size=1440,1100", "--virtual-time-budget=10000", "--dump-dom", page.as_uri()],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
dom = proc.stdout.decode("utf-8", "replace")
match = re.search(r'<pre id="qaResult">(.*?)</pre>', dom, re.S)
if not match:
    print(proc.stderr.decode("utf-8", "replace")[-1600:])
    raise SystemExit("No browser result: " + str(scratch))
data = json.loads(html.unescape(match.group(1)))
print(json.dumps(data, ensure_ascii=False, indent=2))
print("Artifacts:", scratch)
raise SystemExit(0 if 'data-qa="PASS"' in dom else 1)
