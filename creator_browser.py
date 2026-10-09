import json, sys, os
from urllib.parse import urlsplit
from pathlib import Path
from playwright.sync_api import sync_playwright
# Use the existing same-environment browser profile internally; never read cookies or storage.
profile=Path('/workspace/p2cos/browser-profile');profile.mkdir(mode=0o700,exist_ok=True)
with sync_playwright() as p:
 context=p.chromium.launch_persistent_context(str(profile), executable_path='/usr/bin/chromium',headless=True,proxy={'server':'http://proxy:8080'},viewport={'width':1440,'height':1100},locale='zh-CN',timezone_id='Asia/Shanghai',args=['--no-sandbox','--disable-dev-shm-usage'])
 for old_page in list(context.pages):
  old_page.close()
 pages={}
 print(json.dumps({'ready':True}),flush=True)
 for line in sys.stdin:
  try:
   c=json.loads(line);name=c.get('page','default')
   if name not in pages:pages[name]=context.new_page()
   page=pages[name];op=c['op']
   if op=='goto':
    response=page.goto(c['url'],wait_until='domcontentloaded',timeout=45000)
    result={'http_status':response.status if response else None,'title':page.title()}
   elif op=='inspect':
    result={'title':page.title(),'text':page.locator('body').inner_text(timeout=10000)[:12000]}
   elif op=='screenshot':
    destination='/workspace/work/browser/'+c['name']
    if 'selector' in c:page.locator(c['selector']).screenshot(path=destination,timeout=10000)
    else:page.screenshot(path=destination,full_page=False,clip=c.get('clip'))
    result={'screenshot':destination}
   elif op=='hover':
    page.locator(c['selector']).nth(c.get('nth',0)).hover(timeout=10000);result={'hovered':True}
   elif op=='element_info':
    result={'ancestors':page.get_by_text(c['text'],exact=True).first.evaluate('(e)=>{let a=[];for(let i=0;e&&i<5;i++,e=e.parentElement)a.push({tag:e.tagName,cls:e.className,attrs:Array.from(e.attributes).map(x=>x.name)});return a}')}
   elif op=='public_note_ids':
    result={'ids':page.locator('[data-note-id],[note-id],[data-noteid]').evaluate_all('(els)=>els.map(e=>({note_id:e.getAttribute("data-note-id")||e.getAttribute("note-id")||e.getAttribute("data-noteid"),text:(e.innerText||"").slice(0,180)}))')}
   elif op=='tabs':
    result={'tabs':[{'index':i,'title':t.title(),'url':urlsplit(t.url).scheme+'://'+urlsplit(t.url).netloc+urlsplit(t.url).path} for i,t in enumerate(context.pages)]}
   elif op=='adopt_tab':
    pages[name]=context.pages[c['index']];result={'adopted':True}
   elif op=='safe_links':
    result={'links':page.locator('a[href]').evaluate_all('(els)=>els.map(e=>{try{let u=new URL(e.href);return {text:(e.innerText||"").slice(0,120),url:u.origin+u.pathname}}catch{return null}}).filter(Boolean)')}
   elif op=='location':
    u=urlsplit(page.url);result={'url':u.scheme+'://'+u.netloc+u.path}
   elif op=='set_files':
    page.locator(c['selector']).set_input_files(c['paths'],timeout=30000);result={'files_selected':len(c['paths'])}
   elif op=='fill':
    page.locator(c['selector']).nth(c.get('nth',0)).fill(c['text'],timeout=10000);result={'filled':True}
   elif op=='press':
    page.locator(c['selector']).nth(c.get('nth',0)).press(c['key'],timeout=10000);result={'pressed':True}
   elif op=='dom_controls':
    result={'elements':page.locator('button,a,input,textarea,[contenteditable=true],[role=button]').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,text:(e.innerText||e.placeholder||e.getAttribute("aria-label")||"").slice(0,120),type:e.getAttribute("type"),className:e.className,name:e.getAttribute("name"),role:e.getAttribute("role")}))')}
   elif op=='click_xy':
    page.mouse.click(c['x'],c['y']);result={'clicked':True}
   elif op=='controls':
    result={'controls':page.locator('button,a,input,[role=button]').evaluate_all('(els)=>els.map(e=>({tag:e.tagName,text:(e.innerText||e.placeholder||e.getAttribute("aria-label")||"").slice(0,120),type:e.getAttribute("type")}))')}
   elif op=='click_text':
    page.get_by_text(c['text'],exact=c.get('exact',True)).nth(c.get('nth',0)).click(timeout=10000)
    result={'clicked':True}
   elif op=='click':
    page.locator(c['selector']).nth(c.get('nth',0)).click(timeout=10000);result={'clicked':True}
   elif op=='close':
    context.close();break
   else:result={'error':'unknown operation'}
   print(json.dumps({'page':name,'op':op,'result':result},ensure_ascii=False),flush=True)
  except Exception as e:
   print(json.dumps({'error_type':type(e).__name__},ensure_ascii=False),flush=True)
