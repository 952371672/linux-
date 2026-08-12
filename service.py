#!/usr/bin/env python3
"""CMCC Linux v127-style keepalive service.

Execution order: resident probe scheduler -> local SDK/connectWorker -> ordinary
CDP click fallback.  The WebUI exposes every stage so a stuck account is visible.
"""
from __future__ import annotations
import asyncio, csv, io, json, os, random, re, secrets, subprocess, time, urllib.request, base64, contextvars, hmac, hashlib, uuid, concurrent.futures, threading
from urllib.request import Request as UrlRequest, urlopen
from cryptography.hazmat.primitives import serialization
from pathlib import Path
from typing import Any
import websocket
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

ROOT=Path(os.environ.get("CMCC_DATA_DIR","/data")); ROOT.mkdir(parents=True,exist_ok=True)
ACCOUNTS=ROOT/"accounts.json"; EVENTS=ROOT/"events.jsonl"; PROFILES=ROOT/"profiles"; PROFILES.mkdir(exist_ok=True)
SECRET=ROOT/".secret"
if os.environ.get("CMCC_SECRET"):
    _fernet=Fernet(os.environ["CMCC_SECRET"].encode())
else:
    if not SECRET.exists(): SECRET.write_bytes(Fernet.generate_key()); SECRET.chmod(0o600)
    _fernet=Fernet(SECRET.read_bytes())
app=FastAPI(title="CMCC Linux v127 Keepalive",version="1.4.0")
WEBUI_USER=os.environ.get("CMCC_WEBUI_USER","").strip()
WEBUI_PASSWORD=os.environ.get("CMCC_WEBUI_PASSWORD","")
WEBUI_REALM=os.environ.get("CMCC_WEBUI_REALM","CMCC Keepalive")
def auth_credentials():
    p=ROOT/"webui-auth.json"
    try:
        if p.exists():
            x=json.loads(p.read_text("utf-8")); return str(x.get("username",WEBUI_USER)),str(x.get("password",WEBUI_PASSWORD))
    except Exception: pass
    return WEBUI_USER,WEBUI_PASSWORD
class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self,request,call_next):
        expected_user,expected_password=auth_credentials()
        if not expected_user or not expected_password:
            return PlainTextResponse("WebUI authentication is not configured",status_code=503)
        value=request.headers.get("authorization","")
        user=password=scheme=""
        try:
            scheme,encoded=value.split(" ",1)
            raw=base64.b64decode(encoded).decode("utf-8")
            user,password=raw.split(":",1)
        except Exception: pass
        if scheme.lower()!="basic" or not hmac.compare_digest(user,expected_user) or not hmac.compare_digest(password,expected_password):
            return PlainTextResponse("Authentication required",status_code=401,headers={"WWW-Authenticate":f'Basic realm="{WEBUI_REALM}"'})
        return await call_next(request)
app.add_middleware(BasicAuthMiddleware)
app.mount("/webui", StaticFiles(directory="/opt/cmcc-app/webui", html=True), name="webui")
tasks:dict[str,asyncio.Task]={}
stopped_accounts:set[str]=set()
states:dict[str,dict[str,Any]]={}; account_locks:dict[str,asyncio.Lock]={}; scheduler_task=None
# Cross-entrypoint single-flight guard. The per-worker lock prevents duplicate
# rounds inside one worker; this set also covers manual/realtime workers that
# overlap while an old task is being cancelled.
active_run_keys:set[str]=set()
active_clients:dict[str,tuple[Any,Any]]={}
probe_last:dict[str,tuple[str,float]]={}
probe_initialized:set[str]=set()
# Keep all six fixed recovery slots for simultaneous real outages.  Cooldown is
# account-scoped and only prevents an account that just failed recovery from
# immediately monopolising a slot again.
recovery_cooldown_until:dict[str,float]={}
recovery_failure_streak:dict[str,int]={}
RECOVERY_FAILURE_COOLDOWN=60.0
PROBE_INTERVAL=10; PROBE_CONSECUTIVE=2
RUNTIME_CONFIG=ROOT/"runtime-config.json"
CONFIG_LOCK=threading.Lock()
def _load_runtime_config():
    try:
        x=json.loads(RUNTIME_CONFIG.read_text("utf-8"))
        return {"probe_interval":max(5,min(300,int(x.get("probe_interval",10)))),"fallback_concurrency":max(1,min(8,int(x.get("fallback_concurrency",6))))}
    except Exception:return {"probe_interval":10,"fallback_concurrency":6}
_runtime_config=_load_runtime_config()
PROBE_INTERVAL=_runtime_config["probe_interval"]
FALLBACK_CONCURRENCY=min(6,_runtime_config["fallback_concurrency"])
# Fixed six-slot compatibility mode matching the earlier release.
CLIENT_SLOTS=(
    {"name":"slot0","display":":100","port":9223},
    {"name":"slot1","display":":101","port":9224},
    {"name":"slot2","display":":102","port":9225},
    {"name":"slot3","display":":103","port":9226},
    {"name":"slot4","display":":104","port":9227},
    {"name":"slot5","display":":105","port":9228},
)
class DynamicSlotLimiter:
    def __init__(self, limit):
        self.limit=max(1,min(len(CLIENT_SLOTS),int(limit))); self.active=0; self.condition=asyncio.Condition()
    async def acquire(self):
        async with self.condition:
            await self.condition.wait_for(lambda: self.active < self.limit)
            self.active += 1
    async def release(self):
        async with self.condition:
            self.active=max(0,self.active-1); self.condition.notify_all()
    async def __aenter__(self): await self.acquire(); return self
    async def __aexit__(self,*args): await self.release()
    def set_limit(self,value): self.limit=max(1,min(len(CLIENT_SLOTS),int(value)))

fallback_slots=DynamicSlotLimiter(FALLBACK_CONCURRENCY)
fallback_active:set[str]=set()
account_slots:dict[str,dict[str,Any]]={}
slot_pool=asyncio.Queue()
current_slot=contextvars.ContextVar("cmcc_current_slot",default=CLIENT_SLOTS[0])
slot_runtime:dict[str,dict[str,Any]]={}

def create_slot_runtime(slot):
    """Fixed-slot compatibility: display servers are started by entrypoint."""
    return

def destroy_slot_runtime(slot):
    # Fixed display servers remain resident across account runs.
    return

class AccountIn(BaseModel):
    username:str=Field(min_length=1,max_length=200); password:str=Field(min_length=1,max_length=300)
    login_mode:str="main"; name:str|None=None; connect_timeout:int=Field(default=120,ge=20,le=300)
class Action(BaseModel): action:str
class ImportIn(BaseModel): text:str=Field(min_length=1,max_length=2_000_000); login_mode:str="main"; replace:bool=False

def read_accounts():
    if not ACCOUNTS.exists(): return {}
    try:return json.loads(ACCOUNTS.read_text("utf-8"))
    except Exception:return {}
def write_accounts(x):
    t=ACCOUNTS.with_suffix(".tmp"); t.write_text(json.dumps(x,ensure_ascii=False,indent=2),"utf-8"); t.replace(ACCOUNTS); ACCOUNTS.chmod(0o600)
EVENTS_MAX_BYTES=int(os.environ.get("CMCC_EVENTS_MAX_BYTES","52428800"))
EVENTS_BACKUPS=max(1,int(os.environ.get("CMCC_EVENTS_BACKUPS","5")))
_events_lock=threading.Lock()
_events_size_check=0

def _append_event_line(line):
    """Append JSONL with bounded rotation; safe for concurrent worker threads."""
    global _events_size_check
    with _events_lock:
        # Check the size before writing so the active file never grows without a bound.
        try: size=EVENTS.stat().st_size
        except FileNotFoundError: size=0
        if size and size+len(line.encode("utf-8"))>EVENTS_MAX_BYTES:
            oldest=Path(str(EVENTS)+f".{EVENTS_BACKUPS}")
            try: oldest.unlink()
            except FileNotFoundError: pass
            for n in range(EVENTS_BACKUPS-1,0,-1):
                src=Path(str(EVENTS)+f".{n}"); dst=Path(str(EVENTS)+f".{n+1}")
                if src.exists(): src.replace(dst)
            EVENTS.replace(Path(str(EVENTS)+".1"))
        with EVENTS.open("a",encoding="utf-8", buffering=1) as f:
            f.write(line); f.flush()
        _events_size_check += 1

def event(key,status,**extra):
    x={"time":time.strftime("%Y-%m-%d %H:%M:%S"),"account":key,"status":status,**extra}; states[key]=x
    # Keep a redacted, bounded page snapshot for WebUI diagnostics. Never store
    # input values, passwords, tokens, cookies, or full HTML.
    if "page_snapshot" in x:
        snap=x["page_snapshot"]
        if isinstance(snap,dict):
            snap={k:v for k,v in snap.items() if k in ("url","title","body","inputs","buttons","target_count")}
            snap["body"]=str(snap.get("body", ""))[:2200]
            x["page_snapshot"]=snap
    _append_event_line(json.dumps(x,ensure_ascii=False)+"\n")
def normalize_login_mode(value):
    v=str(value or '').strip().lower()
    return "sub" if v in ("sub","subaccount","sub_account","sub_password","sub_login","子账号","子帐号","子账号登录") else "main"

def put_account(username,password,login_mode="main",name=None):
    d=read_accounts(); k=secrets.token_hex(8)
    mode=normalize_login_mode(login_mode)
    # New accounts remain idle until the user explicitly clicks Start.
    d[k]={"name":name or username,"username":username,"login_mode":mode,"connect_timeout":120,"autostart":False,"password":_fernet.encrypt(password.encode()).decode()}
    write_accounts(d); return k
def decrypt(v): return _fernet.decrypt(v.encode()).decode()
PROBE_STRATEGY={"name":"prod-https-2.23","base_url":"https://soho.komect.com","app_key":"b866539514246c187171f759ff409de25149407fcdada3c678a0c39c233cefb1","app_secret":"b5630ba3e5e95defd08306b2c1069c8b4b791098d726f107ad747a216f57eaf5","client_version":"2.23.1","version_num":"2230100","fallback_app_type":"windows|2.23.1|windows|0|-1|2230100|","fallback_rom":"windows-2.23.1","user_agent":f"jtydn-Windows-2.23.1(1.dd2313e.{time.strftime('%m%d')})"}
def _probe_tokens(profile):
    out={"tokens":[],"device_ids":[],"app_types":[],"rom_versions":[],"user_ids":[],"client_version":"","version_num":""}
    def add(k,v):
        v=str(v or '').strip()
        if v and v not in out[k]: out[k].append(v)
    p=Path(profile); files=[]
    cfg=p/'config.json'
    if cfg.is_file():
        try:
            x=json.loads(cfg.read_text('utf-8'))
            add('tokens',x.get('sohoToken') or x.get('SohoToken') or x.get('X-SOHO-SohoToken'))
            add('device_ids',x.get('deviceId') or x.get('X-SOHO-DeviceId')); add('app_types',x.get('appType') or x.get('X-SOHO-AppType'))
            add('rom_versions',x.get('romVersion') or x.get('X-SOHO-RomVersion')); add('user_ids',x.get('userId') or x.get('X-SOHO-UserId'))
            out['client_version']=str(x.get('clientVersion') or x.get('chuanyunVersion') or '').lstrip('Vv'); out['version_num']=str(x.get('versionNum') or '')
        except Exception: pass
    for rel in ('Default/Local Storage/leveldb','Default/Session Storage','Default/IndexedDB','Default/Preferences','Local State'):
        q=p/rel
        if q.is_dir():
            for f in q.rglob('*'):
                try:
                    if f.is_file() and f.stat().st_size<=8*1024*1024: files.append(f)
                except Exception: pass
        elif q.is_file(): files.append(q)
    pats={'tokens':(r'(?:X-SOHO-)?SohoToken["\'\s:=]+([A-Za-z0-9._-]{8,})',),'device_ids':(r'(?:X-SOHO-)?DeviceId["\'\s:=]+([^"\'\x00\r\n,}]{4,120})',),'app_types':(r'(?:X-SOHO-)?AppType["\'\s:=]+([^"\'\x00\r\n,}]{8,240})',),'rom_versions':(r'(?:X-SOHO-)?RomVersion["\'\s:=]+([^"\'\x00\r\n,}]{4,120})',)}
    for f in files[:300]:
        try: txt=f.read_bytes().decode('utf-8','ignore')
        except Exception: continue
        for k,ps in pats.items():
            for pat in ps:
                for m in re.finditer(pat,txt,re.I): add(k,m.group(1))
    return out
def _probe_sig(method,path,h,b=''):
    text=method+'&'+path+'&'+'&'.join(f'{k}={v}' for k,v in h.items() if v)
    if b not in (None,'',{}):
        bj=json.dumps(b,ensure_ascii=False,separators=(',',':'))
        text += '&body=' + (b.get('data','') if isinstance(b,dict) else bj)
    return hmac.new(bytes.fromhex(PROBE_STRATEGY['app_secret']),text.encode(),hashlib.sha256).hexdigest()
def _rsa_probe(obj,pub):
    pem=('-----BEGIN PUBLIC KEY-----\n'+str(pub).strip().replace('\r','').replace('\n','')+'\n-----END PUBLIC KEY-----\n').encode(); key=serialization.load_pem_public_key(pem); n=key.public_numbers(); size=(n.n.bit_length()+7)//8; raw=json.dumps(obj,ensure_ascii=False,separators=(',',':')).encode(); out=[]
    for i in range(0,len(raw),size-11):
        part=raw[i:i+size-11]; out.append(pow(int.from_bytes(b'\0'*(size-len(part))+part,'big'),n.e,n.n).to_bytes(size,'big'))
    return base64.b64encode(b''.join(out)).decode()
def _probe_request(endpoint,tok,body=None):
    s=PROBE_STRATEGY; path=endpoint.replace('/terminal','',1); url=s['base_url']+'/terminal'+path; ts=str(int(time.time()*1000)); token=(tok.get('tokens') or [''])[0]; h={'X-SOHO-AppKey':s['app_key'],'X-SOHO-AppType':(tok.get('app_types') or [s['fallback_app_type']])[0],'X-SOHO-ClientVersion':s['client_version'],'X-SOHO-DeviceId':(tok.get('device_ids') or [s['version_num']])[0],'X-SOHO-RomVersion':(tok.get('rom_versions') or [s['fallback_rom']])[0],'X-SOHO-SohoToken':token,'X-SOHO-Timestamp':ts,'X-SOHO-UserId':(tok.get('user_ids') or [''])[0],'X-SOHO-Uuid':uuid.uuid4().hex,'X-SOHO-VersionNum':s['version_num']}
    def post(u,raw,hh,timeout):
        with urlopen(UrlRequest(u,data=raw,headers=hh,method='POST'),timeout=timeout) as r:return json.loads(r.read(1024*1024).decode('utf-8','ignore'))
    pk_data=post(s['base_url']+'/terminal/login/encryptKey/v1',b'',{'Content-Type':'application/json','User-Agent':s['user_agent'],**h,'X-SOHO-Signature':_probe_sig('POST','/login/encryptKey/v1',h)},6)
    pk=pk_data.get('data','') if isinstance(pk_data,dict) and pk_data.get('code')==2000 else ''
    if body is None: raw=b''; signed=''
    else: signed={'data':_rsa_probe(body,pk)}; raw=json.dumps(signed,ensure_ascii=False,separators=(',',':')).encode()
    # The server rejects reusing the encryptKey request nonce. Windows creates a fresh
    # timestamp/UUID/header set for the business request, so do the same.
    ts2=str(int(time.time()*1000)); h=dict(h); h['X-SOHO-Timestamp']=ts2; h['X-SOHO-Uuid']=uuid.uuid4().hex
    h['X-SOHO-Signature']=_probe_sig('POST',path,h,signed if body is not None else '')
    return post(url,raw,{'Content-Type':'application/json','User-Agent':s['user_agent'],**h},6)
def _rsa_password(password,pub):
    pem=('-----BEGIN PUBLIC KEY-----\n'+str(pub).strip().replace('\r','').replace('\n','')+'\n-----END PUBLIC KEY-----\n').encode(); key=serialization.load_pem_public_key(pem); n=key.public_numbers(); size=(n.n.bit_length()+7)//8; raw=str(password).encode('utf-8')
    if len(raw)>size: raise ValueError('password too long')
    return base64.b64encode(pow(int.from_bytes(b'\0'*(size-len(raw))+raw,'big'),n.e,n.n).to_bytes(size,'big')).decode()
def _write_login_profile(profile,data,username,mode,tok):
    p=Path(profile); p.mkdir(parents=True,exist_ok=True); cfg={}
    try: cfg=json.loads((p/'config.json').read_text('utf-8'))
    except Exception: pass
    d=data.get('data') if isinstance(data,dict) and isinstance(data.get('data'),dict) else {}
    cfg.update({'sohoToken':d.get('sohoToken') or d.get('SohoToken') or cfg.get('sohoToken',''),'userId':d.get('userId') or cfg.get('userId',''),'username':d.get('username') or d.get('subAccount') or username,'isSubAccount':normalize_login_mode(mode)=='sub','isLogined':True,'deviceId':(tok.get('device_ids') or [cfg.get('deviceId') or ''])[0],'appType':(tok.get('app_types') or [cfg.get('appType') or PROBE_STRATEGY['fallback_app_type']])[0],'romVersion':(tok.get('rom_versions') or [cfg.get('romVersion') or PROBE_STRATEGY['fallback_rom']])[0],'clientVersion':PROBE_STRATEGY['client_version'],'versionNum':PROBE_STRATEGY['version_num']})
    (p/'config.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),'utf-8'); return cfg
def _refresh_probe_login(key,cfg,tok):
    try:
        username=str(cfg.get('username','')).strip(); password=decrypt(cfg.get('password',''))
        mode=cfg.get('login_mode','main'); sub=normalize_login_mode(mode)=='sub'
        pk=_probe_request('/terminal/login/publicKey/v1',tok,{'type':1}); public=pk.get('data','') if isinstance(pk,dict) and pk.get('code')==2000 else ''
        if not public:return False,'获取登录公钥失败'
        ep='/terminal/login/home/namePwdLogin/v1' if sub else '/terminal/login/namePwdLogin/v1'; body={('subAccount' if sub else 'username'):username,'password':_rsa_password(password,public),'verificationCode':'','randomCode':''}
        result=_probe_request(ep,tok,body)
        if not isinstance(result,dict) or result.get('code')!=2000:return False,f'登录返回code={result.get("code") if isinstance(result,dict) else "unknown"}'
        saved=_write_login_profile(PROFILES/key,result,username,mode,tok); return bool(saved.get('sohoToken')),'协议登录成功'
    except Exception as e:return False,f'{type(e).__name__}: {str(e)[:120]}'
def probe_account(key,cfg):
    tok=_probe_tokens(PROFILES/key)
    if not tok or not tok.get('tokens'): return {'class':'unknown','reason':'缺少SohoToken'}
    try:
        # The official v127 client performs collectInfo(2/1), then 3 and 4
        # after login. Without this post-login initialization the same token
        # can be rejected by list/sublist as 4015 even though it is present.
        if key not in probe_initialized:
            collect_type='2' if normalize_login_mode(cfg.get('login_mode'))=='sub' else '1'
            init=[]
            for ct in (collect_type,'3','4'):
                r=_probe_request('/terminal/cc/collectInfo/v1',tok,{'type':ct,'collectMethod':'1'})
                init.append(str(r.get('code')) if isinstance(r,dict) else 'bad')
                if not isinstance(r,dict) or r.get('code')!=2000: break
            probe_initialized.add(key)
            if init and init[-1] != '2000':
                return {'class':'unknown','reason':'postInit collectInfo='+','.join(init)}
        # list and sublist are independent status reads. Run them in parallel so
        # slow response from one account route does not add a second full RTT to
        # every probe round. Each request builds its own nonce/signature.
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fa=pool.submit(_probe_request,'/terminal/cc/cloudPc/list/v6',tok,{'pageNum':1})
            fb=pool.submit(_probe_request,'/terminal/cc/cloudPc/sublist/v3',tok,{'pageNum':1})
            a=fa.result(); b=fb.result()
        if any(isinstance(z,dict) and z.get('code')==4015 for z in (a,b)):
            ok,reason=_refresh_probe_login(key,cfg,tok)
            if not ok:return {'class':'unknown','reason':'4015刷新登录失败：'+reason}
            tok=_probe_tokens(PROFILES/key)
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                fa=pool.submit(_probe_request,'/terminal/cc/cloudPc/list/v6',tok,{'pageNum':1})
                fb=pool.submit(_probe_request,'/terminal/cc/cloudPc/sublist/v3',tok,{'pageNum':1})
                a=fa.result(); b=fb.result()
        items=[]
        for z in (a,b):
            if not isinstance(z,dict) or z.get('code') != 2000: continue
            d=z.get('data'); ls=[]
            if isinstance(d,dict):
                if isinstance(d.get('list'),list): ls=d.get('list') or []
                elif isinstance(d.get('data'),list): ls=d.get('data') or []
                elif isinstance(d.get('data'),dict) and isinstance(d['data'].get('list'),list): ls=d['data'].get('list') or []
            elif isinstance(d,list): ls=d
            items += [x for x in ls if isinstance(x,dict)]
        if not items:return {'class':'unknown','reason':'未返回云电脑条目'}
        classes=[]; details=[]
        for x in items:
            service=x.get('serviceStatus'); expired=x.get('expiredStatus'); vm=x.get('vmStatus'); vals=','.join(f'{k}={x.get(k)}' for k in ('vmStatus','serviceStatus','expiredStatus','powerStatus','onlineStatus','connectStatus','status') if x.get(k) is not None)
            if expired not in (None,1,'1'): cls='need'
            elif service not in (None,1,'1'): cls='need'
            elif vm in (1,'1',21,'21',25,'25'): cls='maybe_skip'
            elif vm in (23,'23'): cls='suspect'
            elif any(x.get(k) is not None for k in ('vmStatus','powerStatus','onlineStatus','connectStatus','status')): cls='need'
            else: cls='unknown'
            classes.append(cls); details.append(f"{x.get('userServiceId') or x.get('vmId') or 'unknown'}:{vals}")
        cls='need' if 'need' in classes else ('suspect' if 'suspect' in classes else ('unknown' if 'unknown' in classes else 'maybe_skip'))
        return {'class':cls,'reason':'; '.join(details[:5]),'vmStatus':items[0].get('vmStatus'),'items':len(items),'strong_shutdown':bool(any(x.get('vmStatus') in (23,'23') for x in items) and any(x.get(k) not in (None,1,'1') for x in items for k in ('serviceStatus','powerStatus','onlineStatus','connectStatus')))}
    except Exception as e:return {'class':'unknown','reason':f'{type(e).__name__}: {str(e)[:160]}'}
def http_json(url):
    with urllib.request.urlopen(url,timeout=5) as r:return json.loads(r.read())
def page_targets(port=None):
    port=port or current_slot.get()["port"]
    try:return http_json(f"http://127.0.0.1:{port}/json/list")
    except Exception:return []
def page_target(port=None):
    """Pick the newest usable Electron page, preferring the authenticated app target."""
    xs=page_targets(port)
    usable=[x for x in xs if x.get("webSocketDebuggerUrl") and x.get("type") in ("page","webview")]
    if not usable:
        usable=[x for x in xs if x.get("webSocketDebuggerUrl") and x.get("url","").startswith("file:")]
    # Electron can leave an old target beside the new renderer during navigation.
    # Prefer the newest target and the app shell/business route over start.html.
    usable.sort(key=lambda x:("#/home" in str(x.get("url",'')) or "#/login" in str(x.get("url",'')), "start.html" not in str(x.get("url",'')), str(x.get("id",''))),reverse=True)
    return usable[0] if usable else None
def kill_profile_clients(profile):
    """Kill the complete stale Electron tree owning one persisted profile.

    Chromium puts --user-data-dir only on some child processes; killing only
    matching command lines leaves the main process alive and produces the
    misleading situation where noVNC shows a desktop but CDP has no page.
    """
    try:
        raw=subprocess.check_output(["ps","-eo","pid=,ppid=,args="],text=True,stderr=subprocess.DEVNULL)
        rows=[]
        for line in raw.splitlines():
            parts=line.strip().split(None,2)
            if len(parts)==3:
                try: rows.append((int(parts[0]),int(parts[1]),parts[2]))
                except Exception: pass
        by_pid={p:(pp,cmd) for p,pp,cmd in rows}
        # Match the exact profile first, regardless of whether the process
        # command line still contains the vendor executable name. The wrapper
        # can leave an Electron main process whose argv is shortened, causing
        # the old cmcc-jtydn-only filter to miss the singleton owner.
        ids={p for p,pp,cmd in rows if profile in cmd}
        changed=True
        while changed:
            changed=False
            for p,(pp,cmd) in by_pid.items():
                # Include every descendant of the exact-profile owner, then
                # include related Electron/vendor ancestors. This is scoped to
                # one account profile and does not kill other accounts.
                if p in ids or pp in ids:
                    if p not in ids: ids.add(p); changed=True
                if p in ids and pp in by_pid and pp not in ids:
                    parent_cmd=by_pid[pp][1]
                    if "cmcc-jtydn" in parent_cmd or "electron" in parent_cmd.lower():
                        ids.add(pp); changed=True
        # Also remove a stale client that still owns our fixed CDP port. It is
        # safe here because the service uses one isolated display/client slot;
        # the profile match above remains the primary account boundary.
        # Port-scoped cleanup is handled by the slot owner; never kill another
        # account's renderer from this profile cleanup routine.
        for sig in (15,9):
            for pid in sorted(ids,reverse=True):
                try: os.kill(pid,sig)
                except ProcessLookupError: pass
                except Exception: pass
            time.sleep(.7)
    except Exception:
        # Best-effort cleanup; the subsequent CDP wait remains authoritative.
        pass
def clear_stale_profile_locks(profile):
    """Remove Chromium Singleton* links only after the client tree is gone."""
    try:
        pth=Path(profile)
        for name in ("SingletonLock","SingletonCookie","SingletonSocket"):
            q=pth/name
            try:
                if q.is_symlink() or q.exists(): q.unlink()
            except FileNotFoundError: pass
            except Exception: pass
    except Exception: pass

def kill_port_owner(port):
    """Release only the process group currently owning one slot CDP port."""
    try:
        raw=subprocess.check_output(["ss","-ltnp",f"sport = :{int(port)}"],text=True,stderr=subprocess.DEVNULL)
        ids={int(x) for x in re.findall(r"pid=(\d+)",raw)}
        if not ids:return
        rows=subprocess.check_output(["ps","-eo","pid=,ppid="],text=True,stderr=subprocess.DEVNULL).splitlines()
        by={}
        for line in rows:
            z=line.split()
            if len(z)>=2:
                try:by[int(z[0])]=int(z[1])
                except Exception:pass
        changed=True
        while changed:
            changed=False
            for pid,pp in by.items():
                if pp in ids and pid not in ids:ids.add(pid);changed=True
        for sig in (15,9):
            for pid in ids:
                try:os.kill(pid,sig)
                except Exception:pass
            time.sleep(.5)
    except Exception:pass

def kill_orphan_electron():
    """Remove orphaned Chromium helper processes left after Electron exits."""
    try:
        raw=subprocess.check_output(["ps","-eo","pid=,ppid=,args="],text=True,stderr=subprocess.DEVNULL)
        for line in raw.splitlines():
            parts=line.strip().split(None,2)
            if len(parts)!=3: continue
            try: pid,ppid=int(parts[0]),int(parts[1])
            except Exception: continue
            cmd=parts[2]
            if "cmcc-jtydn" in cmd and ppid==1 and any(x in cmd for x in ("--type=zygote","--type=renderer","--type=gpu-process","--type=utility")):
                try: os.kill(pid,15)
                except Exception: pass
        time.sleep(.8)
    except Exception: pass

def kill_all_client_processes():
    """Hard-reset the single global Electron/CDP/SDK slot inside this container.

    The vendor's singleton owner can lose --user-data-dir from argv, so a
    profile-scoped walk can miss it and the next launch prints 'Another
    instance...'. The ZTE SDK helper bootCypc can also survive after Electron
    exits; when it remains alive, subsequent launches may run API/MQTT bootstrap
    but never expose a renderer on CDP. This deployment exposes one global
    9222/display/client slot, hence all vendor client/SDK processes are one
    safe, bounded cleanup scope.
    """
    try:
        patterns=(
            "/opt/chuanyun-vdi-client/cmcc-jtydn",
            "/opt/chuanyun-vdi-client/resources/app.asar.unpacked/node_modules/chuanyunAddOn-zte/ccsdk/bin/bootCypc",
            "/opt/chuanyun-vdi-client/resources/app.asar.unpacked/node_modules/chuanyunAddOn/ccsdk",
            "bootCypc",
        )
        for sig in (15,9):
            for pat in patterns:
                subprocess.run(["pkill",f"-{sig}","-f",pat],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            time.sleep(1.0)
        # Confirm no non-zombie vendor process still owns the single slot. This
        # is diagnostic only; zombies under PID 1 cannot be killed but also do
        # not own CDP/sockets.
        try:
            raw=subprocess.check_output(["ps","-eo","pid=,stat=,args="],text=True,stderr=subprocess.DEVNULL)
            alive=[]
            for line in raw.splitlines():
                if ("cmcc-jtydn" in line or "bootCypc" in line) and "<defunct>" not in line:
                    stat=line.strip().split(None,2)[1] if len(line.strip().split(None,2))>=2 else ""
                    if "Z" not in stat: alive.append(line.strip()[:220])
            if alive: print("WARN vendor processes survived cleanup: "+json.dumps(alive,ensure_ascii=False),flush=True)
        except Exception: pass
        time.sleep(.5)
    except Exception: pass

class CDP:
    def __init__(self,ws): self.ws=ws; self.i=0
    def command(self,method,params=None):
        self.i+=1; i=self.i
        self.ws.send(json.dumps({"id":i,"method":method,"params":params or {}}))
        end=time.time()+15
        while time.time()<end:
            m=json.loads(self.ws.recv())
            if m.get("id")==i:
                if "error" in m: raise RuntimeError(str(m["error"]))
                return m.get("result",{})
        raise TimeoutError("CDP command timeout")
    def eval(self,expr,wait=True):
        self.i+=1;i=self.i;self.ws.send(json.dumps({"id":i,"method":"Runtime.evaluate","params":{"expression":expr,"returnByValue":True,"awaitPromise":wait}}))
        end=time.time()+15
        while time.time()<end:
            m=json.loads(self.ws.recv())
            if m.get("id")==i:return m.get("result",{}).get("result",{}).get("value")
        raise TimeoutError("CDP evaluate timeout")
def js_click(text):
    q=json.dumps(text,ensure_ascii=False)
    return f"""(()=>{{
      const want={q}, norm=s=>String(s||'').replace(/\\s+/g,'').trim();
      const visible=e=>{{const r=e.getBoundingClientRect?.(),s=getComputedStyle(e);return r&&r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden'}};
      const all=[...document.querySelectorAll('*')].filter(e=>visible(e)&&norm(e.innerText||e.textContent||e.getAttribute('aria-label'))===norm(want));
      if(!all.length)return {{ok:false,reason:'not_found'}};
      const e=all.sort((a,b)=>a.getBoundingClientRect().width*a.getBoundingClientRect().height-b.getBoundingClientRect().width*b.getBoundingClientRect().height)[0];
      let target=e;
      for(let i=0;i<7&&target;i++,target=target.parentElement){{
        const tag=String(target.tagName||'').toLowerCase();
        if(tag==='button'||tag==='a'||target.getAttribute('role')==='button'||target.onclick)break;
      }}
      if(!target)target=e;
      target.scrollIntoView?.({{block:'center',inline:'center'}});target.focus?.();
      const r=target.getBoundingClientRect(); return {{ok:true,x:r.left+r.width/2,y:r.top+r.height/2,tag:target.tagName,text:norm(target.innerText||target.textContent||'')}};
    }})()"""
def js_click_reference(selector,text_hint=None):
    sel=json.dumps(selector,ensure_ascii=False); hint=json.dumps(text_hint,ensure_ascii=False) if text_hint else "null"
    return f"""(()=>{{try{{const xs=[...document.querySelectorAll({sel})];const e={hint}!==null?xs.find(x=>String(x.innerText||x.textContent||'').includes({hint})):xs[0];if(!e)return null;const r=e.getBoundingClientRect();if(!r.width||!r.height)return null;return {{x:r.left+r.width/2,y:r.top+r.height/2,text:String(e.innerText||e.textContent||'').trim()}}}}catch(e){{return null}}}})()"""
def click_reference_connect(c,index=0):
    """Reference-project style: select the card's .btn-link, then native CDP click."""
    selectors=[f".h-item-wrap:nth-child({int(index)+1}) .btn-link",".h-item-wrap .btn-link"]
    for sel in selectors:
        try:
            p=c.eval(js_click_reference(sel,"连接"))
            if not isinstance(p,dict): continue
            x=float(p["x"]);y=float(p["y"])
            c.command("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y})
            c.command("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1})
            c.command("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1})
            return {"ok":True,"selector":sel,"x":x,"y":y,"text":p.get("text","")[:120]}
        except Exception: pass
    return {"ok":False,"reason":"reference_selector_not_found"}
def click_control(c,text):
    r=c.eval(js_click(text))
    if not isinstance(r,dict) or not r.get("ok"): return {"ok":False,"reason":"not_found"}
    try:
        x=float(r.get("x",0)); y=float(r.get("y",0))
        # Caller records the semantic button selection; this function records
        # the actual low-level dispatch attempt and its result.
        c.command("Input.dispatchMouseEvent",{"type":"mouseMoved","x":x,"y":y})
        c.command("Input.dispatchMouseEvent",{"type":"mousePressed","x":x,"y":y,"button":"left","clickCount":1})
        c.command("Input.dispatchMouseEvent",{"type":"mouseReleased","x":x,"y":y,"button":"left","clickCount":1})
        return {"ok":True,"x":x,"y":y,"tag":r.get("tag"),"text":r.get("text")}
    except Exception as e:
        return {"ok":False,"reason":str(e)[:160],"x":x,"y":y,"tag":r.get("tag"),"text":r.get("text")}
def start_client(profile,port,display=None):
    base="/opt/chuanyun-vdi-client"; addon=base+"/resources/app.asar.unpacked/node_modules"; sdk=addon+"/chuanyunAddOn/ccsdk/uos"
    slot=current_slot.get(); display=display or slot["display"]
    env=os.environ.copy();env.update(DISPLAY=display,CY_BIN_PATH=sdk+"/bin",LD_LIBRARY_PATH="/opt/cmcc-runtime-lib:"+addon+"/netdetectAddOn/ntsdk/lib",GST_PLUGIN_PATH=sdk+"/lib",GST_PLUGIN_PATH_1_0=sdk+"/lib")
    launcher="/opt/cmcc-app/fallback-start-client.sh"
    cmd=[launcher,"--no-sandbox","--disable-gpu",f"--remote-debugging-port={port}",f"--user-data-dir={profile}","--no-first-run"]
    kill_port_owner(port)
    log=open(ROOT/("client-"+Path(profile).name+".log"),"a",buffering=1); return subprocess.Popen(cmd,env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True),log
def stop_client(p,log):
    if p:
        try:
            try: os.killpg(p.pid,15)
            except Exception: p.terminate()
            try: p.wait(8)
            except subprocess.TimeoutExpired: raise
        except Exception:
            try:
                try: os.killpg(p.pid,9)
                except Exception: p.kill()
                p.wait(3)
            except Exception:pass
    if log:
        try:log.close()
        except Exception:pass

def stop_active_client(key):
    item=active_clients.pop(key,None)
    if not item:return
    p,log=item
    stop_client(p,log)
    # Only terminate this account's profile tree; never kill another slot.
    kill_profile_clients(str(PROFILES/key))
    clear_stale_profile_locks(str(PROFILES/key))
def reap_children():
    """Reap exited direct children to prevent Electron zombie accumulation."""
    while True:
        try:
            pid,_=os.waitpid(-1,os.WNOHANG)
            if pid<=0:return
        except (ChildProcessError,OSError):return
        except Exception:return

def client_log_runtime_state(profile,offset=0):
    """Read the newest current-launch cloud state without retaining secrets."""
    try:
        path=ROOT/("client-"+Path(profile).name+".log")
        with path.open("r",encoding="utf-8",errors="replace") as f:
            f.seek(max(0,int(offset))); text=f.read()[-160000:]
        hits=[]
        # Covers both cloudPc/list responses and MQTT busStatusChange events.
        for m in re.finditer(r'"vmStatus"\s*:\s*(\d+).*?"vmStatusShow"\s*:\s*"([^"]*)"',text):
            hits.append((m.start(),int(m.group(1)),m.group(2)))
        for m in re.finditer(r'"actionType"\s*:\s*"busStatusChange".*?"vmStatus"\s*:\s*(\d+).*?"vmStatusShow"\s*:\s*"([^"]*)"',text):
            hits.append((m.start(),int(m.group(1)),m.group(2)))
        if not hits:return None
        _,status,show=max(hits,key=lambda x:x[0]); return status,show
    except Exception:return None

async def wait_page(timeout=60,key=None,port=None):
    """Compatibility wrapper for renderer replacement/login polling paths."""
    end=time.time()+timeout
    while time.time()<end:
        t=page_target(port)
        if t:return t
        await asyncio.sleep(1)
    if key:
        try:
            targets=page_targets(port)
            event(key,"cdp_timeout",stage="CDP页面目标超时",reason="未发现可附着renderer；noVNC画面不等于CDP目标",targets=[{"type":x.get("type"),"url":str(x.get("url",''))[:180],"socket":bool(x.get("webSocketDebuggerUrl"))} for x in targets[:8]])
        except Exception:pass
    raise TimeoutError("CDP page target unavailable")

async def wait_page_or_api(timeout=60,key=None,profile=None,log=None,port=None):
    """Race CDP readiness against a timestamp-bounded cloud-list status."""
    try: offset=log.tell() if log else 0
    except Exception: pass
    end=time.time()+timeout
    last_state=None
    while time.time()<end:
        t=page_target(port)
        if t:return ("page",t)
        state=client_log_runtime_state(profile,offset) if profile else None
        if state and state != last_state:
            last_state=state
            if key:event(key,"api_observed",stage="观察到客户端云端状态",vmStatus=state[0],vmStatusShow=state[1][:80],source="client_log_current_offset")
        if state and state[0]==1:
            if key:event(key,"api_state",stage="客户端云端列表/MQTT已确认运行中，跳过CDP等待",vmStatus=1,vmStatusShow=state[1][:80],source="current_client_launch")
            return ("running",state)
        # Non-running API evidence is useful context, but it cannot replace a
        # renderer because the actual Connect control is renderer-owned. Keep
        # waiting for the bounded CDP recovery path instead of relaunching
        # immediately on every vmStatus=23 response.
        if key and int(time.time())%5==0:
            try:
                targets=page_targets(port); detail="当前无可附着renderer"
                event(key,"cdp_probe",stage="等待云电脑页面目标",detail=detail,targets=[{"type":x.get("type"),"url":str(x.get("url",''))[:180],"socket":bool(x.get("webSocketDebuggerUrl"))} for x in targets[:8]],api_state=state[1] if state else None)
            except Exception:pass
        await asyncio.sleep(1)
    raise TimeoutError("CDP page target unavailable")

def cdp_target_error(exc):
    s=str(exc)
    return any(x in s for x in ("No such target id","WebSocketBadStatusException","socket is already closed","Connection to remote host was lost"))

async def reconnect_current_page(ws=None,c=None,port=None):
    """Close a stale renderer socket and attach once to the current target."""
    try:
        if ws: ws.close()
    except Exception: pass
    for _ in range(3):
        t=page_target(port)
        if t and t.get("webSocketDebuggerUrl"):
            try:
                nws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15)
                return nws,CDP(nws),t
            except Exception:
                await asyncio.sleep(.25)
    raise TimeoutError("CDP current renderer unavailable")

async def attach(t,ws):
    if ws:
        try: ws.close()
        except Exception:pass
    sock=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15)
    return sock,CDP(sock)
def js_submit_login():
    return """(()=>{
      const norm=s=>String(s||'').replace(/\\s+/g,'').trim();
      const b=[...document.querySelectorAll('button,[role=\"button\"],a')].find(x=>{const r=x.getBoundingClientRect?.();return r&&r.width>0&&r.height>0&&norm(x.innerText||x.textContent||x.getAttribute('aria-label')).includes('登录')});
      if(!b)return {ok:false,reason:'button_missing'};
      if(b.disabled||b.getAttribute('aria-disabled')==='true')return {ok:false,reason:'button_disabled'};
      const f=b.closest('form'); b.scrollIntoView?.({block:'center'}); b.click();
      if(f&&f.requestSubmit)try{f.requestSubmit(b)}catch(e){}
      return {ok:true,disabled:!!b.disabled};
    })()"""
def js_login_tab(text):
    q=json.dumps(text,ensure_ascii=False)
    return f"""(()=>{{
      const want={q}, norm=s=>String(s||'').replace(/\\s+/g,'').trim();
      const nodes=[...document.querySelectorAll('*')].filter(x=>{{
        const r=x.getBoundingClientRect?.(),s=norm(x.innerText||x.textContent||x.getAttribute('aria-label'));
        return s===norm(want)&&r&&r.width>0&&r.height>0;
      }});
      if(!nodes.length)return {{ok:false,reason:'tab_not_found'}};
      const e=nodes.sort((a,b)=>a.getBoundingClientRect().width*a.getBoundingClientRect().height-b.getBoundingClientRect().width*b.getBoundingClientRect().height)[0];
      let target=e;
      for(let i=0;i<5&&target;i++,target=target.parentElement){{try{{target.scrollIntoView?.({{block:'center'}});target.focus?.();target.click?.();for(const type of ['mousedown','mouseup','click'])target.dispatchEvent(new MouseEvent(type,{{bubbles:true,cancelable:true,view:window}}));}}catch(err){{}}}}
      const r=e.getBoundingClientRect(); return {{ok:true,x:r.left+r.width/2,y:r.top+r.height/2}};
    }})()"""
def capture_snapshot(c):
    return c.eval("({url:location.href,title:document.title,body:(document.body&&document.body.innerText||'').slice(0,1800),inputs:[...document.querySelectorAll('input')].map(x=>({type:x.type,placeholder:x.placeholder})),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,60),target_count:document.querySelectorAll('*').length})") or {}


def cloud_state(c):
    return c.eval("""(()=>{
      const norm=s=>String(s||'').replace(/\\s+/g,' ').trim();
      const terms=['运行中','已连接','正常','连接中','启动中','开机中','正在连接','正在启动','已关机'];
      const els=[...document.querySelectorAll('*')].filter(e=>{const r=e.getBoundingClientRect?.();return r&&r.width>0&&r.height>0&&terms.some(t=>norm(e.innerText||e.textContent).includes(t))});
      const pick=els.sort((a,b)=>a.getBoundingClientRect().width*a.getBoundingClientRect().height-b.getBoundingClientRect().width*b.getBoundingClientRect().height)[0];
      let e=pick; let text=pick?norm(pick.innerText||pick.textContent):'';
      for(let i=0;i<6&&e;i++,e=e.parentElement){const s=norm(e.innerText||e.textContent);if(s.length>20&&s.length<1200&&terms.some(t=>s.includes(t))){text=s;break}}
      let vmStatus=null;
      const scan=v=>{if(vmStatus!==null||!v||typeof v!=='object')return;try{if(Number(v.vmStatus)===1||Number(v.vm_status)===1)vmStatus=1;for(const k of Object.keys(v).slice(0,80)){const x=v[k];if(x&&typeof x==='object')scan(x)}}catch(err){}};
      for(const k of Object.keys(window).filter(k=>/cloud|pc|service|connect|firm|user/i.test(k)).slice(0,120)){try{scan(window[k])}catch(err){}}
      return {text:text.slice(0,900),url:location.href,vmStatus};
    })()""") or {};

def cloud_runtime_status(c):
    """Read a bounded vmStatus hint from renderer state; never log credentials."""
    try:
        r=c.eval("""(()=>{
          const seen=new Set(), hits=[];
          const walk=(v,d)=>{if(d>3||v==null||hits.length>8||typeof v!=='object'||seen.has(v))return;seen.add(v);
            for(const k of Object.keys(v).slice(0,80)){let x;try{x=v[k]}catch(e){continue}
              if((k==='vmStatus'||k==='vm_status'||k==='status')&&(typeof x==='number'||typeof x==='string'))hits.push({key:k,value:String(x).slice(0,40)});
              if(x&&typeof x==='object')walk(x,d+1);
            }};
          for(const k of ['clouds','mainApi','store','state','userInfo','cloudList']){try{walk(window[k],0)}catch(e){}}
          const s=String(document.body?.innerText||'');return {vm1:hits.some(x=>x.value==='1'),hits:hits.slice(0,8),running:/运行中|已连接|正常/.test(s),url:location.href};
        })()""") or {}
        return r if isinstance(r,dict) else {}
    except Exception:return {}

def already_running(c):
    st=cloud_state(c);rt=cloud_runtime_status(c);text=str(st.get("text",''))
    normal=any(x in text for x in ("运行中","正常","已连接")) and not any(x in text for x in ("连接中","启动中","开机中","正在连接","正在启动"))
    return bool(normal or rt.get("vm1")),st,rt


async def switch_login_mode(c,mode):
    label="子账号登录" if mode=="sub" else "账密登录"
    for _ in range(40):
        try:
            clicked=c.eval(js_login_tab(label))
            state=c.eval("({password:!!document.querySelector('input[type=password]'),account:!![...document.querySelectorAll('input')].find(x=>x.type==='text'||x.type==='tel'),body:(document.body&&document.body.innerText||'').slice(0,1200)})") or {}
            body=str(state.get("body",''))
            # The password tab does not always expose type=password immediately;
            # the visible labels are the stronger signal.
            if (state.get("password") and state.get("account")) or ("账号名" in body and "密码" in body):
                return True
            if clicked: await asyncio.sleep(.25)
        except Exception: pass
        await asyncio.sleep(.25)
    return False
async def screenshot_page(key,c,stage="页面截图"):
    # noVNC already provides the live visual diagnostic. CDP screenshots add
    # latency and can block the same renderer path used for state checks.
    return None
async def screenshot_quiet(key,c):
    return None

async def login_and_find(c,ws,key,cfg):
    event(key,"privacy",stage="客户端已启动，等待隐私确认")
    privacy_seen=False
    # Privacy is persisted per Electron profile. On later launches the client can
    # open directly on #/login without showing the agreement; that is a valid
    # fast path, not a timeout.
    for n in range(80):
        try:
            if click_control(c,"已满14周岁并同意"):
                privacy_seen=True; event(key,"privacy_accepted",stage="已接受隐私协议",elapsed=n*0.25); break
            state=c.eval("({url:location.href,title:document.title,body:(document.body&&document.body.innerText||'').slice(0,1600),inputs:document.querySelectorAll('input').length,buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,40),target_count:document.querySelectorAll('*').length})") or {}
            body=str(state.get("body",'')) if isinstance(state,dict) else ''
            url=str(state.get("url",'')) if isinstance(state,dict) else ''
            event(key,"privacy_probe",stage="等待隐私/登录页",url=url,detail=body[:260],page_snapshot=state)
            # A persisted session may open directly on the cloud list. Do not
            # spend the privacy timeout probing a page that is already usable.
            home=("#/home" in url and any(x in body for x in ("我的云电脑","已分配云电脑","云电脑")))
            if home:
                privacy_seen=True
                event(key,"cloud_home_shell",stage="已进入云电脑列表，等待云电脑卡片加载",url=url,cloud_list_loaded=False,page_snapshot=state)
                break
            if "#/login" in url or state.get("inputs",0)>1 or "账密登录" in body or "子账号登录" in body:
                privacy_seen=True; event(key,"privacy_skipped",stage="已记录隐私协议，客户端直接进入登录页",url=url); break
        except Exception:
            try:
                nt=page_target()
                if nt and nt.get("webSocketDebuggerUrl"):
                    nws=websocket.create_connection(nt["webSocketDebuggerUrl"],timeout=10)
                    try: ws.close()
                    except Exception: pass
                    ws=nws; c=CDP(ws)
            except Exception: pass
        await asyncio.sleep(.25)
    if not privacy_seen:
        # Do not fail here on a fresh start.html.  The first CDP target can be
        # slow and the welcome page may expose the agreement control only after
        # the initial poll. Let the renderer-refresh gate below handle it.
        state={}
        try:
            state=c.eval("({url:location.href,title:document.title,body:(document.body&&document.body.innerText||'').slice(0,1800),inputs:[...document.querySelectorAll('input')].map(x=>({type:x.type,placeholder:x.placeholder})),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,60),target_count:document.querySelectorAll('*').length})") or {}
        except Exception: pass
        body=str(state.get("body",'')); url=str(state.get("url",''))
        # Established profiles have already passed consent. Only run the
        # welcome-page acceptance loop when the current renderer actually
        # exposes the welcome document; otherwise proceed to login/home.
        if not ("start.html" in url or "已满14周岁并同意" in body or "隐私政策" in body):
            if "#/login" in url or "#/home" in url or state.get("inputs"):
                privacy_seen=True
            else:
                event(key,"privacy_timeout",stage="隐私协议等待超时",detail=str(state)[:500],page_snapshot=state)
                try:
                    nt=page_target()
                    if nt and nt.get("webSocketDebuggerUrl"):
                        nws=websocket.create_connection(nt["webSocketDebuggerUrl"],timeout=10); nc=CDP(nws)
                        await screenshot_page(key,nc,"隐私协议等待超时"); nws.close()
                except Exception: pass
                raise TimeoutError("privacy agreement unavailable")
    await asyncio.sleep(.25)
    # A fresh profile can remain on the Electron welcome/privacy document even
    # after the first short privacy poll.  Never send login-tab clicks to that
    # document: explicitly accept privacy, rediscover the renderer, and wait for
    # the real login route first.
    t=await wait_page(20); ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15); c=CDP(ws)
    # Consent can replace the Electron renderer. Re-discover the page target on
    # every iteration and retry the real input dispatch until the login route is
    # actually visible; a dispatched click alone is not treated as completion.
    state={}
    for attempt in range(80):
        try:
            nt=page_target()
            if nt and nt.get("webSocketDebuggerUrl") and nt.get("webSocketDebuggerUrl") != t.get("webSocketDebuggerUrl"):
                try: ws.close()
                except Exception: pass
                t=nt; ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15); c=CDP(ws)
            state=c.eval("({url:location.href,body:(document.body&&document.body.innerText||'').slice(0,1800)})") or {}
            url=str(state.get("url",'')); body=str(state.get("body",''))
            if "#/login" in url or "账密登录" in body or "子账号登录" in body:
                event(key,"privacy_navigation",stage="隐私确认后已进入登录页",url=url); break
            if "#/home" in url and any(x in body for x in ("我的云电脑","已分配云电脑","云电脑","家庭云电脑","个人云电脑","运行中","已关机","连接")):
                event(key,"privacy_navigation",stage="隐私确认后已直接进入云电脑首页",url=url); break
            cr=click_control(c,"已满14周岁并同意")
            if cr and cr.get("ok"):
                event(key,"privacy_retry_click",stage="重试确认隐私协议",detail=str(cr)[:180])
            await asyncio.sleep(.25)
        except Exception as e:
            event(key,"privacy_probe_error",stage="隐私确认后等待登录页",reason=str(e)[:160])
            await asyncio.sleep(.25)
    # If the first gate already saw #/home, keep the same renderer and proceed
    # directly to the business-page gate instead of polling for a login route.
    def is_business_home(s):
        u=str(s.get("url",'')); b=str(s.get("body",''))
        return "#/home" in u and any(x in b for x in ("我的云电脑","已分配云电脑","云电脑","个人中心"))
    if is_business_home(state):
        body_now=str(state.get("body",''))
        card_ready=("暂无任何匹配结果" not in body_now and any(x in body_now for x in ("家庭云电脑","个人云电脑","已关机","连接","运行中","正常","已连接")))
        if card_ready:
            return c,ws,state
        # The authenticated #/home shell is already the business page. A
        # delayed/empty cloud-card response must not be misreported as a
        # privacy/login-route failure; downstream SDK/card polling handles it.
        card_end=time.time()+45
        while time.time()<card_end:
            try:
                state=capture_snapshot(c); body_now=str(state.get("body",''))
                card_ready=("暂无任何匹配结果" not in body_now and any(x in body_now for x in ("家庭云电脑","个人云电脑","已关机","连接","运行中","正常","已连接")))
                event(key,"cloud_list_probe",stage="等待云电脑卡片加载",ready=card_ready,detail=body_now[:320],page_snapshot=state)
                if card_ready: break
            except Exception as e:
                # Renderer replacement is expected during Electron navigation.
                try:
                    nt=page_target();
                    if nt and nt.get("webSocketDebuggerUrl"):
                        try: ws.close()
                        except Exception: pass
                        ws=websocket.create_connection(nt["webSocketDebuggerUrl"],timeout=15); c=CDP(ws)
                except Exception: pass
            await asyncio.sleep(1)
        state_url=str(state.get("url",'')); state_body=str(state.get("body",''))
        if is_business_home(state):
            event(key,"login_success",stage="已进入云电脑业务页，跳过登录表单",url=state_url,cloud_list_loaded=("暂无任何匹配结果" not in state_body),main_api=bool(c.eval("!!(window.mainApi&&window.mainApi.connectWorker)")))
            return c,ws,state
    state_url=str(state.get("url",'')); state_body=str(state.get("body",''))
    if "#/login" not in state_url and "账密登录" not in state_body and "子账号登录" not in state_body:
        event(key,"privacy_navigation_timeout",stage="隐私确认后未进入登录页",detail=str(state)[:500],page_snapshot=state)
        try:
            nt=page_target()
            if nt and nt.get("webSocketDebuggerUrl"):
                nws=websocket.create_connection(nt["webSocketDebuggerUrl"],timeout=10); nc=CDP(nws)
                await screenshot_page(key,nc,"隐私确认后未进入登录页"); nws.close()
        except Exception: pass
        raise TimeoutError("privacy accepted but login route unavailable")
    route="子账号登录" if cfg.get("login_mode")=="sub" else "账密登录"; event(key,"login_route",stage=route)
    # The initial screen is SMS login. Switch to the password tab before
    # searching for a password input; both tabs remain in the DOM.
    if not await switch_login_mode(c,cfg.get("login_mode","main")):
        snap=c.eval("({url:location.href,title:document.title,body:(document.body&&document.body.innerText||'').slice(0,1800),inputs:[...document.querySelectorAll('input')].map(x=>({type:x.type,placeholder:x.placeholder})),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,60),target_count:document.querySelectorAll('*').length})")
        event(key,"login_mode_error",stage="账密登录页未切换",detail=str(snap)[:500],page_snapshot=snap)
        await screenshot_page(key,c,"账密登录页未切换")
        raise RuntimeError("账密登录页未切换: "+str(snap)[:700])
    await asyncio.sleep(.3)
    user=json.dumps(cfg["username"]);pwd=json.dumps(decrypt(cfg["password"]))
    fill=f"""(()=>{{
      const a=[...document.querySelectorAll('input')];
      const p=a.find(x=>x.type==='password');
      const u=a.find(x=>x.type==='text'||x.type==='tel'||x.autocomplete==='username') || a.find(x=>x!==p && x.type!=='checkbox');
      const q=a.find(x=>x.type==='checkbox');
      const set=(x,v)=>{{if(!x)return;const proto=Object.getPrototypeOf(x);const d=Object.getOwnPropertyDescriptor(proto,'value');if(d&&d.set)d.set.call(x,v);else x.value=v;x.dispatchEvent(new Event('input',{{bubbles:true}}));x.dispatchEvent(new Event('change',{{bubbles:true}}))}};
      set(u,{user}); set(p,{pwd}); if(q&&!q.checked)q.click(); return !!(u&&p)
    }})()"""
    filled=False
    for _ in range(40):
        try:
            if c.eval(fill): filled=True; break
        except Exception: pass
        await asyncio.sleep(.5)
    if not filled:
        snapshot=c.eval("({url:location.href,body:(document.body&&document.body.innerText||'').slice(0,1200),inputs:[...document.querySelectorAll('input')].map(x=>({type:x.type,placeholder:x.placeholder}))})")
        raise RuntimeError("login form unavailable: "+str(snapshot)[:500])
    submit_result=None
    for _ in range(20):
        try:
            submit_result=c.eval(js_submit_login())
            if isinstance(submit_result,dict) and submit_result.get("ok"):
                break
        except Exception: pass
        await asyncio.sleep(.5)
    if not isinstance(submit_result,dict) or not submit_result.get("ok"):
        snap=c.eval("({url:location.href,body:(document.body&&document.body.innerText||'').slice(0,1400),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>({text:(x.innerText||x.textContent||'').trim(),disabled:!!x.disabled})).filter(x=>x.text)})")
        raise RuntimeError("login submit unavailable: "+str(snap)[:700])
    event(key,"logging_in",stage="已提交登录，等待业务页",submit=submit_result)
    end=time.time()+60;last=""
    while time.time()<end:
        await asyncio.sleep(1)
        try:
            t=await wait_page(3)
            try: ws.close()
            except Exception: pass
            ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15); c=CDP(ws)
            info=c.eval("({url:location.href,title:document.title,body:(document.body&&document.body.innerText||'').slice(0,2200),inputs:[...document.querySelectorAll('input')].map(x=>({type:x.type,placeholder:x.placeholder})),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,80),target_count:document.querySelectorAll('*').length,api:!!(window.mainApi&&window.mainApi.connectWorker)})") or {}
            last=str(info)
            body=info.get("body","") if isinstance(info,dict) else ""
            url=str(info.get("url","")) if isinstance(info,dict) else ""
            # Do not treat the login page as a successful business page merely
            # because its footer contains “云电脑” and mainApi is already exposed.
            on_login="#/login" in url or "账号名密码登录" in body or ("手机号" in body and "验证码" in body)
            has_cloud_card=("暂无任何匹配结果" not in body and any(x in body for x in ("家庭云电脑","个人云电脑","已关机","连接","运行中","正常","已连接")))
            if not on_login and "#/home" in url and has_cloud_card:
                event(key,"login_success",stage="登录成功，云电脑业务页已确认",main_api=bool(info.get("api")));return c,ws,info
            if "#/login" in url and ("账号名密码登录" in body or "手机号" in body):
                # Login page remained after submit or the renderer is still on
                # the login route. Always expose the newest snapshot to WebUI.
                last="登录页仍未离开，可能登录提交未生效或业务页尚未加载"
                if "验证码" in body or "错误" in body or "失败" in body:
                    last="登录页出现验证码/错误提示，需要人工确认"
        except Exception as e:
            if cdp_target_error(e):
                try:
                    ws,c,_=await reconnect_current_page(ws,c)
                except Exception: pass
        event(key,"waiting_login",stage="等待业务页",detail=last[:240],page_snapshot=info if isinstance(info,dict) else {"body":last})
        await screenshot_page(key,c,"等待业务页")
    raise RuntimeError("login/cloud list not confirmed; 未确认业务页")
async def sdk_keepalive(key,c,ws,cfg):
    ok,st,rt=already_running(c)
    if ok:
        event(key,"success",stage="进入时已确认云电脑运行中",reason="initial_running_vmStatus_1" if rt.get("vm1") else "initial_cloud_state_normal",keepalive_confirmed=True,vmStatus=1 if rt.get("vm1") else None)
        return True
    event(key,"sdk_start",stage="保活流程：云电脑未处于运行中，开始SDK连接尝试",method="sdk_connectWorker",next_on_failure="client_click_fallback")
    check=c.eval("({api:!!window.mainApi,worker:!!(window.mainApi&&window.mainApi.connectWorker),url:location.href,keys:Object.keys(window).filter(k=>/cloud|pc|connect|firm/i.test(k)).slice(0,80),body:(document.body&&document.body.innerText||'').slice(0,1600)})")
    if not isinstance(check,dict) or not check.get("worker"): raise RuntimeError("mainApi.connectWorker unavailable")
    event(key,"sdk_page_confirmed",stage="SDK业务页确认",detail="url="+str(check.get("url","")))
    # Official renderer flow for normal cloud PCs:
    #   GET_FIRM_AUTH({userServiceId}) -> mainApi.connectWorker({userServiceId, traceId, ...data})
    # The previous implementation grabbed the whole Vue store (keys like clouds,
    # banners, skinMode), so options.spuCode was undefined and Linux main process
    # crashed at options.spuCode.includes(...).
    expr="""(()=>{
      const safeKeys=o=>o&&typeof o==='object'?Object.keys(o).slice(0,80):[];
      const unbox=v=>{try{const o=JSON.parse(v);return o&&Object.prototype.hasOwnProperty.call(o,'key')?o.key:o}catch(e){return v}};
      const readStore=k=>{try{return unbox(localStorage.getItem(k))}catch(e){return null}};
      let clouds=readStore('clouds');
      if(typeof clouds==='string'){try{clouds=JSON.parse(clouds)}catch(e){}}
      if(!Array.isArray(clouds)) clouds=[];
      const cloud=clouds.find(x=>x&&x.userServiceId) || null;
      if(!cloud)return {done:true,error:'cloud card unavailable in localStorage.clouds',cloudsType:typeof clouds,cloudsLen:Array.isArray(clouds)?clouds.length:null};
      const userServiceId=cloud.userServiceId;
      const traceId='hermes_'+Date.now().toString(36)+'_'+Math.random().toString(36).slice(2,8);
      window.__cmcc_sdk={done:false,error:null,result:null,started:Date.now(),phase:'getFirmAuth',userServiceId};
      Promise.resolve(window.mainApi.request({url:'/cc/getFirmAuth/v1',data:{userServiceId}})).then(d=>{
        const code=d&&d.code;
        if(code!==2000){window.__cmcc_sdk={done:true,error:'getFirmAuth failed code='+code+' msg='+String(d&&d.msg||'').slice(0,120),result:null};return;}
        const data=(d&&d.data)||{};
        const opts={...data,userServiceId,traceId};
        for(const k of ['spuCode','spuName','skuName','skuSpec','cloudPcType','series','vmId','vmID','userServiceId']) if((opts[k]===undefined||opts[k]===null||opts[k]==='')&&cloud[k]!==undefined) opts[k]=cloud[k];
        opts.spuCode=String(opts.spuCode||''); opts.spuName=String(opts.spuName||''); opts.skuName=String(opts.skuName||'');
        window.__cmcc_connect_options=opts;
        window.__cmcc_sdk={done:false,error:null,result:null,started:Date.now(),phase:'connectWorker',userServiceId,optionKeys:safeKeys(opts),spuCode:opts.spuCode||null,vmId:opts.vmId||null};
        return Promise.resolve(window.mainApi.connectWorker(opts)).then(r=>{
          window.__cmcc_sdk={done:true,error:null,result:r,phase:'connectWorkerDone',userServiceId,optionKeys:safeKeys(opts),spuCode:opts.spuCode||null,vmId:opts.vmId||null};
        }).catch(e=>{
          window.__cmcc_sdk={done:true,error:String(e),result:null,phase:'connectWorkerError',userServiceId,optionKeys:safeKeys(opts),spuCode:opts.spuCode||null,vmId:opts.vmId||null};
        });
      }).catch(e=>{window.__cmcc_sdk={done:true,error:'getFirmAuth exception: '+String(e),result:null,phase:'getFirmAuthError',userServiceId};});
      return {done:false,started:true,userServiceId,cloudKeys:safeKeys(cloud),cloudSpuCode:cloud.spuCode||null};
    })()"""
    initial=c.eval(expr);event(key,"sdk_invoked",stage="已按官方流程获取firmAuth并调用 mainApi.connectWorker",detail=str(initial)[:500])
    if isinstance(initial,dict) and initial.get("done"):
        raise RuntimeError(str(initial.get("error"))[:220])
    for n in range(1,31):
        await asyncio.sleep(1);r=c.eval("window.__cmcc_sdk||null");event(key,"sdk_poll",stage=f"SDK执行中 {n}s",detail=str({k:r.get(k) for k in ('done','error','phase','userServiceId','optionKeys','spuCode','vmId') if isinstance(r,dict) and k in r})[:500] if isinstance(r,dict) else str(r)[:300])
        if isinstance(r,dict) and r.get("done"):
            if r.get("error"):raise RuntimeError("SDK失败: "+str(r["error"])[:220])
            event(key,"success",stage="保活成功：SDK connectWorker 已完成",method="sdk_connectWorker",evidence="SDK完成后返回并记录done=true",reason="sdk_connectWorker",keepalive_confirmed=True,spuCode=r.get('spuCode'),vmId=r.get('vmId'));return True
    raise TimeoutError("SDK connectWorker 超时")
async def click_fallback(key,c,ws,cfg):
    event(key,"click_fallback",stage="SDK失败，进入普通点击保活兜底",method="client_click_fallback",trigger="sdk_failed",next="connect_button_then_state_confirmation")
    # The page may have replaced its renderer after SDK failure. Rediscover and
    # reattach before searching for the button.
    t=page_target()
    if t and t.get("webSocketDebuggerUrl"):
        try: ws.close()
        except Exception: pass
        ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15); c=CDP(ws)
    before_body=str(c.eval("document.body.innerText") or "")
    before_state=cloud_state(c)
    before_card=str(before_state.get("text",''))
    initial_normal=any(x in before_card for x in ("运行中","正常","已连接")) and not any(x in before_card for x in ("连接中","启动中","开机中","正在连接","正在启动"))
    if initial_normal:
        event(key,"success",stage="点击前已确认云电脑运行中",reason="cloud_state_already_normal",keepalive_confirmed=True); return True
    clicked=False
    for label in ("连接","启动","开机"):
        for _ in range(20):
            try:
                cr=click_control(c,label)
                event(key,"click_attempt",stage=f"尝试点击连接控件：{label}",label=label,result={k:cr.get(k) for k in ("ok","reason","x","y","tag","text") if k in cr})
                if isinstance(cr,dict) and cr.get("ok"):
                    event(key,"click_dispatched",stage="已向连接控件发送原生鼠标事件",detail=str(cr)[:220]); clicked=True; break
            except Exception: pass
            await asyncio.sleep(.5)
        if clicked: break
    if not clicked:
        try:
            cr=click_reference_connect(c,0)
            event(key,"reference_click_attempt",stage="尝试参考项目.btn-link点击",result={k:cr.get(k) for k in ("ok","reason","selector","x","y","text") if k in cr})
            if cr.get("ok"):
                event(key,"click_dispatched_reference",stage="按参考项目卡片.btn-link策略发送原生点击",detail=str(cr)[:240]); clicked=True
        except Exception: pass
    if not clicked:
        snapshot=capture_snapshot(c)
        raise RuntimeError("connect button unavailable: "+str(snapshot)[:900])
    event(key,"connect_invoked",stage="已点击连接，等待状态变化")
    await screenshot_quiet(key,c)
    before_body=str(c.eval("document.body.innerText") or "")
    before_state=cloud_state(c); before_card=str(before_state.get("text",''))
    changed=False; transition=False; transition_seen=False; last=before_body; consecutive_normal=0
    end=time.time()+min(int(cfg.get("connect_timeout",120)),24)
    while time.time()<end:
        await asyncio.sleep(.7)
        try:
            # Fast path: keep using the current CDP session. Reattaching through
            # /json/list every poll can add seconds even though the card state is
            # already updated.
            snap=c.eval("({url:location.href,body:(document.body&&document.body.innerText||'').slice(0,1400),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,50)})") or {}
            last=str(snap.get("body",'')) if isinstance(snap,dict) else str(snap)
            after_state=cloud_state(c); after_card=str(after_state.get("text",''))
        except Exception:
            try:
                t=await wait_page(1)
                try:
                    if ws: ws.close()
                except Exception: pass
                ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=8); c=CDP(ws)
                snap=c.eval("({url:location.href,body:(document.body&&document.body.innerText||'').slice(0,1400),buttons:[...document.querySelectorAll('button,[role=button],a')].map(x=>(x.innerText||x.textContent||'').trim()).filter(Boolean).slice(0,50)})") or {}
                last=str(snap.get("body",'')) if isinstance(snap,dict) else str(snap)
                after_state=cloud_state(c); after_card=str(after_state.get("text",''))
            except Exception as e:
                event(key,"poll_error",stage="点击后读取状态失败",reason=str(e)[:160]); continue
        normal=any(x in after_card for x in ("运行中","正常","已连接")) and not any(x in after_card for x in ("连接中","启动中","开机中","正在连接","正在启动"))
        consecutive_normal = consecutive_normal + 1 if normal else 0
        card_changed=after_card != before_card and bool(after_card)
        transition=transition or any(x in after_card for x in ("连接中","启动中","开机中","正在连接","正在启动"))
        state_after_click=normal and card_changed
        transition_seen=transition_seen or transition or state_after_click
        changed=changed or card_changed or transition
        event(key,"poll",stage="点击保活等待状态恢复",changed=changed,transition=transition,transition_seen=transition_seen,normal=normal,normal_count=consecutive_normal,card=after_card[:320],body=last[:320])
        # Strong proof if we saw a transition. If the cloud page has already
        # jumped to final normal state, two consecutive normal reads are enough;
        # otherwise users wait even though the cloud is already running.
        if transition_seen and normal:
            event(key,"success",stage="保活成功：点击连接后观察到云电脑恢复运行",method="client_click_fallback",evidence="点击后云卡状态已恢复为运行中并通过轮询确认",reason="state_changed_and_recovered",keepalive_confirmed=True);return True
        if consecutive_normal >= 2:
            event(key,"success",stage="保活成功：点击后连续确认云电脑处于运行中",method="client_click_fallback",evidence="连续两次读取到稳定正常状态",reason="confirmed_normal_twice",keepalive_confirmed=True);return True
    try: await screenshot_quiet(key,c)
    except Exception: pass
    raise RuntimeError("点击后状态未恢复：未观察到稳定运行中状态；最后卡片状态="+before_card[:220]+"；最后页面="+last[:320])
async def run_once(key,cfg):
    # Single-flight across probe, realtime and manual entrypoints. A duplicate
    # trigger must not start another Electron/profile/CDP recovery in parallel.
    if key in active_run_keys:
        event(key,"run_coalesced",stage="账号已有保活任务运行，合并重复触发",reason="single_flight")
        return False
    now=time.monotonic(); until=recovery_cooldown_until.get(key,0.0)
    if until>now:
        remaining=max(1,int(until-now))
        event(key,"recovery_cooldown",stage=f"该账号上次恢复失败，冷却{remaining}秒后再试",reason="failed_recovery_cooldown",cooldown_remaining=remaining)
        return False
    active_run_keys.add(key)
    try:
        result=await _run_once_slot(key,cfg)
        # The API-state shortcut returns None after it has emitted a verified
        # success event; only an explicit False is an unconfirmed recovery.
        if result is False:
            raise RuntimeError("SDK/客户端恢复未确认成功")
        recovery_failure_streak.pop(key,None); recovery_cooldown_until.pop(key,None)
        return True
    except asyncio.CancelledError:
        raise
    except Exception as e:
        streak=recovery_failure_streak.get(key,0)+1; recovery_failure_streak[key]=streak
        cooldown=min(300.0,RECOVERY_FAILURE_COOLDOWN*(2**min(streak-1,2)))
        recovery_cooldown_until[key]=time.monotonic()+cooldown
        event(key,"recovery_failed",stage=f"恢复未成功，{int(cooldown)}秒后允许下一次重试",reason=f"{type(e).__name__}: {str(e)[:220]}",failure_streak=streak,cooldown_seconds=int(cooldown))
        return False
    finally:
        active_run_keys.discard(key)

async def _run_once_slot(key,cfg):
    # Every heavy run owns one isolated display/CDP slot for its whole lifetime.
    async with fallback_slots:
        slot=await slot_pool.get(); token=current_slot.set(slot)
        try:
            create_slot_runtime(slot)
        except Exception:
            slot_pool.put_nowait(slot); current_slot.reset(token)
            raise
        fallback_active.add(key); account_slots[key]=slot; event(key,"fallback_slot_acquired",stage=f"已取得兜底保活槽位 {slot['name']}（并发上限{fallback_slots.limit}）",active=list(fallback_active),limit=fallback_slots.limit,display=slot["display"],cdp_port=slot["port"],vnc_token=slot["name"])
        try:
            # A recovery request may wait behind another heavy account. Recheck
            # the cloud state after slot acquisition so a recovered account does
            # not launch Electron unnecessarily.
            try:
                preflight=await asyncio.to_thread(probe_account,key,cfg)
                if preflight.get("class")=="maybe_skip":
                    event(key,"recovery_preflight_normal",stage="取得槽位后复核已恢复，跳过客户端启动",method="api_probe",evidence="槽位等待期间云电脑已恢复",vmStatus=preflight.get("vmStatus"),detail=preflight.get("reason",""),keepalive_confirmed=True)
                    return True
            except Exception as pe:
                event(key,"recovery_preflight_error",stage="取得槽位后复核失败，继续SDK/客户端恢复",reason=f"{type(pe).__name__}: {str(pe)[:180]}")
            return await _run_once_heavy(key,cfg,slot)
        finally:
            stop_active_client(key)
            reap_children()
            fallback_active.discard(key); account_slots.pop(key,None); event(key,"fallback_slot_released",stage=f"已释放兜底保活槽位 {slot['name']}",active=list(fallback_active),limit=fallback_slots.limit,display=slot["display"],cdp_port=slot["port"],vnc_token=slot["name"])
            for _ in range(10):
                try:
                    if not page_targets(slot["port"]): break
                except Exception: break
                await asyncio.sleep(.3)
            reap_children()
            destroy_slot_runtime(slot); slot_pool.put_nowait(slot); current_slot.reset(token)

async def _run_once_heavy(key,cfg,slot):
    profile=PROFILES/key;profile.mkdir(parents=True,exist_ok=True);p=log=ws=None
    try:
        event(key,"client_start",stage="启动Linux客户端（为本地SDK提供业务页）",slot=slot["name"],display=slot["display"],cdp_port=slot["port"])
        event(key,"client_cleanup",stage=f"启动前清理账号 profile={key} 的槽位资源")
        kill_profile_clients(str(profile))
        clear_stale_profile_locks(str(profile))
        p,log=start_client(str(profile),slot["port"],slot["display"])
        active_clients[key]=(p,log)
        try:
            ready=await wait_page_or_api(timeout=35,key=key,profile=str(profile),log=log,port=slot["port"])
        except TimeoutError:
            # The vendor client can finish its API/MQTT bootstrap while its
            # first renderer dies. Do one clean, bounded relaunch instead of
            # waiting forever on a desktop that no longer has a CDP page.
            event(key,"cdp_relaunch",stage="首次Renderer未出现，清理后重新启动客户端")
            active_clients.pop(key,None); stop_client(p,log)
            kill_profile_clients(str(profile)); clear_stale_profile_locks(str(profile))
            await asyncio.sleep(1)
            p,log=start_client(str(profile),slot["port"],slot["display"]); active_clients[key]=(p,log)
            ready=await wait_page_or_api(timeout=35,key=key,profile=str(profile),log=log,port=slot["port"])
        if ready[0]=="running":
            event(key,"success",stage="保活成功：当前客户端启动后的云端状态已是运行中",method="current_launch_api_state",evidence="当前启动日志/MQTT vmStatus=1",reason="current_launch_vmStatus_1",vmStatus=1,vmStatusShow=ready[1][1][:80],keepalive_confirmed=True)
            return
        t=ready[1]
        ws=websocket.create_connection(t["webSocketDebuggerUrl"],timeout=15);c=CDP(ws)

        c,ws,info=await login_and_find(c,ws,key,cfg)
        if "#/home" in str(info.get("url",'')) and "暂无任何匹配结果" not in str(info.get("body",'')) and any(x in str(info.get("body",'')) for x in ("连接","运行中","正常","已关机","家庭云电脑","个人云电脑")):
            # Short-circuit an already-running cloud before SDK/click. The renderer
            # may expose vmStatus=1 even when the card text is delayed.
            running,st,rt=already_running(c)
            if running:
                reason="initial_running_vmStatus_1" if rt.get("vm1") else "initial_cloud_state_normal"
                event(key,"success",stage="保活成功：进入业务页时云电脑已经运行中",method="initial_state",evidence="当前renderer云卡状态",reason=reason,vmStatus=1 if rt.get("vm1") else None,card=str(st.get("text",''))[:260],keepalive_confirmed=True)
                return
            try: await sdk_keepalive(key,c,ws,cfg)
            except Exception as e:
                event(key,"sdk_failed",stage="本地SDK失败，准备点击兜底",reason=f"{type(e).__name__}: {str(e)[:220]}")
                await click_fallback(key,c,ws,cfg)
            return
        try: await sdk_keepalive(key,c,ws,cfg)
        except Exception as e:
            event(key,"sdk_failed",stage="本地SDK失败，准备点击兜底",reason=f"{type(e).__name__}: {str(e)[:220]}")
            await click_fallback(key,c,ws,cfg)
    finally:
        if ws:
            try:ws.close()
            except Exception:pass
        active_clients.pop(key,None)
        stop_client(p,log)
        kill_profile_clients(str(profile))
        clear_stale_profile_locks(str(profile))
        reap_children()
async def worker(key):
    lock=account_locks.setdefault(key,asyncio.Lock())
    reap_children()
    # Fixed-deadline cadence: request time no longer adds drift to the next
    # probe. The configured interval itself remains unchanged.
    next_probe=time.monotonic()
    async with lock:
        while True:
            if key in stopped_accounts:return
            cfg=read_accounts().get(key)
            if not cfg:return
            try:
                reap_children()
                result=await asyncio.to_thread(probe_account,key,cfg)
                cls=str(result.get("class","unknown")); previous,count=probe_last.get(key,(None,0))
                count=count+1 if cls==previous else 1; probe_last[key]=(cls,count)
                event(key,"probe_result",stage=f"接口探针：{cls}，第{count}次连续结果",probe_class=cls,probe_count=count,vmStatus=result.get("vmStatus"),detail=result.get("reason",""))
                if cls=="maybe_skip":
                    event(key,"probe_normal",stage="接口探针确认云电脑正常，跳过客户端保活",method="api_probe",evidence="当前SOHO云电脑列表状态",vmStatus=result.get("vmStatus"),detail=result.get("reason",""))
                elif cls in ("suspect","need") and count==1:
                    # Fast confirmation only after the first suspect/need result.
                    # Regular healthy cadence stays unchanged; the extra check
                    # prevents waiting a full 10 seconds to confirm a real drop.
                    await asyncio.sleep(1)
                    confirm=await asyncio.to_thread(probe_account,key,cfg)
                    ccls=str(confirm.get("class","unknown")); probe_last[key]=(ccls,2 if ccls==cls else 1)
                    event(key,"probe_fast_confirm",stage="首次异常后1秒快速复核",probe_class=ccls,probe_count=2 if ccls==cls else 1,vmStatus=confirm.get("vmStatus"),detail=confirm.get("reason",""))
                    if ccls in ("suspect","need"):
                        event(key,"probe_trigger",stage="快速复核仍异常，触发SDK/点击保活",trigger_class=ccls,probe_count=2,trigger_policy="fast_confirm_1s",detail=confirm.get("reason",""))
                        await run_once(key,cfg)
                        probe_last[key]=(None,0)
                    elif ccls=="maybe_skip":
                        event(key,"probe_normal",stage="快速复核确认云电脑正常，跳过客户端保活",method="api_probe",evidence="异常后的快速复核",vmStatus=confirm.get("vmStatus"),detail=confirm.get("reason",""))
                elif cls in ("suspect","need") and (result.get("strong_shutdown") or count>=PROBE_CONSECUTIVE):
                    fast="strong_shutdown" if result.get("strong_shutdown") else "confirmed_twice"
                    event(key,"probe_trigger",stage="接口探针确认云电脑疑似关机，触发SDK/点击保活",trigger_class=cls,probe_count=count,trigger_policy=fast,detail=result.get("reason",""))
                    ok=await run_once(key,cfg)
                    # Verify immediately after a recovery attempt instead of
                    # waiting for the next periodic round.
                    if ok is not False:
                        try:
                            verify=await asyncio.to_thread(probe_account,key,cfg)
                            event(key,"post_recovery_probe",stage="保活动作后立即复核协议状态",probe_class=verify.get("class"),vmStatus=verify.get("vmStatus"),detail=verify.get("reason",""),keepalive_confirmed=verify.get("class")=="maybe_skip")
                        except Exception as ve:
                            event(key,"post_recovery_probe_error",stage="保活后协议复核失败，交由下一轮探针继续观察",reason=f"{type(ve).__name__}: {str(ve)[:180]}")
                    probe_last[key]=(None,0)
                elif cls=="unknown" and count>=PROBE_CONSECUTIVE and result.get("reason")!="缺少SohoToken":
                    event(key,"probe_unknown",stage="接口探针连续未知，触发一次客户端兜底",probe_count=count,detail=result.get("reason",""))
                    await run_once(key,cfg); probe_last[key]=(None,0)
                elif cls=="unknown" and result.get("reason")=="缺少SohoToken":
                    event(key,"probe_token_missing",stage="接口探针缺少Token，启动一次客户端以建立登录缓存")
                    await run_once(key,cfg); probe_last[key]=(None,0)
            except asyncio.CancelledError:raise
            except Exception as e:
                if key in stopped_accounts:return
                event(key,"error",stage="本轮失败",reason=f"{type(e).__name__}: {str(e)[:240]}")
            if key in stopped_accounts:return
            next_probe+=PROBE_INTERVAL
            await asyncio.sleep(max(0.0,next_probe-time.monotonic()))

async def probe_scheduler():
    event("_scheduler","scheduler",stage=f"实时探针运行中：每{PROBE_INTERVAL}秒扫描，协议探针正常跳过，SDK失败后点击兜底")
    while True:
        try:
            for k,cfg in read_accounts().items():
                if k in stopped_accounts or not cfg.get("autostart",True): continue
                if k not in tasks or tasks[k].done():
                    tasks[k]=asyncio.create_task(worker(k))
            await asyncio.sleep(PROBE_INTERVAL)
        except asyncio.CancelledError:raise
        except Exception as e:event("_scheduler","error",stage="探针异常",reason=str(e)[:240]);await asyncio.sleep(PROBE_INTERVAL)
@app.on_event("startup")
async def startup():
    stopped_accounts.clear()
    while not slot_pool.empty():
        try: slot_pool.get_nowait()
        except asyncio.QueueEmpty: break
    for slot in CLIENT_SLOTS: slot_pool.put_nowait(slot)
    global scheduler_task;scheduler_task=asyncio.create_task(probe_scheduler())
@app.on_event("shutdown")
async def shutdown():
    if scheduler_task:scheduler_task.cancel()

def account_view(k,x):
    st=dict(states.get(k) or {})
    if k in account_slots: st.update({"display":account_slots[k]["display"],"cdp_port":account_slots[k]["port"],"vnc_token":account_slots[k]["name"]})
    return {"key":k,**{q:v for q,v in x.items() if q not in ("password","interval_seconds","autostart")},"state":st}
@app.get("/")
def index():return FileResponse("/opt/cmcc-app/webui/index.html")
@app.get("/diagnostics/{key}.png")
def diagnostics_png(key):
    p=ROOT/(f"page-{key}.png")
    if not p.exists(): raise HTTPException(404,"diagnostic screenshot unavailable")
    return FileResponse(p,media_type="image/png",headers={"Cache-Control":"no-store"})
@app.get("/live/{key}.html")
def live_page(key):
    if key not in read_accounts(): raise HTTPException(404,"account not found")
    return FileResponse("/opt/cmcc-app/webui/live.html",media_type="text/html",headers={"Cache-Control":"no-store"})

@app.get("/runtime-config")
def runtime_config():
    return {"probe_interval":PROBE_INTERVAL,"fallback_concurrency":fallback_slots.limit,"active_slots":len(fallback_active),"max_slots":len(CLIENT_SLOTS)}
class RuntimeConfigIn(BaseModel):
    probe_interval:int=Field(ge=5,le=300)
    fallback_concurrency:int=Field(ge=1,le=8)
@app.post("/runtime-config")
def update_runtime_config(x:RuntimeConfigIn):
    global PROBE_INTERVAL
    with CONFIG_LOCK:
        PROBE_INTERVAL=x.probe_interval
        fallback_slots.set_limit(x.fallback_concurrency)
        RUNTIME_CONFIG.write_text(json.dumps({"probe_interval":PROBE_INTERVAL,"fallback_concurrency":fallback_slots.limit},ensure_ascii=False,indent=2),"utf-8")
        RUNTIME_CONFIG.chmod(0o600)
    event("_scheduler","runtime_config",stage=f"运行参数已更新：探针每{PROBE_INTERVAL}秒，槽位上限{fallback_slots.limit}",probe_interval=PROBE_INTERVAL,fallback_concurrency=fallback_slots.limit)
    return runtime_config()

@app.get("/health")
def health():return {"ok":True,"version":"1.4.0","accounts":len(read_accounts()),"running":[k for k,v in tasks.items() if not v.done()],"scheduler":"running" if scheduler_task and not scheduler_task.done() else "stopped","fallback_concurrency":fallback_slots.limit,"fallback_active":list(fallback_active),"client_slots":[{"name":s["name"],"display":s["display"],"cdp_port":s["port"]} for s in CLIENT_SLOTS]}
class PasswordChange(BaseModel):
    current_password:str=Field(min_length=1,max_length=300)
    new_password:str=Field(min_length=8,max_length=300)
@app.post("/auth/change-password")
def change_password(x:PasswordChange,request:Request):
    user,password=auth_credentials()
    supplied=request.headers.get("authorization","")
    if not hmac.compare_digest(x.current_password,password): raise HTTPException(403,"当前密码不正确")
    if x.new_password==x.current_password: raise HTTPException(400,"新密码不能与当前密码相同")
    p=ROOT/"webui-auth.json"; p.write_text(json.dumps({"username":user,"password":x.new_password},ensure_ascii=False),"utf-8"); p.chmod(0o600)
    return {"ok":True,"message":"密码已修改，请使用新密码重新登录"}
@app.get("/accounts")
def accounts():return [account_view(k,x) for k,x in read_accounts().items()]
@app.get("/accounts/export")
def export_accounts():
    """Export account credentials as a UTF-8 text file for the authenticated operator.

    This endpoint is protected by the global WebUI Basic Auth middleware. It
    deliberately returns only the supported import format and never logs the
    generated content.
    """
    lines=[]
    for x in read_accounts().values():
        try:
            username=str(x.get("username", "")); password=decrypt(str(x.get("password", "")))
        except Exception:
            continue
        if not username: continue
        mode=normalize_login_mode(x.get("login_mode", "main"))
        lines.append(f"{username},{password},{'子账号登录' if mode == 'sub' else '账密登录'}")
    body="\n".join(lines)+("\n" if lines else "")
    return PlainTextResponse(body,media_type="text/plain; charset=utf-8",headers={"Content-Disposition":"attachment; filename=cmcc-accounts.txt","Cache-Control":"no-store"})
@app.post("/accounts")
def add(x:AccountIn):return {"key":put_account(x.username,x.password,x.login_mode,x.name)}
@app.post("/accounts/import")
def import_accounts(x:ImportIn):
    d={} if x.replace else read_accounts();added=[];skipped=[]
    # Match the Windows v127 importer: tolerate UTF-8 BOM, Chinese commas,
    # tabs, ---- and | separators, and optional login-mode labels.
    text=x.text.replace("\ufeff","").replace("，",",")
    rows=[]
    for raw in text.splitlines():
        raw=raw.strip()
        if not raw or raw.startswith("#"): continue
        prefix=re.match(r"^(账密登录|账号登录|子账号登录|子帐号登录|子账号|子帐号)[:：,\s]+", raw, flags=re.I)
        prefix_mode=normalize_login_mode(prefix.group(1)) if prefix else None
        raw=raw[prefix.end():] if prefix else raw
        if "," in raw: row=[v.strip() for v in raw.split(",")]
        elif "----" in raw: row=[v.strip() for v in raw.split("----")]
        elif "|" in raw: row=[v.strip() for v in raw.split("|")]
        else: row=[v.strip() for v in re.split(r"\s+",raw)]
        if prefix_mode=="sub": row.append("子账号登录")
        elif prefix_mode=="main": row.append("账密登录") if prefix else None
        rows.append(row)
    for n,row in enumerate(rows,1):
        if not row or not any(row) or row[0].lower() in ("username","account","账号"):continue
        # Supported formats:
        #   main: account,password
        #   sub:  sub_account,password,子账号登录
        if len(row)>3 and any(normalize_login_mode(v)=="main" for v in row[2:]): row=[row[0],row[1],"账密登录"]
        if len(row) not in (2,3):
            skipped.append({"line":n,"reason":"格式应为：账号，密码 或 子账号，密码，子账号登录"});continue
        mode=normalize_login_mode(x.login_mode); clean=[]
        for value in row:
            if normalize_login_mode(value)=="sub": mode="sub"
            elif normalize_login_mode(value)=="main" and value.strip() in ("账密登录","账号登录"): mode="main"
            else: clean.append(value)
        if len(clean)<2:
            skipped.append({"line":n,"reason":"没有解析到账号和密码"});continue
        u,p=clean[0],clean[1]
        if any(v.get("username")==u and v.get("login_mode")==mode for v in d.values()):
            skipped.append({"line":n,"reason":"重复账号","username":u});continue
        k=secrets.token_hex(8);d[k]={"name":u,"username":u,"login_mode":mode,"connect_timeout":120,"autostart":False,"password":_fernet.encrypt(p.encode()).decode()};added.append(k)
    write_accounts(d);return {"added":len(added),"skipped":skipped,"keys":added,"format":"Windows v127兼容：账号,密码 / 账号,密码,子账号登录 / 账号----密码 / 账号 密码","replace":x.replace}
@app.delete("/accounts/{key}")
async def delete(key):
    t=tasks.pop(key,None)
    if t:t.cancel()
    d=read_accounts()
    if key not in d:raise HTTPException(404,"account not found")
    d.pop(key);write_accounts(d);return {"deleted":key}
class BulkDeleteIn(BaseModel):
    keys:list[str]=Field(min_length=1,max_length=1000)
@app.post("/accounts/bulk-delete")
async def bulk_delete(x:BulkDeleteIn):
    d=read_accounts(); requested=list(dict.fromkeys(str(k) for k in x.keys)); deleted=[]; missing=[]
    for key in requested:
        if key not in d: missing.append(key); continue
        t=tasks.pop(key,None)
        if t:t.cancel()
        stopped_accounts.add(key); stop_active_client(key); d.pop(key); states.pop(key,None); probe_last.pop(key,None); probe_initialized.discard(key); deleted.append(key)
    write_accounts(d)
    return {"deleted":len(deleted),"deleted_keys":deleted,"missing":missing}
class BulkStopIn(BaseModel):
    keys:list[str]|None=None
@app.post("/accounts/bulk-stop")
async def bulk_stop(x:BulkStopIn=BulkStopIn()):
    all_accounts=read_accounts()
    requested=list(dict.fromkeys(str(k) for k in (x.keys or list(all_accounts.keys()))))
    stopped=[]; missing=[]
    for key in requested:
        if key not in all_accounts:
            missing.append(key); continue
        event(key,"action_received",stage="收到用户操作：stop",action="stop")
        stopped_accounts.add(key)
        all_accounts[key]["autostart"]=False
        t=tasks.pop(key,None)
        if t:t.cancel()
        stop_active_client(key)
        event(key,"stopped",stage="已停止（客户端进程已关闭，不会被实时探针自动重启）",reason="user")
        stopped.append(key)
    write_accounts(all_accounts)
    return {"stopped":len(stopped),"stopped_keys":stopped,"missing":missing}
class BulkStartIn(BaseModel):
    keys:list[str]|None=None
@app.post("/accounts/bulk-start")
async def bulk_start(x:BulkStartIn=BulkStartIn()):
    all_accounts=read_accounts()
    requested=list(dict.fromkeys(str(k) for k in (x.keys or list(all_accounts.keys()))))
    started=[]; missing=[]
    for key in requested:
        if key not in all_accounts:
            missing.append(key); continue
        stopped_accounts.discard(key)
        all_accounts[key]["autostart"]=True
        event(key,"action_received",stage="收到用户操作：start",action="start")
        old=tasks.get(key)
        if old is None or old.done():
            tasks[key]=asyncio.create_task(worker(key)); started.append(key)
        else:
            started.append(key)
    write_accounts(all_accounts)
    return {"started":len(started),"started_keys":started,"missing":missing}
@app.post("/accounts/{key}/action")
async def action(key:str,x:Action):
    if key not in read_accounts():raise HTTPException(404,"account not found")
    if x.action in ("start","run"):
        event(key,"action_received",stage=f"收到用户操作：{x.action}",action=x.action)
        stopped_accounts.discard(key)
        cfg=read_accounts().get(key)
        if cfg:
            cfg["autostart"]=True
            all_accounts=read_accounts();all_accounts[key]=cfg;write_accounts(all_accounts)
        old=tasks.pop(key,None) if x.action=="run" else None
        if old:old.cancel()
        # Stop and Run must synchronously tear down the previous client before
        # creating a new worker; otherwise the new Electron becomes a proxy.
        stop_active_client(key)
        if key not in tasks or tasks[key].done():tasks[key]=asyncio.create_task(worker(key))
    elif x.action=="stop":
        event(key,"action_received",stage="收到用户操作：stop",action="stop")
        stopped_accounts.add(key)
        all_accounts=read_accounts()
        if key in all_accounts:
            all_accounts[key]["autostart"]=False
            write_accounts(all_accounts)
        t=tasks.pop(key,None)
        if t:t.cancel()
        stop_active_client(key)
        event(key,"stopped",stage="已停止（客户端进程已关闭，不会被实时探针自动重启）",reason="user")
    else:raise HTTPException(400,"action must be start, stop or run")
    return {"key":key,"action":x.action}
@app.get("/diagnostics/{key}.png")
def diagnostic_image(key):
    path=ROOT/(f"page-{key}.png")
    if not path.exists(): raise HTTPException(404,"diagnostic image unavailable")
    return FileResponse(path,media_type="image/png")
@app.get("/events")
def events():
    if not EVENTS.exists():return []
    # Never read the whole unbounded JSONL file on every 1-second WebUI poll.
    # The old read_text().splitlines() caused the Python service RSS to grow
    # into gigabytes once events.jsonl became large.
    try:
        with EVENTS.open("rb") as f:
            f.seek(0,2); end=f.tell(); f.seek(max(0,end-512*1024)); raw=f.read().decode("utf-8","replace")
        lines=raw.splitlines()[-500:]
    except Exception:
        return []
    out=[]
    for x in lines:
        if not x.strip(): continue
        try: out.append(json.loads(x))
        except Exception: pass
    return out
