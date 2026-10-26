"""真实 Edge 的 Key/CCS 流程回归：仅合成接口，禁止远端写入及实际唤起。"""
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
scratch = Path(tempfile.mkdtemp(prefix="relay-key-ccs-browser-"))
bootstrap = r'''
window.qaCalls=[];window.qaErrors=[];window.qaOpened=[];window.qaCopied=[];
window.qaTokenName='工作 Key 😀&?#"/';
window.qaSite={id:'fixture',name:'中转&站「甲」',type:'sub2api',enabled:true,base_url:'https://fixture.invalid'};
window.qaAccount={site:'fixture',site_name:qaSite.name,ok:true,group:'default',group_id:7,tokens:[],group_keys:{},keys_complete:true,token_count:0};
window.addEventListener('error',e=>qaErrors.push(e.message));
window.addEventListener('unhandledrejection',e=>qaErrors.push(String(e.reason)));
HTMLAnchorElement.prototype.click=function(){
 if(this.href.startsWith('ccswitch:'))qaOpened.push(this.href);
 else throw new Error('Unexpected navigation');
};
window.fetch=async(url,init={})=>{
 const route=new URL(url,location.href).pathname,body=init.body?JSON.parse(init.body):{};
 qaCalls.push({route,body});let result;
 const wait=async name=>{while(!window[name])await new Promise(r=>setTimeout(r,10));};
 if(route.endsWith('/state'))result={ok:true,mode:'server',sites:[qaSite],settings:{currency:'USD',auto_refresh_model_health:false},accounts:{accounts:[qaAccount]},schedule:{enabled:false},task:{running:false}};
 else if(route.endsWith('/index'))result={ok:true,models:[],sites:[qaSite],model_count:0,offer_count:0,site_count:1};
 else if(route.endsWith('/token/create')){
  await wait('qaReleaseCreate');result={ok:true,token:{id:42,name:qaTokenName,key:'sk-synthetic-key-42',group:'default',group_id:7}};
 }else if(route.endsWith('/export/ccswitch')){
  await wait('qaReleaseExport');
  if(window.qaExportError)throw new Error('synthetic export error');
  const tokenId=body.token_id||99,name=qaSite.name+'-'+qaTokenName+'-'+body.model;
  const link='ccswitch://v1/import?'+new URLSearchParams({name,model:body.model,apiKey:'synthetic-export-key'});
  result={ok:true,link,token_id:tokenId,token_name:qaTokenName,created:body.create,app_name:'Codex',model:body.model,endpoint:'https://fixture.invalid/v1',group:'default',warn:[]};
  if(body.create){
   qaAccount=JSON.parse(JSON.stringify(state.accounts[0]));
   qaAccount.tokens.push({id:99,name:qaTokenName,group:'default',group_id:7,enabled:true,expired_time:-1});
   qaAccount.token_count=qaAccount.tokens.length;result.account=qaAccount;
  }
 }else throw new Error('Unexpected route: '+route);
 return {ok:true,status:200,json:async()=>JSON.parse(JSON.stringify(result))};
};
'''
checks = r'''
// 测试专用：浏览器直接打开 file:// 时只允许探活；这里按服务托管页面继续验证交互。
window.isFilePage = () => false;
document.addEventListener('DOMContentLoaded',async()=>{
 const results=[];
 const check=(name,ok)=>{results.push({name,pass:!!ok});if(!ok)throw new Error(name);};
 const until=async fn=>{for(let i=0;i<300;i++){if(fn())return;await new Promise(r=>setTimeout(r,10));}throw new Error('Timed out');};
 const count=route=>qaCalls.filter(c=>c.route.endsWith(route)).length;
 const offer={site:'fixture',site_name:qaSite.name,group:'default',group_id:7,model_raw:'gpt/模型-β?x=1&y=2',input:1,output:2,group_ratio:1};
 const model={key:'fixture',display:'测试模型',offers:[offer],site_count:1,aliases:[]};
 try{
  await until(()=>state.mode==='server'&&count('/index')===1);
  await new Promise(r=>setTimeout(r,20));
  copyText=async text=>{qaCopied.push(text);return true;};
  state.detailCache.set('fixture',model);state.detailCache.set('other',{...model,key:'other',display:'其他模型'});
  await openModelDetail('fixture');
  document.querySelector('[data-act="offer-toggle"]').click();
  document.getElementById('offerApp0').value='codex';document.getElementById('offerUsage0').checked=false;
  document.querySelector('[data-act="makekey"]').click();
  check('创建保留输入名称确认框',document.getElementById('dialogTitle').textContent==='创建 Key');
  const input=document.getElementById('confirmInput');input.value=qaTokenName;input.dispatchEvent(new Event('input'));
  document.getElementById('dialogOk').click();document.getElementById('dialogOk').click();
  await until(()=>count('/token/create')===1);
  check('重复确认只发送一次创建请求',count('/token/create')===1);
  check('创建携真实group_id和原始Unicode名称',qaCalls.at(-1).body.group_id===7&&qaCalls.at(-1).body.name===qaTokenName);
  qaReleaseCreate=true;
  await until(()=>!!document.querySelector('[data-created-import]'));
  check('创建成功自动复制完整Key且仅一次',qaCopied.length===1&&qaCopied[0]==='sk-synthetic-key-42');
  check('局部缓存保留真实group_id',state.accounts[0].tokens[0].group_id===7);
  check('创建后保留展开状态和导入选项',!document.getElementById('offerDetails0').hidden&&document.getElementById('offerApp0').value==='codex'&&!document.getElementById('offerUsage0').checked);
  check('创建后未额外刷新账户或健康',count('/accounts')===0&&!qaCalls.some(c=>/health|refresh/.test(c.route)));
  check('创建后仍需确认导入',qaOpened.length===0&&document.getElementById('dialogTitle').textContent==='Key 已创建并复制');
  document.getElementById('dialogOk').click();
  const listImport=document.querySelector('[data-act="offer-import"]');listImport.click();
  await until(()=>count('/export/ccswitch')===1);
  check('创建结果与列表重复入口合并在途请求',count('/export/ccswitch')===1);
  let call=qaCalls.filter(c=>c.route.endsWith('/export/ccswitch')).at(-1);
  check('创建后导入只指定新Key且create=false',call.body.token_id==='42'&&call.body.create===false&&call.body.group_id===7);
  check('前端不覆盖服务端导入名称',!('name' in call.body)&&!('key_name' in call.body));
  qaReleaseExport=true;
  await until(()=>qaOpened.length===1);
  check('CCS名称与服务端中文emoji特殊字符完整一致',new URL(qaOpened[0]).searchParams.get('name')===qaSite.name+'-'+qaTokenName+'-'+offer.model_raw);
  check('完成导入即显示结果不等待额外账户请求',document.getElementById('offerStatus0').textContent.includes('已请求打开')&&count('/accounts')===0);
  // 模拟旧页面的无token入口：后端返回新增Key及本地快照，前端不再发全量刷新。
  const auto=document.createElement('button');auto.dataset.offer='0';auto.textContent='自动建Key导入';document.getElementById('offerStatus0').appendChild(auto);
  await importOffer(auto);
  check('自动创建导入成功回填token_id便于再次复用',auto.dataset.token==='99'&&auto.dataset.hasCreated==='1');
  check('使用本地账户快照更新Key列表且不额外GET',state.accounts[0].tokens.some(t=>t.id===99)&&count('/accounts')===0);
  check('自动创建导入保留展开状态及选项',!document.getElementById('offerDetails0').hidden&&document.getElementById('offerApp0').value==='codex'&&!document.getElementById('offerUsage0').checked);
  const countBefore=count('/export/ccswitch'),openedBefore=qaOpened.length;
  qaReleaseExport=false;
  const pending=importOffer(document.querySelector('[data-act="offer-import"][data-token="99"]'));
  await until(()=>count('/export/ccswitch')===countBefore+1);
  await openModelDetail('other');qaReleaseExport=true;await pending;
  check('切换模型后旧导入不自动唤起CCS',qaOpened.length===openedBefore);
  check('切换模型后旧导入不改写新页面',!document.getElementById('offerStatus0').textContent.includes('已请求打开'));
  await openModelDetail('fixture');qaExportError=true;
  await importOffer(document.querySelector('[data-act="offer-import"]'));
  check('导入失败提供反馈并恢复按钮',document.getElementById('offerStatus0').textContent.includes('synthetic export error')&&!document.querySelector('[data-act="offer-import"]').disabled);
  check('全部操作无额外健康和全量刷新请求',!qaCalls.some(c=>/health|refresh|task\//.test(c.route))&&count('/accounts')===0);
  check('浏览器无未处理错误',qaErrors.length===0);
  document.body.dataset.qa='PASS';
 }catch(e){results.push({error:String(e.stack||e)});document.body.dataset.qa='FAIL';}
 const pre=document.createElement('pre');pre.id='qaResult';pre.textContent=JSON.stringify({results,errors:qaErrors});document.body.appendChild(pre);
});
'''
source = (ROOT / "中转站比价.html").read_text(encoding="utf-8")
source = source.replace('<script src="data/prices.js" onerror="window.__DATA_LOAD_FAILED__=true"></script>', '<script>' + bootstrap + '</script>')
source = source.replace('</body>', '<script>' + checks + '</script></body>')
page = scratch / 'key-ccs-check.html'
page.write_text(source, encoding='utf-8')
proc = subprocess.run([str(EDGE), '--headless=new', '--disable-gpu', '--in-process-gpu', '--no-sandbox',
    '--disable-background-networking', '--no-first-run', '--no-default-browser-check', '--disable-extensions',
    f'--user-data-dir={scratch / "profile"}', '--window-size=1440,1100', '--virtual-time-budget=10000', '--dump-dom', page.as_uri()],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
dom = proc.stdout.decode('utf-8', 'replace')
match = re.search(r'<pre id="qaResult">(.*?)</pre>', dom, re.S)
if not match:
    print(proc.stderr.decode('utf-8', 'replace')[-1600:])
    raise SystemExit('No browser result: ' + str(scratch))
data = json.loads(html.unescape(match.group(1)))
print(json.dumps(data, ensure_ascii=False, indent=2))
print('Artifacts:', scratch)
raise SystemExit(0 if 'data-qa="PASS"' in dom else 1)
